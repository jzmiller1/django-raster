import datetime
import fnmatch
import os
import tempfile
import zipfile

import numpy
from celery import current_app, group
from celery.contrib.methods import task_method

from django.conf import settings
from django.contrib.gis.gdal import GDALRaster
from django.contrib.gis.gdal.error import GDALException
from django.core.files import File
from django.db import connection
from django.dispatch import Signal
from raster.exceptions import RasterException
from raster.models import RasterLayerBandMetadata, RasterLayerReprojected, RasterTile
from raster.tiles import utils
from raster.tiles.const import WEB_MERCATOR_SRID, WEB_MERCATOR_TILESIZE

rasterlayers_parser_ended = Signal(providing_args=['instance'])


class RasterLayerParser(object):
    """
    Class to parse raster layers.
    """
    def __init__(self, rasterlayer):
        self.rasterlayer = rasterlayer

        # Set raster tilesize
        self.tilesize = int(getattr(settings, 'RASTER_TILESIZE', WEB_MERCATOR_TILESIZE))
        self.zoomdown = getattr(settings, 'RASTER_ZOOM_NEXT_HIGHER', True)

    def log(self, msg, status=None, zoom=None):
        """
        Write a message to the parse log of the rasterlayer instance and update
        the parse status object.
        """
        parsestatus = self.rasterlayer.parsestatus
        parsestatus.refresh_from_db()

        if status is not None:
            parsestatus.status = status

        if zoom is not None and zoom not in parsestatus.tile_levels:
            parsestatus.tile_levels.append(zoom)
            parsestatus.tile_levels.sort()

        # Prepare datetime stamp for log
        now = '[{0}] '.format(datetime.datetime.now().strftime('%Y-%m-%d %T'))

        if parsestatus.log:
            now = '\n' + now

        parsestatus.log += now + msg
        parsestatus.save()

    def open_raster_file(self):
        """
        Get raster source file to extract tiles from.

        This makes a local copy of rasterfile, unzips the raster and reprojects
        it into web mercator if necessary. The reprojected raster is stored for
        reuse such that reprojection does only happen once.

        The local copy of the raster is needed if files are stored on remote
        storages.
        """
        reproj, created = RasterLayerReprojected.objects.get_or_create(rasterlayer=self.rasterlayer)

        # Choose source for raster data, use the reprojected version if it exists.
        if not reproj.rasterfile:
            rasterfile_source = self.rasterlayer.rasterfile
        else:
            rasterfile_source = reproj.rasterfile

        # Create workdir
        raster_workdir = getattr(settings, 'RASTER_WORKDIR', None)
        self.tmpdir = tempfile.mkdtemp(dir=raster_workdir)

        # Copy raster file source to local folder
        filepath = os.path.join(self.tmpdir, os.path.basename(rasterfile_source.name))
        rasterfile = open(filepath, 'wb')
        for chunk in rasterfile_source.chunks():
            rasterfile.write(chunk)
        rasterfile.close()

        # If the raster file is compressed, decompress it, otherwise try to
        # open the source file directly.
        if os.path.splitext(rasterfile.name)[1].lower() == '.zip':
            # Open and extract zipfile
            zf = zipfile.ZipFile(rasterfile.name)
            zf.extractall(self.tmpdir)

            # Remove zipfile
            os.remove(rasterfile.name)

            # Get filelist from directory
            matches = []
            for root, dirnames, filenames in os.walk(self.tmpdir):
                for filename in fnmatch.filter(filenames, '*.*'):
                    matches.append(os.path.join(root, filename))

            # Open the first raster file found in the matched files.
            self.dataset = None
            for match in matches:
                try:
                    self.dataset = GDALRaster(match, write=True)
                    break
                except GDALException:
                    pass

            # Raise exception if no file could be opened by gdal.
            if not self.dataset:
                raise RasterException('Could not open rasterfile.')
        else:
            self.dataset = GDALRaster(rasterfile.name, write=True)

        # Extract metadata
        if created:
            self.extract_metadata()

        # Reproject raster into web mercator if necessary
        if not self.dataset.srs.srid == WEB_MERCATOR_SRID:
            self.log(
                'Transforming raster to SRID {0}'.format(WEB_MERCATOR_SRID),
                status=self.rasterlayer.parsestatus.REPROJECTING_RASTER
            )

            # Reproject the dataset
            self.dataset = self.dataset.transform(WEB_MERCATOR_SRID)

            # Compress reprojected raster file and store it
            dest = tempfile.NamedTemporaryFile(dir=self.tmpdir, suffix='.zip')
            dest_zip = zipfile.ZipFile(dest.name, 'w', allowZip64=True)
            dest_zip.write(
                filename=self.dataset.name,
                arcname=os.path.basename(self.dataset.name),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            dest_zip.close()

            # Store zip file in reprojected raster model
            reproj.rasterfile = File(open(dest_zip.filename, 'rb'))
            reproj.save()
            self.log('Finished transforming raster.')

        # Make sure nodata value is set from input on all bands
        if self.rasterlayer.nodata not in ('', None):
            for band in self.dataset.bands:
                band.nodata_value = float(self.rasterlayer.nodata)

    def create_initial_histogram_buckets(self):
        """
        Gets the empty histogram arrays for statistics collection.
        """
        self.hist_values = []
        self.hist_bins = []

        for i, band in enumerate(self.dataset.bands):
            bandmeta = RasterLayerBandMetadata.objects.filter(rasterlayer=self.rasterlayer, band=i).first()
            self.hist_values.append(numpy.array(bandmeta.hist_values))
            self.hist_bins.append(numpy.array(bandmeta.hist_bins))

    def extract_metadata(self):
        """
        Extract and store metadata for the raster and its bands.
        """
        self.log('Extracting metadata from raster.')

        # Extract global raster metadata
        meta = self.rasterlayer.metadata
        meta.uperleftx = self.dataset.origin.x
        meta.uperlefty = self.dataset.origin.y
        meta.width = self.dataset.width
        meta.height = self.dataset.height
        meta.scalex = self.dataset.scale.x
        meta.scaley = self.dataset.scale.y
        meta.skewx = self.dataset.skew.x
        meta.skewy = self.dataset.skew.y
        meta.numbands = len(self.dataset.bands)
        meta.srs_wkt = self.dataset.srs.wkt
        meta.srid = self.dataset.srs.srid
        meta.save()

        # Extract band metadata
        for i, band in enumerate(self.dataset.bands):
            bandmeta = RasterLayerBandMetadata.objects.filter(rasterlayer=self.rasterlayer, band=i).first()
            if not bandmeta:
                bandmeta = RasterLayerBandMetadata(rasterlayer=self.rasterlayer, band=i)

            bandmeta.nodata_value = band.nodata_value
            bandmeta.min = band.min
            bandmeta.max = band.max
            # Depending on Django version, the band statistics include std and mean.
            if hasattr(band, 'std'):
                bandmeta.std = band.std
            if hasattr(band, 'mean'):
                bandmeta.mean = band.mean
            bandmeta.save()

        self.log('Finished extracting metadata from raster.')

    def create_tiles(self, zoom_levels):
        """
        Create tiles for input zoom levels, either a list or an integer.
        """
        if isinstance(zoom_levels, int):
            self.populate_tile_level(zoom_levels)
        else:
            for zoom in zoom_levels:
                self.populate_tile_level(zoom)

    def populate_tile_level(self, zoom):
        """
        Create tiles for this raster at the given zoomlevel.

        This routine first snaps the raster to the grid of the zoomlevel,
        then creates  the tiles from the snapped raster.
        """
        # Abort if zoom level is above resolution of the raster layer
        if zoom > self.max_zoom:
            return
        elif zoom == self.max_zoom:
            self.create_initial_histogram_buckets()

        # Compute the tile x-y-z index range for the rasterlayer for this zoomlevel
        bbox = self.rasterlayer.extent()
        quadrants = utils.quadrants(bbox, zoom)

        self.log('Creating {0} tiles in {1} quadrants at zoom {2}.'.format(self.nr_of_tiles(zoom), len(quadrants), zoom))

        # Process quadrants in parallell
        quadrant_task_group = group(self.process_quadrant.si(indexrange, zoom) for indexrange in quadrants)
        quadrant_task_group.apply()

        # Store histogram data
        if zoom == self.max_zoom:
            bandmetas = RasterLayerBandMetadata.objects.filter(rasterlayer=self.rasterlayer)
            for bandmeta in bandmetas:
                bandmeta.hist_values = self.hist_values[bandmeta.band].tolist()
                bandmeta.save()

        self.log('Finished parsing at zoom level {0}.'.format(zoom), zoom=zoom)

    _quadrant_count = 0

    @current_app.task(filter=task_method)
    def process_quadrant(self, indexrange, zoom):
        """
        Create raster tiles for a quadrant of tiles defined by a x-y-z index
        range and a zoom level.
        """
        self._quadrant_count += 1
        self.log(
            'Starting tile creation for quadrant {0} at zoom level {1}'.format(self._quadrant_count, zoom),
            status=self.rasterlayer.parsestatus.CREATING_TILES
        )

        # Compute scale of tiles for this zoomlevel
        tilescale = utils.tile_scale(zoom)

        # Compute quadrant bounds and create destination file
        bounds = utils.tile_bounds(indexrange[0], indexrange[1], zoom)
        dest_file = tempfile.NamedTemporaryFile(dir=self.tmpdir, suffix='.tif')

        # Snap dataset to the quadrant
        snapped_dataset = self.dataset.warp({
            'name': dest_file.name,
            'origin': [bounds[0], bounds[3]],
            'scale': [tilescale, -tilescale],
            'width': (indexrange[2] - indexrange[0] + 1) * self.tilesize,
            'height': (indexrange[3] - indexrange[1] + 1) * self.tilesize,
        })

        # Create all tiles in this quadrant
        for tilex in range(indexrange[0], indexrange[2] + 1):
            for tiley in range(indexrange[1], indexrange[3] + 1):
                # Calculate raster tile origin
                bounds = utils.tile_bounds(tilex, tiley, zoom)

                # Construct band data arrays
                pixeloffset = (
                    (tilex - indexrange[0]) * self.tilesize,
                    (tiley - indexrange[1]) * self.tilesize
                )

                band_data = [
                    {
                        'data': band.data(offset=pixeloffset, size=(self.tilesize, self.tilesize)),
                        'nodata_value': band.nodata_value
                    } for band in snapped_dataset.bands
                ]

                # Add tile data to histogram
                if zoom == self.max_zoom:
                    self.push_histogram(band_data)

                # Warp source raster into this tile (in memory)
                dest = GDALRaster({
                    'width': self.tilesize,
                    'height': self.tilesize,
                    'origin': [bounds[0], bounds[3]],
                    'scale': [tilescale, -tilescale],
                    'srid': WEB_MERCATOR_SRID,
                    'datatype': snapped_dataset.bands[0].datatype(),
                    'bands': band_data,
                })

                # Store tile
                RasterTile.objects.create(
                    rast=dest,
                    rasterlayer=self.rasterlayer,
                    tilex=tilex,
                    tiley=tiley,
                    tilez=zoom
                )

    def push_histogram(self, data):
        """
        Add data to band level histogram.
        """
        # Loop through bands of this tile
        for i, dat in enumerate(data):
            # Create histogram for new data with the same bins
            new_hist = numpy.histogram(dat['data'], bins=self.hist_bins[i])
            # Add counts of this tile to band metadata histogram
            self.hist_values[i] += new_hist[0]

    def drop_all_tiles(self):
        """
        Delete all existing tiles for this parser's rasterlayer.
        """
        self.log('Clearing all existing tiles.')
        self.rasterlayer.rastertile_set.all().delete()
        self.log('Finished clearing existing tiles.')

    def drop_empty_tiles(self):
        """
        Remove rasters that are only no-data from the current rasterlayer.
        """
        self.log(
            'Dropping empty raster tiles.',
            status=self.rasterlayer.parsestatus.DROPPING_EMPTY_TILES
        )

        # Setup SQL command
        sql = (
            "DELETE FROM raster_rastertile "
            "WHERE ST_Count(rast)=0 "
            "AND rasterlayer_id={0}"
        ).format(self.rasterlayer.id)

        # Run SQL to drop empty tiles
        cursor = connection.cursor()
        cursor.execute(sql)
        self.log('Finished dropping empty raster tiles.')

    def send_success_signal(self):
        """
        Send parser end signal for other dependencies to be handling new tiles.
        """
        self.log(
            'Successfully finished parsing raster',
            status=self.rasterlayer.parsestatus.FINISHED
        )
        rasterlayers_parser_ended.send(sender=self.rasterlayer.__class__, instance=self.rasterlayer)

    def compute_max_zoom(self):
        """
        Set max zoom property based on rasterlayer metadata.
        """
        # Compute max zoom at the web mercator projection
        self.max_zoom = utils.closest_zoomlevel(
            abs(self.dataset.scale.x)
        )

        # Store max zoom level in metadata
        self.rasterlayer.metadata.max_zoom = self.max_zoom
        self.rasterlayer.metadata.save()

        # Reduce max zoom by one if zoomdown flag was disabled
        if not self.zoomdown:
            self.max_zoom -= 1

    def nr_of_tiles(self, zoom):
        """
        Compute the number of tiles for the rasterlayer on a given zoom level.
        """
        bbox = self.rasterlayer.extent()
        indexrange = utils.tile_index_range(bbox, zoom)
        return (indexrange[2] - indexrange[0] + 1) * (indexrange[3] - indexrange[1] + 1)