"""Registration-independent lifecycle operations for importer orders."""

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Literal, Sequence

from biomero_schema.imports import (
    SHALLOW_ZARR_OPERATION,
    ImportOptionsEnvelope,
    ShallowZarrImportOperation,
    parse_import_options,
)
from biomero_schema.zarr import (
    SHALLOW_COLLECTION_MANIFEST,
    ShallowCollection,
    ZarrImportOptions,
)

from .pixel_identity import PixelIdentityError
from .result_zarr import (
    ReturnedZarrDecision,
    discover_ngff_nodes,
    evaluate_returned_zarr,
    normalize_returned_zarr,
)


@dataclass(frozen=True)
class PreparedImportItem:
    """One physical path and registration view produced by the lifecycle."""

    path: Path
    registration: ZarrImportOptions
    role: Literal["input", "primary", "image-label", "plate-label-preview"]


@dataclass(frozen=True)
class PreparedImportPlan:
    """Deterministic importer inputs after all requested native operations."""

    items: tuple[PreparedImportItem, ...]
    options: ImportOptionsEnvelope
    decisions: tuple[ReturnedZarrDecision, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.options.operations)


def _positive_int_env(name: str, default: int, logger: logging.Logger) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
        if parsed < 1:
            raise ValueError
        return parsed
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, value, default)
        return default


def _is_zarr(path: Path) -> bool:
    return path.is_dir() and (
        path.name.lower().endswith(".zarr")
        or (path / ".zattrs").is_file()
        or (path / "zarr.json").is_file()
    )


def _load_shallow_collection(path: Path) -> ShallowCollection | None:
    manifest = path / SHALLOW_COLLECTION_MANIFEST
    if not manifest.is_file():
        return None
    try:
        return ShallowCollection.from_dict(json.loads(
            manifest.read_text(encoding="utf-8")
        ))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PixelIdentityError(
            f"Invalid existing shallow collection: {manifest}"
        ) from exc


def _common_plate_label(
    collection: ShallowCollection,
    requested: str | None,
) -> str | None:
    labels_per_image = []
    for image in collection.images:
        if image.source.source_object_type != "Plate":
            return None
        prefix = PurePosixPath(image.image_node_path) / "labels"
        names = set()
        for path in image.label_node_paths:
            try:
                relative = PurePosixPath(path).relative_to(prefix)
            except ValueError:
                continue
            if len(relative.parts) == 1:
                names.add(relative.name)
        labels_per_image.append(names)
    common = set.intersection(*labels_per_image) if labels_per_image else set()
    if requested:
        return requested if requested in common else None
    return next(iter(common)) if len(common) == 1 else None


def _items_for_shallow_collection(
    root: Path,
    collection: ShallowCollection,
    operation: ShallowZarrImportOperation,
) -> tuple[PreparedImportItem, ...]:
    source_types = {
        image.source.source_object_type for image in collection.images
    }
    if source_types == {"Plate"}:
        items = [PreparedImportItem(
            path=root,
            registration=ZarrImportOptions(),
            role="primary",
        )]
        if operation.import_plate_label_preview:
            label_name = _common_plate_label(
                collection,
                operation.plate_label_name,
            )
            if label_name is not None:
                items.append(PreparedImportItem(
                    path=root,
                    registration=ZarrImportOptions(
                        platePixelSource="label",
                        plateLabelName=label_name,
                    ),
                    role="plate-label-preview",
                ))
        return tuple(items)

    if source_types != {"Image"}:
        raise PixelIdentityError(
            "Shallow collection must contain only Image or only Plate sources"
        )
    if not operation.import_image_label_views:
        return ()
    items = []
    for image in collection.images:
        local_components = (
            component for component in image.label_components
            if component.source is None
        )
        paths = (
            tuple(component.logical_node_path for component in local_components)
            if image.label_components
            else image.label_node_paths
        )
        items.extend(
            PreparedImportItem(
                path=root.joinpath(*PurePosixPath(path).parts),
                registration=ZarrImportOptions(),
                role="image-label",
            )
            for path in paths
        )
    return tuple(items)


