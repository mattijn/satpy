#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2022 Satpy developers
#
# This file is part of satpy.
#
# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.
"""VIIRS NOAA enterprise L2 product reader.

This module defines the :class:`VIIRSJRRFileHandler` file handler, to
be used for reading VIIRS Level 2 products generated by the NOAA enterprise
suite, which are downloadable via NOAA CLASS.
A wide variety of such products exist and, at present, only three are
supported here, showing example filenames:
 - Cloud mask: JRR-CloudMask_v2r3_j01_s202112250807275_e202112250808520_c202112250837300.nc
 - Aerosol properties: JRR-ADP_v2r3_j01_s202112250807275_e202112250808520_c202112250839550.nc
 - Surface reflectance: SurfRefl_v1r1_j01_s202112250807275_e202112250808520_c202112250845080.nc
All products use the same base reader `viirs_l2_jrr` and can be read through satpy with::

    import satpy
    import glob

    filenames = glob.glob('JRR-ADP*.nc')
    scene = satpy.Scene(filenames,
                        reader='viirs_l2_jrr')
    scene.load(['smoke_concentration'])

NOTE:
Multiple products contain datasets with the same name! For example, both the cloud mask
and aerosol files contain a cloud mask, but these are not identical.
For clarity, the aerosol file cloudmask is named `cloud_mask_adp` in this reader.
"""


import logging

import numpy as np
import xarray as xr

from satpy import DataID
from satpy.readers.file_handlers import BaseFileHandler
from satpy.utils import get_chunk_size_limit

LOG = logging.getLogger(__name__)


