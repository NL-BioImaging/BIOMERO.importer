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
