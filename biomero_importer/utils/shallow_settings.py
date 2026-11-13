# -*- coding: utf-8 -*-
# Copyright (C) 2026 Torec Luik
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
"""Environment settings of shallow-Zarr handling, read and parsed in one place.

This module has no heavy imports so that the API, the importer and the
lifecycle can all use it without pulling in optional dependencies.
"""

import os

ENV_SHALLOW_ZARR = "BIOMERO_SHALLOW_ZARR"
ENV_SHALLOW_WORKERS = "BIOMERO_SHALLOW_ZARR_WORKERS"
ENV_REMOTE_SHALLOW_ZARR = "BIOMERO_REMOTE_SHALLOW_ZARR"

# Shallowing is opt-in; consuming remote receipts is on unless switched off.
DEFAULT_SHALLOW_ZARR = False
DEFAULT_REMOTE_SHALLOW_ZARR = True
DEFAULT_SHALLOW_WORKERS = 1

_TRUE_VALUE = "true"


def env_flag_enabled(name: str, default: bool) -> bool:
    """
    Check whether an environment flag is set to ``true`` (case-insensitive).

    Args:
        name (str): Environment variable to read.
        default (bool): Value used when the variable is not set.

    Returns:
        bool: True only if the (stripped) value equals ``true`` ignoring
        case.
    """
    return os.getenv(name, str(default)).strip().lower() == _TRUE_VALUE