def _full_result_items(
    root: Path,
    operation: ShallowZarrImportOperation,
    registration: ZarrImportOptions,
) -> tuple[PreparedImportItem, ...]:
    items = [PreparedImportItem(root, registration, "primary")]
    if not operation.import_image_label_views:
        return tuple(items)
    try:
        nodes = discover_ngff_nodes(root)
    except PixelIdentityError:
        return tuple(items)
    image_nodes = tuple(node for node in nodes if node.role == "image")
    if len(image_nodes) != 1 or image_nodes[0].node_path != ".":
        return tuple(items)
    items.extend(
        PreparedImportItem(
            root.joinpath(*PurePosixPath(node.node_path).parts),
            ZarrImportOptions(),
            "image-label",
        )
        for node in nodes
        if node.role == "label"
    )
    return tuple(items)


class ImportLifecycleEngine:
    """Execute optional native operations without knowing register.py."""

    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger(__name__)

    def prepare(
        self,
        files: Sequence[str | Path],
        options: ImportOptionsEnvelope | dict | None,
    ) -> PreparedImportPlan:
        envelope = parse_import_options(options)
        if not envelope.operations:
            return PreparedImportPlan(
                items=tuple(
                    PreparedImportItem(
                        Path(path), envelope.registration, "input"
                    )
                    for path in files
                ),
                options=envelope,
            )
        items = tuple(
            PreparedImportItem(Path(path), envelope.registration, "input")
            for path in files
        )
        decisions = ()
        for operation in envelope.operations:
            if operation.kind != SHALLOW_ZARR_OPERATION:
                raise ValueError(
                    f"Unsupported importer lifecycle operation {operation.kind}"
                )
            items, decisions = self._prepare_shallow(items, operation)
        return PreparedImportPlan(
            items=items,
            options=envelope,
            decisions=decisions,
        )

    def _prepare_shallow(
        self,
        current: tuple[PreparedImportItem, ...],
        operation: ShallowZarrImportOperation,
    ) -> tuple[tuple[PreparedImportItem, ...], tuple[ReturnedZarrDecision, ...]]:
        if os.getenv("BIOMERO_SHALLOW_ZARR", "false").lower() != "true":
            raise ValueError(
                "biomero.shallow-zarr requested but BIOMERO_SHALLOW_ZARR is disabled"
            )
        workers = _positive_int_env(
            "BIOMERO_SHALLOW_ZARR_WORKERS", 1, self.logger
        )
        prepared = []
        decisions = []
        for item in current:
            root = item.path
            if not _is_zarr(root):
                prepared.append(item)
                continue
            existing = _load_shallow_collection(root)
            if existing is not None:
                self.logger.info(
                    "Reusing existing shallow Zarr manifest for %s", root
                )
                prepared.extend(_items_for_shallow_collection(
                    root, existing, operation
                ))
                continue
            self.logger.info(
                "Evaluating returned Zarr %s with %s identity worker(s)",
                root,
                workers,
            )
            decision = evaluate_returned_zarr(
                root,
                operation.canonical_inputs,
                identity_workers=workers,
            )
            decisions.append(decision)
            self.logger.info(
                "Returned Zarr decision for %s: %s (%s)",
                root,
                decision.outcome,
                decision.reason,
            )
            if decision.unchanged_passthrough:
                continue
            if not decision.eligible:
                prepared.extend(_full_result_items(
                    root, operation, item.registration
                ))
                continue
            normalized = normalize_returned_zarr(
                decision,
                operation.canonical_inputs.workflow_id,
            )
            self.logger.info(
                "Stored shallow Zarr %s with %s image node(s)",
                root,
                len(normalized.collection.images),
            )
            prepared.extend(_items_for_shallow_collection(
                root, normalized.collection, operation
            ))
        return tuple(prepared), tuple(decisions)


__all__ = [
    "ImportLifecycleEngine",
    "PreparedImportItem",
    "PreparedImportPlan",
]