class VIIRSJRRFileHandler(BaseFileHandler):
    """NetCDF4 reader for VIIRS Active Fires."""

    def __init__(self, filename, filename_info, filetype_info):
        """Initialize the geo filehandler."""
        super(VIIRSJRRFileHandler, self).__init__(filename, filename_info,
                                                  filetype_info)
        # use entire scans as chunks
        row_chunks_m = max(get_chunk_size_limit() // 4 // 3200, 1)  # 32-bit floats
        row_chunks_i = row_chunks_m * 2
        self.nc = xr.open_dataset(self.filename,
                                  decode_cf=True,
                                  mask_and_scale=True,
                                  chunks={
                                      'Columns': -1,
                                      'Rows': row_chunks_i,
                                      'Along_Scan_375m': -1,
                                      'Along_Track_375m': row_chunks_i,
                                      'Along_Scan_750m': -1,
                                      'Along_Track_750m': row_chunks_m,
                                  })
        if 'columns' in self.nc.dims:
            self.nc = self.nc.rename({'Columns': 'x', 'Rows': 'y'})
        elif 'Along_Track_375m' in self.nc.dims:
            self.nc = self.nc.rename({'Along_Scan_375m': 'x', 'Along_Track_375m': 'y'})
            self.nc = self.nc.rename({'Along_Scan_750m': 'x', 'Along_Track_750m': 'y'})

        # For some reason, no 'standard_name' is defined in some netCDF files, so
        # here we manually make the definitions.
        if 'Latitude' in self.nc:
            self.nc['Latitude'].attrs.update({'standard_name': 'latitude'})
        if 'Longitude' in self.nc:
            self.nc['Longitude'].attrs.update({'standard_name': 'longitude'})

        self.algorithm_version = filename_info['platform_shortname']
        self.sensor_name = 'viirs'

    def rows_per_scans(self, data_arr: xr.DataArray) -> int:
        """Get number of array rows per instrument scan based on data resolution."""
        return 32 if data_arr.shape[1] == 6400 else 16

    def get_dataset(self, dataset_id: DataID, info: dict) -> xr.DataArray:
        """Get the dataset."""
        data_arr = self.nc[info['file_key']]
        data_arr = self._mask_invalid(data_arr, info)
        units = info.get("units", data_arr.attrs.get("units"))
        if units is None or units == "unitless":
            units = "1"
        if units == "%" and data_arr.attrs.get("units") in ("1", "unitless"):
            data_arr *= 100.0  # turn into percentages
        data_arr.attrs["units"] = units
        if "standard_name" in info:
            data_arr.attrs["standard_name"] = info["standard_name"]
        self._decode_flag_meanings(data_arr)
        data_arr.attrs["platform_name"] = self.platform_name
        data_arr.attrs["sensor"] = self.sensor_name
        data_arr.attrs["rows_per_scan"] = self.rows_per_scans(data_arr)
        return data_arr

    def _mask_invalid(self, data_arr: xr.DataArray, ds_info: dict) -> xr.DataArray:
        fill_value = data_arr.encoding.get("_FillValue")
        if fill_value is not None and not np.isnan(fill_value):
            # xarray auto mask and scale handled this
            return data_arr
        yaml_fill = ds_info.get("_FillValue")
        if yaml_fill is not None:
            return data_arr.where(data_arr != yaml_fill)
        valid_range = ds_info.get("valid_range", data_arr.attrs.get("valid_range"))
        if valid_range is not None:
            return data_arr.where((valid_range[0] <= data_arr) & (data_arr <= valid_range[1]))
        return data_arr

    @staticmethod
    def _decode_flag_meanings(data_arr: xr.DataArray):
        flag_meanings = data_arr.attrs.get("flag_meanings", None)
        if isinstance(flag_meanings, str) and "\n" not in flag_meanings:
            # only handle CF-standard flag meanings
            data_arr.attrs['flag_meanings'] = [flag for flag in data_arr.attrs['flag_meanings'].split(' ')]

    @property
    def start_time(self):
        """Get first date/time when observations were recorded."""
        return self.filename_info['start_time']

    @property
    def end_time(self):
        """Get last date/time when observations were recorded."""
        return self.filename_info['end_time']

    @property
    def platform_name(self):
        """Get platform name."""
        platform_path = self.filename_info['platform_shortname']
        platform_dict = {'NPP': 'Suomi-NPP',
                         'JPSS-1': 'NOAA-20',
                         'J01': 'NOAA-20',
                         'JPSS-2': 'NOAA-21',
                         'J02': 'NOAA-21'}
        return platform_dict[platform_path.upper()]

    def available_datasets(self, configured_datasets=None):
        """Get information of available datasets in this file.

        Args:
            configured_datasets (list): Series of (bool or None, dict) in the
                same way as is returned by this method (see below). The bool
                is whether the dataset is available from at least one
                of the current file handlers. It can also be ``None`` if
                no file handler before us knows how to handle it.
                The dictionary is existing dataset metadata. The dictionaries
                are typically provided from a YAML configuration file and may
                be modified, updated, or used as a "template" for additional
                available datasets. This argument could be the result of a
                previous file handler's implementation of this method.

        Returns:
            Iterator of (bool or None, dict) pairs where dict is the
            dataset's metadata. If the dataset is available in the current
            file type then the boolean value should be ``True``, ``False``
            if we **know** about the dataset but it is unavailable, or
            ``None`` if this file object is not responsible for it.

        """
        for is_avail, ds_info in (configured_datasets or []):
            if is_avail is not None:
                # some other file handler said it has this dataset
                # we don't know any more information than the previous
                # file handler so let's yield early
                yield is_avail, ds_info
                continue
            if self.file_type_matches(ds_info['file_type']) is None:
                # this is not the file type for this dataset
                yield None, ds_info
            file_key = ds_info.get("file_key", ds_info["name"])
            yield file_key in self.nc, ds_info


class VIIRSSurfaceReflectanceWithVIHandler(VIIRSJRRFileHandler):
    """File handler for surface reflectance files with optional vegetation indexes."""

    def _mask_invalid(self, data_arr: xr.DataArray, ds_info: dict) -> xr.DataArray:
        new_data_arr = super()._mask_invalid(data_arr, ds_info)
        if ds_info["file_key"] in ("NDVI", "EVI"):
            good_mask = self._get_veg_index_good_mask()
            new_data_arr = new_data_arr.where(good_mask)
        return new_data_arr

    def _get_veg_index_good_mask(self) -> xr.DataArray:
        # each mask array should be TRUE when pixels are UNACCEPTABLE
        qf1 = self.nc['QF1 Surface Reflectance']
        has_sun_glint = (qf1 & 0b11000000) > 0
        is_cloudy = (qf1 & 0b00001100) > 0  # mask everything but "confident clear"
        cloud_quality = (qf1 & 0b00000011) < 0b10

        qf2 = self.nc['QF2 Surface Reflectance']
        has_snow_or_ice = (qf2 & 0b00100000) > 0
        has_cloud_shadow = (qf2 & 0b00001000) > 0
        water_mask = (qf2 & 0b00000111)
        has_water = (water_mask <= 0b010) | (water_mask == 0b101)  # shallow water, deep ocean, arctic

        qf7 = self.nc['QF7 Surface Reflectance']
        has_aerosols = (qf7 & 0b00001100) > 0b1000  # high aerosol quantity
        adjacent_to_cloud = (qf7 & 0b00000010) > 0

        bad_mask = (
                has_sun_glint |
                is_cloudy |
                cloud_quality |
                has_snow_or_ice |
                has_cloud_shadow |
                has_water |
                has_aerosols |
                adjacent_to_cloud
        )
        # upscale from M-band resolution to I-band resolution
        bad_mask_iband_dask = bad_mask.data.repeat(2, axis=1).repeat(2, axis=0)
        good_mask_iband = xr.DataArray(~bad_mask_iband_dask, dims=qf1.dims)
        return good_mask_iband
