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
"""Registration-independent lifecycle operations for importer orders."""

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import errno
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Iterator, Literal, Sequence

from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from biomero_schema.imports import (
    SHALLOW_ZARR_OPERATION,
    ImportOptionsEnvelope,
    ShallowZarrImportOperation,
    parse_import_options,
)
from biomero_schema.shallower import (
    SHALLOW_OPERATION_REPORT,
    RemoteShallowReceipt,
    ShallowOperationReport,
)
from biomero_schema.zarr import (
    SHALLOW_COLLECTION_MANIFEST,
    ShallowManifest,
    ZarrImportOptions,
)
from biomero_shallower import __version__ as shallower_version
from biomero_shallower.discovery import discover_ngff_nodes
from biomero_shallower.evaluation import (
    OUTCOME_KEEP_FULL,
    REASON_NORMALIZATION_FAILED,
    ReturnedZarrDecision,
    evaluate_returned_zarr,
    is_failure_reason,
    remove_transfer_input_marker,
)
from biomero_shallower.normalization import normalize_returned_zarr
from biomero_shallower.operations import (
    RESULT_KEPT_FULL,
    RESULT_NORMALIZED,
    locked,
    validate_report,
)
from biomero_shallower.registration import load_managed_storage_roots
from biomero_shallower.transaction import (
    prune_was_committed,
    recover,
    rollback_folders,
    write_json,
)

from .canonical_store import cache_refreshed_inputs
from .pixel_identity import PixelIdentityError
from .shallow_settings import (
    DEFAULT_REMOTE_SHALLOW_ZARR,
    DEFAULT_SHALLOW_WORKERS,
    DEFAULT_SHALLOW_ZARR,
    ENV_REMOTE_SHALLOW_ZARR,
    ENV_SHALLOW_WORKERS,
    ENV_SHALLOW_ZARR,
    env_flag_enabled,
)

# Provenance and fallback files written next to a result store.
IMPORT_STORAGE_FILENAME = ".biomero-import-storage.json"
REMOTE_ATTEMPT_FILENAME = ".biomero-remote-attempt.json"
IMPORT_STORAGE_SCHEMA_VERSION = 1

# Values recorded in the import storage file.
STORAGE_SHALLOW = "shallow-zarr"
STORAGE_FULL = "full-zarr"
LOCATION_REMOTE = "remote"
LOCATION_IMPORTER = "importer"

# Zarr store layout and NGFF node roles.
ZARR_DIRECTORY_SUFFIX = ".zarr"
ZARR_V2_ATTRIBUTES_FILENAME = ".zattrs"
ZARR_V3_METADATA_FILENAME = "zarr.json"
LABELS_DIRECTORY = "labels"
ROOT_NODE_PATH = "."
NODE_ROLE_IMAGE = "image"
NODE_ROLE_LABEL = "label"

# Source object types of a shallow collection and plate registration views.
SOURCE_TYPE_PLATE = "Plate"
SOURCE_TYPE_IMAGE = "Image"
PLATE_PIXEL_SOURCE_LABEL = "label"

# Roles of the items the lifecycle hands to registration.
ROLE_INPUT = "input"
ROLE_PRIMARY = "primary"
ROLE_IMAGE_LABEL = "image-label"
ROLE_PLATE_LABEL_PREVIEW = "plate-label-preview"


@dataclass(frozen=True)
class PreparedImportItem:
    """
    One physical path and registration view produced by the lifecycle.

    Attributes:
        path (Path): Where the item lives on disk.
        registration (ZarrImportOptions): How the item is registered.
        role (str): One of the ``ROLE_*`` values defined above.
    """

    path: Path
    registration: ZarrImportOptions
    # One of the ROLE_* values below.
    role: Literal[
        "input", "primary", "image-label", "plate-label-preview"
    ]


@dataclass(frozen=True)
class PreparedImportPlan:
    """
    Deterministic importer inputs after all requested native operations.

    Attributes:
        items (tuple[PreparedImportItem, ...]): What to register.
        options (ImportOptionsEnvelope): The options of the import order.
        decisions (tuple[ReturnedZarrDecision, ...]): Local evaluation
            decisions, one per locally evaluated store.
    """

    items: tuple[PreparedImportItem, ...]
    options: ImportOptionsEnvelope
    decisions: tuple[ReturnedZarrDecision, ...] = ()

    @property
    def changed(self) -> bool:
        """
        Whether any lifecycle operation was requested.

        Returns:
            bool: True if the import options contain at least one operation.
        """
        return bool(self.options.operations)


@dataclass(frozen=True)
class PreparedStore:
    """
    The outcome of preparing one Zarr store.

    Attributes:
        items (tuple[PreparedImportItem, ...]): What to register for the
            store.
        decision (ReturnedZarrDecision | None): The local evaluation
            decision, None when the store was decided remotely or reused
            without evaluation.
    """

    items: tuple[PreparedImportItem, ...]
    decision: ReturnedZarrDecision | None = None


