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
"""Shallower's public Zarr result API, re-exported for importer callers.

Other components (for example biomero-scripts) import these names from the
importer package, so they stay available here. Only names that shallower
publishes in ``biomero_shallower.result_zarr.__all__`` are re-exported.
"""
from biomero_shallower.result_zarr import (
    MaterializedShallowResult,
    NgffNode,
    NormalizedShallowResult,
    ReturnedZarrDecision,
    ShallowRegistration,
    discover_ngff_nodes,
    evaluate_returned_zarr,
    find_returned_zarr_stores,
    is_failure_reason,
    load_managed_storage_roots,
    materialize_shallow_zarr,
    normalize_returned_zarr,
    remove_transfer_input_marker,
    resolve_managed_source_path,
    resolve_shallow_registration,
)

__all__ = [
    "MaterializedShallowResult",
    "NgffNode",
    "NormalizedShallowResult",
    "ReturnedZarrDecision",
    "ShallowRegistration",
    "discover_ngff_nodes",
    "evaluate_returned_zarr",
    "find_returned_zarr_stores",
    "is_failure_reason",
    "load_managed_storage_roots",
    "materialize_shallow_zarr",
    "normalize_returned_zarr",
    "remove_transfer_input_marker",
    "resolve_managed_source_path",
    "resolve_shallow_registration",
]
