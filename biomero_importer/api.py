"""Stable client-facing API for BIOMERO.importer upload orders."""

from copy import deepcopy
from typing import Any, Mapping

from biomero_schema.imports import (
    SHALLOW_ZARR_OPERATION,
    ImportOptionsEnvelope,
    parse_import_options,
)

from .utils.ingest_tracker import STAGE_NEW_ORDER, log_ingestion_step


IMPORTER_API_SCHEMA = 1


class UnsupportedImportOperation(ValueError):
    """Raised when an order requests an unavailable lifecycle operation."""


def _flag_enabled(name: str, default: bool = False) -> bool:
    import os

    return os.getenv(name, str(default)).strip().lower() == "true"


def get_importer_capabilities() -> dict[str, Any]:
    """Return capabilities without requiring a running importer process."""

    operations = []
    if _flag_enabled("BIOMERO_SHALLOW_ZARR"):
        operations.append(SHALLOW_ZARR_OPERATION)
    return {
        "schema": IMPORTER_API_SCHEMA,
        "importOptionsSchemas": [1, 2],
        "lifecycleOperations": operations,
        "externalPreprocessing": True,
    }


def supports_import_options(
    value: ImportOptionsEnvelope | Mapping[str, Any] | None,
) -> bool:
    """Return whether this importer can execute every requested operation."""

    envelope = parse_import_options(value)
    supported = set(get_importer_capabilities()["lifecycleOperations"])
    return all(operation.kind in supported for operation in envelope.operations)


def submit_import_order(
    order: Mapping[str, Any],
    *,
    log_order=log_ingestion_step,
) -> str:
    """Validate and durably enqueue an import order.

    Existing callers may keep using ``log_ingestion_step``. New clients get a
    capability-checked API which normalizes the versioned options envelope
    before the append-only pending event is written.
    """

    required = (
        "Group",
        "Username",
        "UUID",
        "DestinationID",
        "DestinationType",
        "Files",
    )
    missing = [name for name in required if name not in order]
    if missing:
        raise ValueError(
            "Missing required import order fields: " + ", ".join(missing)
        )
    normalized = deepcopy(dict(order))
    envelope = parse_import_options(normalized.get("ImportOptions"))
    if envelope.operations and not supports_import_options(envelope):
        requested = ", ".join(
            operation.kind for operation in envelope.operations
        )
        raise UnsupportedImportOperation(
            f"Importer does not support requested operation(s): {requested}"
        )
    # Preserve the old empty/flat representation unless a caller explicitly
    # uses lifecycle operations. This makes submission behavior transparent to
    # existing import setups and older readers of the event table.
    if envelope.operations:
        normalized["ImportOptions"] = envelope.to_dict()
    log_order(normalized, STAGE_NEW_ORDER)
    return str(normalized["UUID"])


__all__ = [
    "IMPORTER_API_SCHEMA",
    "UnsupportedImportOperation",
    "get_importer_capabilities",
    "submit_import_order",
    "supports_import_options",
]