# Hint shared by the errors of a result the installed shallower cannot parse.
VERSION_MISMATCH_HINT = (
    "Install the matching biomero-shallower in the importer "
    "or rerun the workflow with matching versions"
)

# Persisted keys, in order, of the three kinds of import storage records.
_RECORD_HEAD_FIELDS = ("schema", "workflow_id", "storage", "location")
_REMOTE_RECORD_FIELDS = _RECORD_HEAD_FIELDS + (
    "decision_basis", "container", "tool_version", "task_id", "job_id",
    "report_sha256",
)
_IMPORTER_FULL_RECORD_FIELDS = _RECORD_HEAD_FIELDS + ("tool_version", "reason")
_IMPORTER_SHALLOW_RECORD_FIELDS = _RECORD_HEAD_FIELDS + (
    "tool_version", "decision_basis",
)


@dataclass(frozen=True)
class ImportStorageRecord:
    """
    Content of the versioned ``.biomero-import-storage.json`` file.

    The persisted format is read by other BIOMERO components, so each kind of
    record keeps exactly its field set and key order; a field that applies but
    has no value (a missing Slurm job id) is written as ``null``.

    Attributes:
        workflow_id (str): The workflow the result belongs to.
        storage (str): ``STORAGE_SHALLOW`` or ``STORAGE_FULL``.
        location (str): ``LOCATION_REMOTE`` or ``LOCATION_IMPORTER``.
        tool_version (str | None): Shallower version that handled the result.
        decision_basis (str | None): Why the result was shallowed.
        reason (str | None): Why the full result was kept.
        container (str | None): Remote helper image.
        task_id (str | None): Remote helper task.
        job_id (str | None): Slurm job of the remote helper.
        report_sha256 (str | None): Checksum of the remote report.
        schema (int): Version of this record format.
    """

    workflow_id: str
    storage: str
    location: str
    tool_version: str | None = None
    decision_basis: str | None = None
    reason: str | None = None
    container: str | None = None
    task_id: str | None = None
    job_id: str | None = None
    report_sha256: str | None = None
    schema: int = IMPORT_STORAGE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        """
        Serialize with the field set and key order of this kind of record.

        Returns:
            dict: The record as persisted, in key order.
        """
        if self.location == LOCATION_REMOTE:
            field_names = _REMOTE_RECORD_FIELDS
        elif self.storage == STORAGE_FULL:
            field_names = _IMPORTER_FULL_RECORD_FIELDS
        else:
            field_names = _IMPORTER_SHALLOW_RECORD_FIELDS
        values = asdict(self)
        return {name: values[name] for name in field_names}

    def write(self, root: Path) -> None:
        """
        Write the record next to the result store.

        Args:
            root (Path): The result store the record describes.
        """
        write_json(root / IMPORT_STORAGE_FILENAME, self.to_dict())


def _positive_int_env(name: str, default: int, logger: logging.Logger) -> int:
    """
    Read a positive integer setting, falling back to a default if invalid.

    Args:
        name (str): Environment variable to read.
        default (int): Value used when the variable is unset or invalid.
        logger (logging.Logger): Logger for the invalid-value warning.

    Returns:
        int: The parsed value, or ``default``.
    """
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
        if parsed < 1:
            raise ValueError
        return parsed
    except ValueError:
        logger.warning(f"Ignoring invalid {name}={value!r}; using {default}")
        return default


def _is_zarr(path: Path) -> bool:
    """
    Check whether a path is a Zarr store, by suffix or Zarr v2/v3 metadata.

    Args:
        path (Path): The path to check.

    Returns:
        bool: True if the path is a directory that looks like a Zarr store.
    """
    return path.is_dir() and (
        path.name.lower().endswith(ZARR_DIRECTORY_SUFFIX)
        or (path / ZARR_V2_ATTRIBUTES_FILENAME).is_file()
        or (path / ZARR_V3_METADATA_FILENAME).is_file()
    )


def _require_no_pending_prune(root: Path) -> None:
    """
    Refuse to continue without locking while a prune is still pending.

    Args:
        root (Path): The result store.

    Raises:
        ValueError: If an interrupted prune has left a rollback folder.
    """
    if next(iter(rollback_folders(root)), None) is not None:
        raise ValueError(
            "Interrupted shallow transaction needs recovery on storage "
            "supporting locks before retrying")


@contextmanager
def _recover_result_store(root: Path) -> Iterator[None]:
    """
    Recover an interrupted prune of a store while holding its lock.

    Falls back to legacy preparation, without recovery or concurrency
    protection, when the filesystem does not support locks.

    Args:
        root (Path): The result store to lock and recover.

    Yields:
        None: While the lock (if any) is held.

    Raises:
        ValueError: If the store is a symlink, or a prune is pending on a
            filesystem without lock support.
        OSError: If locking fails for a reason other than missing support.
    """
    if root.is_symlink():
        raise ValueError("Cannot normalize a symlinked Zarr store")
    with ExitStack() as stack:
        try:
            stack.enter_context(locked(root))
        except OSError as exc:
            if exc.errno not in (errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            _require_no_pending_prune(root)
            logging.getLogger(__name__).warning(
                f"Filesystem locking is unsupported for {root}; using legacy "
                "import preparation without recovery or concurrency protection")
            yield
            return
        # Restore an interrupted prune before inspecting any metadata.
        # Keep the lock through normalization and registration planning.
        recover(root)
        yield


def _load_shallow_manifest(path: Path) -> ShallowManifest | None:
    """
    Load the shallow manifest of a store, or None if it has none.

    Args:
        path (Path): The result store.

    Returns:
        ShallowManifest | None: The parsed manifest, or None if absent.

    Raises:
        PixelIdentityError: If the manifest exists but cannot be parsed.
    """
    manifest = path / SHALLOW_COLLECTION_MANIFEST
    if not manifest.is_file():
        return None
    try:
        return ShallowManifest.from_dict(json.loads(
            manifest.read_text(encoding="utf-8")
        ))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PixelIdentityError(
            f"Invalid existing shallow collection: {manifest}"
        ) from exc


def _load_committed_manifest(root: Path) -> ShallowManifest:
    """
    Load the manifest of a committed prune, which must exist.

    Args:
        root (Path): The result store.

    Returns:
        ShallowManifest: The parsed manifest.

    Raises:
        PixelIdentityError: If the manifest is missing or invalid.
    """
    committed_manifest = _load_shallow_manifest(root)
    if committed_manifest is None:
        raise PixelIdentityError(
            f"Committed shallow result of {root} has no manifest"
        )
    return committed_manifest


def _common_plate_label(
    manifest: ShallowManifest,
    requested: str | None,
) -> str | None:
    """
    Pick the label every plate image has, to show as the plate preview.

    Args:
        manifest (ShallowManifest): The shallow manifest of the result.
        requested (str | None): The label asked for in the order, if any.

    Returns:
        str | None: The requested label if every image has it, otherwise the
        only label shared by all images; None when there is no unambiguous
        choice or a source is not a plate.
    """
    label_names_per_image = []
    for image in manifest.collection.images:
        source_type = manifest.image_binding(image.node_id).source.source_object_type
        if source_type != SOURCE_TYPE_PLATE:
            return None
        image_labels_path = PurePosixPath(image.node_path) / LABELS_DIRECTORY
        label_names = set()
        label_paths = (
            label.node_path
            for label in manifest.collection.labels
            if label.source_image_id == image.node_id
        )
        for label_path in label_paths:
            try:
                relative_label_path = PurePosixPath(label_path).relative_to(
                    image_labels_path
                )
            except ValueError:
                continue
            if len(relative_label_path.parts) == 1:
                label_names.add(relative_label_path.name)
        label_names_per_image.append(label_names)
    names_in_every_image = (
        set.intersection(*label_names_per_image) if label_names_per_image else set()
    )
    if requested:
        return requested if requested in names_in_every_image else None
    only_shared_label = len(names_in_every_image) == 1
    return next(iter(names_in_every_image)) if only_shared_label else None


def _items_for_shallow_manifest(
    root: Path,
    manifest: ShallowManifest,
    operation: ShallowZarrImportOperation,
) -> tuple[PreparedImportItem, ...]:
    """
    Plan the registration items of a shallow result.

    Args:
        root (Path): The result store.
        manifest (ShallowManifest): The shallow manifest of the result.
        operation (ShallowZarrImportOperation): The requested operation.

    Returns:
        tuple[PreparedImportItem, ...]: The items to register.

    Raises:
        PixelIdentityError: If the collection mixes Image and Plate sources.
    """
    source_types = {
        binding.source.source_object_type for binding in manifest.bindings.images
    }
    if source_types == {SOURCE_TYPE_PLATE}:
        return _plate_items(root, manifest, operation)
    if source_types == {SOURCE_TYPE_IMAGE}:
        return _image_items(root, manifest, operation)
    raise PixelIdentityError(
        "Shallow collection must contain only Image or only Plate sources"
    )


def _plate_items(
    root: Path,
    manifest: ShallowManifest,
    operation: ShallowZarrImportOperation,
) -> tuple[PreparedImportItem, ...]:
    """
    Plan the registration of a plate, plus a label preview view if requested.

    Args:
        root (Path): The result store.
        manifest (ShallowManifest): The shallow manifest of the result.
        operation (ShallowZarrImportOperation): The requested operation.

    Returns:
        tuple[PreparedImportItem, ...]: The plate and optional preview items.
    """
    items = [PreparedImportItem(
        path=root,
        registration=ZarrImportOptions(),
        role=ROLE_PRIMARY,
    )]
    if not operation.import_plate_label_preview:
        return tuple(items)
    label_name = _common_plate_label(manifest, operation.plate_label_name)
    if label_name is not None:
        items.append(PreparedImportItem(
            path=root,
            registration=ZarrImportOptions(
                platePixelSource=PLATE_PIXEL_SOURCE_LABEL,
                plateLabelName=label_name,
            ),
            role=ROLE_PLATE_LABEL_PREVIEW,
        ))
    return tuple(items)


def _image_items(
    root: Path,
    manifest: ShallowManifest,
    operation: ShallowZarrImportOperation,
) -> tuple[PreparedImportItem, ...]:
    """
    Plan the registration of the collection root, plus label views if requested.

    Args:
        root (Path): The result store.
        manifest (ShallowManifest): The shallow manifest of the result.
        operation (ShallowZarrImportOperation): The requested operation.

    Returns:
        tuple[PreparedImportItem, ...]: The root and optional label items.
    """
    # The collection root is the user-facing workflow result. Its registered
    # PixelBuffer resolves to the canonical source pixels while the attached
    # shallow reference exposes every result label to label-aware viewers.
    # Label projections remain optional auxiliary objects for workflows that
    # still select an OMERO Image and consume its pixels as a TIFF mask.
    items = [PreparedImportItem(
        path=root, registration=ZarrImportOptions(), role=ROLE_PRIMARY,
    )]
    if not manifest.collection.labels or not operation.import_image_label_views:
        return tuple(items)
    for image in manifest.collection.images:
        local_components = (
            binding.component
            for binding in manifest.label_bindings_for_image(image.node_id)
            if binding.component.source is None
        )
        items.extend(
            PreparedImportItem(
                path=root.joinpath(*PurePosixPath(label_path).parts),
                registration=ZarrImportOptions(),
                role=ROLE_IMAGE_LABEL,
            )
            for label_path in (
                component.logical_node_path for component in local_components
            )
        )
    return tuple(items)


def _full_result_items(
    root: Path,
    operation: ShallowZarrImportOperation,
    registration: ZarrImportOptions,
) -> tuple[PreparedImportItem, ...]:
    """
    Plan the registration of an unpruned result and its label views.

    Args:
        root (Path): The result store.
        operation (ShallowZarrImportOperation): The requested operation.
        registration (ZarrImportOptions): Registration options of the result.

    Returns:
        tuple[PreparedImportItem, ...]: The result and optional label items.
    """
    items = [PreparedImportItem(root, registration, ROLE_PRIMARY)]
    if not operation.import_image_label_views:
        return tuple(items)
    try:
        nodes = discover_ngff_nodes(root)
    except PixelIdentityError:
        return tuple(items)
    image_nodes = tuple(node for node in nodes if node.role == NODE_ROLE_IMAGE)
    single_root_image = (
        len(image_nodes) == 1 and image_nodes[0].node_path == ROOT_NODE_PATH
    )
    if not single_root_image:
        return tuple(items)
    items.extend(
        PreparedImportItem(
            root.joinpath(*PurePosixPath(node.node_path).parts),
            ZarrImportOptions(),
            ROLE_IMAGE_LABEL,
        )
        for node in nodes
        if node.role == NODE_ROLE_LABEL
    )
    return tuple(items)


def _prune_committed(root: Path, logger: logging.Logger) -> bool:
    """
    Check whether a prune of a store committed, so its manifest is the result.

    A manifest next to an uncommitted (or unreadable) prune journal means the
    prune was interrupted before the commit point and must be rolled back.

    Args:
        root (Path): The result store.
        logger (logging.Logger): Logger for an unreadable journal.

    Returns:
        bool: True if the prune committed.
    """
    try:
        return prune_was_committed(root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            f"Cannot read the prune journal of {root} ({exc}); treating the "
            "prune as uncommitted"
        )
        return False


def _sha256_of_file(path: Path) -> str:
    """
    Return the hex SHA-256 digest of a file's bytes.

    Args:
        path (Path): The file to hash.

    Returns:
        str: The hex digest.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_full_storage(
    root: Path, operation: ShallowZarrImportOperation, reason: str
) -> None:
    """
    Record that the importer kept the full result, and why.

    Args:
        root (Path): The result store.
        operation (ShallowZarrImportOperation): The requested operation.
        reason (str): Why the full result was kept.
    """
    ImportStorageRecord(
        workflow_id=str(operation.canonical_inputs.workflow_id),
        storage=STORAGE_FULL,
        location=LOCATION_IMPORTER,
        tool_version=shallower_version,
        reason=reason,
    ).write(root)


def _keep_full_level(reason: str) -> int:
    """
    Choose the log level of a keep-full reason.

    Args:
        reason (str): The keep-full reason.

    Returns:
        int: WARNING for genuine failures, INFO for expected outcomes.
    """
    return logging.WARNING if is_failure_reason(reason) else logging.INFO


class ImportLifecycleEngine:
    """
    Execute optional native operations without knowing register.py.
    """

    def __init__(self, logger: logging.Logger | None = None):
        """
        Create the engine.

        Args:
            logger (logging.Logger, optional): Logger for the lifecycle
                decisions. Defaults to this module's logger.
        """
        self.logger = logger or logging.getLogger(__name__)

    def prepare(
        self,
        files: Sequence[str | Path],
        options: ImportOptionsEnvelope | dict | None,
    ) -> PreparedImportPlan:
        """
        Apply the operations requested in the import options to the files.

        Args:
            files (Sequence[str | Path]): Paths of the files or stores of the
                order.
            options (ImportOptionsEnvelope | dict | None): Import options, as an
                envelope or its dict form.

        Returns:
            PreparedImportPlan: The items to register, the options and the local
            decisions.

        Raises:
            ValueError: If an operation is unsupported or cannot be applied.
        """
        envelope = parse_import_options(options)
        items = tuple(
            PreparedImportItem(Path(path), envelope.registration, ROLE_INPUT)
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
        """
        Shallow every Zarr store in the items or keep its full result.

        Args:
            current (tuple[PreparedImportItem, ...]): Items produced by the
                previous lifecycle step.
            operation (ShallowZarrImportOperation): The requested shallow-Zarr
                operation, including any receipts of results that were shallowed
                remotely.

        Returns:
            tuple[tuple[PreparedImportItem, ...], tuple[ReturnedZarrDecision, ...]]:
            The items to register and the local evaluation decisions.

        Raises:
            ValueError: If shallowing is disabled or a receipt cannot be used.
        """
        self._require_shallow_enabled(operation)
        identity_workers = _positive_int_env(
            ENV_SHALLOW_WORKERS, DEFAULT_SHALLOW_WORKERS, self.logger
        )
        self.logger.debug(
            f"Preparing shallow Zarr import: {len(current)} item(s), "
            f"{len(operation.remote_receipts)} remote receipt(s), "
            f"{identity_workers} identity worker(s)"
        )
        used_receipts = set()
        prepared_items = []
        decisions = []
        for item in current:
            if not _is_zarr(item.path):
                self.logger.debug(
                    f"Not a Zarr store, skipping shallowing: {item.path}"
                )
                prepared_items.append(item)
                continue
            with _recover_result_store(item.path):
                receipt = self._match_receipt(item.path, operation)
                if receipt is not None:
                    if receipt.artifact_path in used_receipts:
                        raise ValueError(
                            "Remote shallow receipt matches multiple stores"
                        )
                    used_receipts.add(receipt.artifact_path)
                prepared_store = self._prepare_store(
                    item, receipt, operation, identity_workers
                )
            prepared_items.extend(prepared_store.items)
            if prepared_store.decision is not None:
                decisions.append(prepared_store.decision)
        expected_receipts = {
            receipt.artifact_path for receipt in operation.remote_receipts
        }
        if used_receipts != expected_receipts:
            raise ValueError("Remote shallow receipt has no matching import file")
        return tuple(prepared_items), tuple(decisions)

    def _require_shallow_enabled(
        self, operation: ShallowZarrImportOperation
    ) -> None:
        """
        Reject operations that the administrator has not enabled.

        Args:
            operation (ShallowZarrImportOperation): The requested operation.

        Raises:
            ValueError: If shallowing, or remote receipt consumption for an
                operation with receipts, is disabled.
        """
        if not env_flag_enabled(ENV_SHALLOW_ZARR, DEFAULT_SHALLOW_ZARR):
            raise ValueError(
                f"biomero.shallow-zarr requested but {ENV_SHALLOW_ZARR} is disabled"
            )
        if operation.remote_receipts and not env_flag_enabled(
            ENV_REMOTE_SHALLOW_ZARR, DEFAULT_REMOTE_SHALLOW_ZARR
        ):
            raise ValueError("Remote shallow receipt consumption is disabled")

    def _prepare_store(
        self,
        item: PreparedImportItem,
        receipt: RemoteShallowReceipt | None,
        operation: ShallowZarrImportOperation,
        identity_workers: int,
    ) -> PreparedStore:
        """
        Prepare one locked Zarr store: remote result, reuse or local shallowing.

        This may write provenance files and prune the store.

        Args:
            item (PreparedImportItem): The store to prepare.
            receipt (RemoteShallowReceipt | None): The store's remote receipt,
                None if it was not shallowed remotely.
            operation (ShallowZarrImportOperation): The requested operation.
            identity_workers (int): Parallelism of the local identity evaluation.

        Returns:
            PreparedStore: The items to register and the local decision, if any.
        """
        root = item.path
        existing_manifest = _load_shallow_manifest(root)
        if receipt is not None:
            remote_report = self._accept_remote_result(root, receipt, operation)
            if remote_report.result == RESULT_KEPT_FULL:
                return PreparedStore(
                    _full_result_items(root, operation, item.registration)
                )
        elif (root / SHALLOW_OPERATION_REPORT).exists():
            self._set_aside_remote_attempt(root)
        else:
            self.logger.debug(
                f"No remote receipt or report for {root}; using local shallowing"
            )
        if existing_manifest is not None:
            self.logger.debug(f"Reusing existing shallow Zarr manifest for {root}")
            return PreparedStore(
                _items_for_shallow_manifest(root, existing_manifest, operation)
            )
        decision = self._evaluate_locally(root, operation, identity_workers)
        if not decision.eligible:
            _write_full_storage(root, operation, decision.reason)
            return PreparedStore(
                _full_result_items(root, operation, item.registration), decision
            )
        return PreparedStore(
            self._shallow_locally(item, decision, operation), decision
        )

    def _match_receipt(
        self,
        root: Path,
        operation: ShallowZarrImportOperation,
    ) -> RemoteShallowReceipt | None:
        """
        Find the remote receipt of a store.

        A receipt matches by artifact path or, after the user renamed the
        result, by the checksum of the unchanged remote report.

        Args:
            root (Path): The result store.
            operation (ShallowZarrImportOperation): The requested operation,
                holding the remote receipts.

        Returns:
            RemoteShallowReceipt | None: The matching receipt, or None.

        Raises:
            ValueError: If several receipts match.
        """
        matching_receipts = [
            receipt for receipt in operation.remote_receipts
            if root.as_posix().endswith("/" + receipt.artifact_path)
        ]
        report_path = root / SHALLOW_OPERATION_REPORT
        renamed_after_transfer = (
            not matching_receipts
            and bool(operation.remote_receipts)
            and report_path.is_file()
        )
        if renamed_after_transfer:
            # User result renaming changes the directory name after transfer.
            # Bind to the unchanged trusted report instead of rewriting it.
            report_checksum = _sha256_of_file(report_path)
            matching_receipts = [
                receipt for receipt in operation.remote_receipts
                if receipt.report_sha256 == report_checksum
            ]
        if len(matching_receipts) > 1:
            raise ValueError("Ambiguous remote shallow receipt")
        self.logger.debug(
            f"Matched {len(matching_receipts)} remote receipt(s) for {root} by "
            "artifact path or report checksum"
        )
        return matching_receipts[0] if matching_receipts else None

    def _accept_remote_result(
        self,
        root: Path,
        receipt: RemoteShallowReceipt,
        operation: ShallowZarrImportOperation,
    ) -> ShallowOperationReport:
        """
        Validate a remotely shallowed result and record how it was stored.

        Args:
            root (Path): The result store.
            receipt (RemoteShallowReceipt): The receipt of the remote helper.
            operation (ShallowZarrImportOperation): The requested operation.

        Returns:
            ShallowOperationReport: The validated remote report; its ``result``
            says whether the helper kept the full result or pruned it.
        """
        report = self._validate_remote_receipt(root, receipt, operation)
        if report.refreshed_inputs is not None:
            cache_refreshed_inputs(
                operation.canonical_inputs, report.refreshed_inputs.inputs,
                load_managed_storage_roots(),
            )
        remote_kept_full = report.result == RESULT_KEPT_FULL
        ImportStorageRecord(
            workflow_id=str(operation.canonical_inputs.workflow_id),
            storage=STORAGE_FULL if remote_kept_full else STORAGE_SHALLOW,
            location=LOCATION_REMOTE,
            decision_basis=report.reason,
            container=receipt.image,
            tool_version=receipt.tool_version,
            task_id=str(receipt.task_id),
            job_id=receipt.slurm_job_id,
            report_sha256=receipt.report_sha256,
        ).write(root)
        if remote_kept_full:
            self.logger.log(
                _keep_full_level(report.reason),
                f"Remote shallowing kept the full result for {root}: "
                f"{report.reason}"
            )
            remove_transfer_input_marker(root)
        else:
            self.logger.info(
                f"Accepted remote shallow result for {root} ({report.reason})"
            )
        return report

    def _validate_remote_receipt(
        self,
        root: Path,
        receipt: RemoteShallowReceipt,
        operation: ShallowZarrImportOperation,
    ) -> ShallowOperationReport:
        """
        Check a remote result against its receipt and the installed shallower.

        The receipt arrives in the same import options as the checksum it
        carries, so the checksum is not independent of the receipt: trust
        relies on BIOMERO orchestrating the import options. The receipt's
        image and version are logged, not compared with a configured value.

        Args:
            root (Path): The result store.
            receipt (RemoteShallowReceipt): The receipt of the remote helper.
            operation (ShallowZarrImportOperation): The requested operation.

        Returns:
            ShallowOperationReport: The validated remote report.

        Raises:
            ValueError: If the checksum, report, manifest or task provenance
                does not match, or the installed package cannot parse the result
                of another shallower version.
        """
        self.logger.info(
            f"Validating remote shallow receipt for {root} (image "
            f"{receipt.image}, version {receipt.tool_version}, "
            f"task {receipt.task_id})"
        )
        if _sha256_of_file(root / SHALLOW_OPERATION_REPORT) != receipt.report_sha256:
            raise ValueError("Remote shallow report checksum mismatch")
        self.logger.debug(
            f"Remote shallow report checksum matches receipt for {root}"
        )
        report = self._validate_report_with_installed_package(
            root, receipt, operation
        )
        report_matches_receipt = (
            report.result in {RESULT_NORMALIZED, RESULT_KEPT_FULL}
            and report.task_id == receipt.task_id
            and report.slurm_job_id == receipt.slurm_job_id
        )
        if not report_matches_receipt:
            raise ValueError("Remote shallow task provenance mismatch")
        self.logger.debug(
            f"Remote shallow report for {root}: result "
            f"{report.result}, reason {report.reason}, task and "
            "Slurm job match the receipt"
        )
        return report

    def _validate_report_with_installed_package(
        self,
        root: Path,
        receipt: RemoteShallowReceipt,
        operation: ShallowZarrImportOperation,
    ) -> ShallowOperationReport:
        """
        Parse and validate the remote report and manifest.

        A different shallower version only produces a warning (after the
        installed package accepted the result); the error of a result the
        installed package cannot parse names both versions.

        Args:
            root (Path): The result store.
            receipt (RemoteShallowReceipt): The receipt of the remote helper.
            operation (ShallowZarrImportOperation): The requested operation.

        Returns:
            ShallowOperationReport: The validated remote report.

        Raises:
            ValueError: If validation fails; schema/parse failures of another
                version carry a hint to install the matching package.
        """
        version_mismatch = receipt.tool_version != shallower_version
        try:
            report = validate_report(
                root, operation.canonical_inputs,
                image=receipt.image, tool_version=receipt.tool_version,
                artifact=PurePosixPath(receipt.artifact_path).name,
            )
        except (ValidationError, json.JSONDecodeError) as exc:
            # Only schema/parse failures suggest a version problem;
            # integrity failures keep their original message.
            if not version_mismatch:
                raise
            raise ValueError(
                self._unparseable_result_message(receipt, exc)) from exc
        if version_mismatch:
            self.logger.warning(
                f"Remote shallower version {receipt.tool_version} "
                f"(image {receipt.image}) differs from installed "
                f"version {shallower_version}; {root} validated "
                "with the installed package"
            )
        return report

    @staticmethod
    def _unparseable_result_message(
        receipt: RemoteShallowReceipt,
        error: Exception,
        installed_version: str = shallower_version,
    ) -> str:
        """
        Explain why the installed shallower cannot parse a remote result.

        Readers keep accepting older schema numbers, so a result that
        cannot be parsed was most likely written by a newer shallower.

        Args:
            receipt (RemoteShallowReceipt): The receipt of the remote helper.
            error (Exception): The parse or validation error.
            installed_version (str): The installed shallower version.

        Returns:
            str: The error message, with the install hint when the receipt
                version is newer than the installed one.
        """
        try:
            written_by_newer = (
                Version(receipt.tool_version) > Version(installed_version))
        except InvalidVersion:
            written_by_newer = False
        if written_by_newer:
            return (
                f"Remote shallow result was written by a newer "
                f"biomero-shallower ({receipt.tool_version}, image "
                f"{receipt.image}) than the one installed "
                f"({installed_version}): {error}. {VERSION_MISMATCH_HINT}")
        return (
            f"Remote shallow result from biomero-shallower "
            f"{receipt.tool_version} (image {receipt.image}) cannot be "
            f"read by installed {installed_version}: {error}. "
            f"{VERSION_MISMATCH_HINT}")

    def _set_aside_remote_attempt(self, root: Path) -> None:
        """
        Handle a remote report that has no receipt.

        Args:
            root (Path): The result store.

        Raises:
            ValueError: If the remote helper already pruned the result, since it
                cannot be trusted or restored without its receipt.
        """
        report_path = root / SHALLOW_OPERATION_REPORT
        remote_attempt_report = ShallowOperationReport.from_dict(json.loads(
            report_path.read_text(encoding="utf-8")))
        if remote_attempt_report.result == RESULT_NORMALIZED:
            raise ValueError("Remote shallow result is missing its trusted receipt")
        self.logger.warning(
            f"Remote shallowing left {root} with result "
            f"{remote_attempt_report.result} ({remote_attempt_report.reason}) "
            "and no receipt; evaluating locally"
        )
        # Preserve fallback provenance without marking the later local
        # normalization as a remotely completed operation on retry.
        report_path.replace(root / REMOTE_ATTEMPT_FILENAME)

    def _evaluate_locally(
        self,
        root: Path,
        operation: ShallowZarrImportOperation,
        identity_workers: int,
    ) -> ReturnedZarrDecision:
        """
        Compare the returned store with its canonical inputs.

        Args:
            root (Path): The result store.
            operation (ShallowZarrImportOperation): The requested operation.
            identity_workers (int): Parallelism of the identity evaluation.

        Returns:
            ReturnedZarrDecision: Whether the store may be shallowed, and why.
        """
        self.logger.info(
            f"Evaluating returned Zarr {root} with {identity_workers} "
            "identity worker(s)"
        )
        decision = evaluate_returned_zarr(
            root,
            operation.canonical_inputs,
            identity_workers=identity_workers,
            storage_roots=load_managed_storage_roots(),
        )
        if decision.refreshed_inputs:
            cache_refreshed_inputs(
                operation.canonical_inputs, decision.refreshed_inputs,
                load_managed_storage_roots(),
            )
        decision_level = (
            _keep_full_level(decision.reason)
            if decision.outcome == OUTCOME_KEEP_FULL else logging.INFO
        )
        self.logger.log(
            decision_level,
            f"Returned Zarr decision for {root}: {decision.outcome} "
            f"({decision.reason})"
        )
        if remove_transfer_input_marker(root):
            self.logger.info(f"Consumed temporary Zarr input marker for {root}")
        return decision

    def _shallow_locally(
        self,
        item: PreparedImportItem,
        decision: ReturnedZarrDecision,
        operation: ShallowZarrImportOperation,
    ) -> tuple[PreparedImportItem, ...]:
        """
        Prune a verified duplicate result, keeping the full result on failure.

        Args:
            item (PreparedImportItem): The store to prune.
            decision (ReturnedZarrDecision): The eligible local decision.
            operation (ShallowZarrImportOperation): The requested operation.

        Returns:
            tuple[PreparedImportItem, ...]: The items to register.
        """
        root = item.path
        self.logger.info(
            f"Shallowing returned Zarr {root} locally (basis: {decision.reason})"
        )
        try:
            manifest = normalize_returned_zarr(
                decision, operation.canonical_inputs.workflow_id
            ).manifest
        except (PixelIdentityError, OSError, ValueError) as exc:
            # Data (corrupt metadata, identity) and filesystem failures
            # are expected here; anything else is a bug: propagate.
            if not _prune_committed(root, self.logger):
                return self._keep_full_after_failed_shallowing(
                    item, operation, exc
                )
            # Failed after the commit point (e.g. while deleting the rollback
            # folder): the store is a valid shallow result and the next
            # recovery finishes the cleanup.
            self.logger.warning(
                f"Shallowing {root} failed after commit ({exc}); using the "
                "committed shallow result; the next recovery finishes the "
                "cleanup", exc_info=True,
            )
            manifest = _load_committed_manifest(root)
        ImportStorageRecord(
            workflow_id=str(operation.canonical_inputs.workflow_id),
            storage=STORAGE_SHALLOW,
            location=LOCATION_IMPORTER,
            tool_version=shallower_version,
            decision_basis=decision.reason,
        ).write(root)
        self.logger.info(
            f"Stored shallow Zarr {root} with "
            f"{len(manifest.collection.images)} image node(s)"
        )
        return _items_for_shallow_manifest(root, manifest, operation)

    def _keep_full_after_failed_shallowing(
        self,
        item: PreparedImportItem,
        operation: ShallowZarrImportOperation,
        failure: Exception,
    ) -> tuple[PreparedImportItem, ...]:
        """
        Roll back a failed prune and import the full result instead.

        Args:
            item (PreparedImportItem): The store whose prune failed.
            operation (ShallowZarrImportOperation): The requested operation.
            failure (Exception): The error that failed the prune.

        Returns:
            tuple[PreparedImportItem, ...]: The items of the full result.
        """
        root = item.path
        # Idempotent rollback; fails loudly if incomplete.
        recover(root)
        self.logger.warning(
            f"Shallowing {root} failed ({failure}); rolled back and "
            "importing the full result", exc_info=True,
        )
        _write_full_storage(root, operation, f"{REASON_NORMALIZATION_FAILED}: {failure}")
        return _full_result_items(root, operation, item.registration)


__all__ = [
    "ImportLifecycleEngine",
    "PreparedImportItem",
    "PreparedImportPlan",
]
