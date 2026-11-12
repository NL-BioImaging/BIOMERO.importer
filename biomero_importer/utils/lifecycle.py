"""Registration-independent lifecycle operations for importer orders."""

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import errno
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Literal, Sequence

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
from biomero_shallower.operations import (
    NORMALIZATION_FAILED,
    RESULT_KEPT_FULL,
    RESULT_NORMALIZED,
    locked,
    validate_report,
)
from biomero_shallower.transaction import (
    prune_committed,
    recover,
    rollback_folders,
    write_json,
)

from .canonical_store import cache_refreshed_inputs
from .pixel_identity import PixelIdentityError
from .result_zarr import (
    OUTCOME_KEEP_FULL,
    ReturnedZarrDecision,
    discover_ngff_nodes,
    is_failure_reason,
    evaluate_returned_zarr,
    normalize_returned_zarr,
    remove_transfer_input_marker,
    load_managed_storage_roots,
)
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
    """One physical path and registration view produced by the lifecycle."""

    path: Path
    registration: ZarrImportOptions
    # One of the ROLE_* values below.
    role: Literal[
        "input", "primary", "image-label", "plate-label-preview"
    ]


@dataclass(frozen=True)
class PreparedImportPlan:
    """Deterministic importer inputs after all requested native operations."""

    items: tuple[PreparedImportItem, ...]
    options: ImportOptionsEnvelope
    decisions: tuple[ReturnedZarrDecision, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether any lifecycle operation was requested."""
        return bool(self.options.operations)


@dataclass(frozen=True)
class PreparedStore:
    """The outcome of preparing one Zarr store.

    Attributes:
        items: What to register for the store.
        decision: The local evaluation decision, None when the store was
            decided remotely or reused without evaluation.
    """

    items: tuple[PreparedImportItem, ...]
    decision: ReturnedZarrDecision | None = None


@dataclass(frozen=True)
class ImportStorageRecord:
    """Content of the versioned ``.biomero-import-storage.json`` file.

    Fields that do not apply to a record (for example ``container`` for a
    local result) are left out of the file.
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

    def write(self, root: Path) -> None:
        """Write the record next to the result store ``root``."""
        write_json(root / IMPORT_STORAGE_FILENAME, {
            field: value for field, value in asdict(self).items()
            if value is not None
        })


def _positive_int_env(name: str, default: int, logger: logging.Logger) -> int:
    """Read a positive integer setting, falling back to ``default`` if invalid."""
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
    """Whether ``path`` is a Zarr store, by suffix or Zarr v2/v3 metadata."""
    return path.is_dir() and (
        path.name.lower().endswith(ZARR_DIRECTORY_SUFFIX)
        or (path / ZARR_V2_ATTRIBUTES_FILENAME).is_file()
        or (path / ZARR_V3_METADATA_FILENAME).is_file()
    )


def _require_no_pending_prune(root: Path) -> None:
    """Refuse to continue without locking while a prune is still pending."""
    if next(iter(rollback_folders(root)), None) is not None:
        raise ValueError(
            "Interrupted shallow transaction needs recovery on storage "
            "supporting locks before retrying")


@contextmanager
def _recover_result_store(root: Path):
    """Recover automatically, using legacy preparation if locks are unsupported."""
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
    """Load the shallow manifest of a store, or None if it has none.

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
    """Load the manifest of a committed prune, which must exist.

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
    """Pick the label every plate image has, to show as the plate preview.

    Args:
        manifest: The shallow manifest of the result.
        requested: The label asked for in the order, if any.

    Returns:
        The requested label if every image has it, otherwise the only label
        shared by all images, or None when there is no unambiguous choice or
        a source is not a plate.
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
    """Plan the registration items of a shallow result.

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
    """Register the plate, plus a label preview view if one can be chosen."""
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
    """Register the collection root, plus label views if requested."""
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
    """Plan the registration of an unpruned result and its label views."""
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
    """Whether a prune of ``root`` committed, so its manifest is the result.

    A manifest next to an uncommitted (or unreadable) prune journal means the
    prune was interrupted before the commit point and must be rolled back.
    """
    try:
        return prune_committed(root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            f"Cannot read the prune journal of {root} ({exc}); treating the "
            "prune as uncommitted"
        )
        return False


def _sha256_of_file(path: Path) -> str:
    """Return the hex SHA-256 digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_full_storage(
    root: Path, operation: ShallowZarrImportOperation, reason: str
) -> None:
    """Record that the importer kept the full result, and why."""
    ImportStorageRecord(
        workflow_id=str(operation.canonical_inputs.workflow_id),
        storage=STORAGE_FULL,
        location=LOCATION_IMPORTER,
        tool_version=shallower_version,
        reason=reason,
    ).write(root)


def _keep_full_level(reason: str) -> int:
    """WARNING for genuine failures, INFO for expected keep-full outcomes."""
    return logging.WARNING if is_failure_reason(reason) else logging.INFO


class ImportLifecycleEngine:
    """Execute optional native operations without knowing register.py."""

    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger(__name__)

    def prepare(
        self,
        files: Sequence[str | Path],
        options: ImportOptionsEnvelope | dict | None,
    ) -> PreparedImportPlan:
        """Apply the operations requested in ``options`` to the files.

        Args:
            files: Paths of the files or stores of the order.
            options: Import options, as an envelope or its dict form.

        Returns:
            The items to register, the options and the local decisions.

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
        """Shallow every Zarr store in ``current`` or keep its full result.

        Args:
            current: Items produced by the previous lifecycle step.
            operation: The requested shallow-Zarr operation, including any
                receipts of results that were shallowed remotely.

        Returns:
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
        """Reject operations that the administrator has not enabled."""
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
        """Prepare one locked Zarr store: remote result, reuse or local shallowing.

        This may write provenance files and prune the store.

        Args:
            item: The store to prepare.
            receipt: The store's remote receipt, None if it was not shallowed
                remotely.
            operation: The requested shallow-Zarr operation.
            identity_workers: Parallelism of the local identity evaluation.
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
        """Find the remote receipt of ``root``.

        A receipt matches by artifact path or, after the user renamed the
        result, by the checksum of the unchanged remote report.

        Returns:
            The matching receipt, or None if ``root`` has none.

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
        """Validate a remotely shallowed result and record how it was stored.

        Returns:
            The validated remote report; its ``result`` says whether the
            helper kept the full result or pruned it.
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
        """Check a remote result against its receipt and the installed shallower.

        The receipt arrives in the same import options as the checksum it
        carries, so the checksum is not independent of the receipt: trust
        relies on BIOMERO orchestrating the import options. The receipt's
        image and version are logged, not compared with a configured value.

        Raises:
            ValueError: If the checksum, report, manifest or task provenance
                does not match, or the installed package cannot parse the
                result of another shallower version.
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
        """Parse and validate the remote report and manifest.

        A different shallower version only produces a warning (after the
        installed package accepted the result); the error of a result the
        installed package cannot parse names both versions.

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
                f"Remote shallow result from biomero-shallower "
                f"{receipt.tool_version} (image {receipt.image}) is not "
                f"compatible with installed {shallower_version}: {exc}. "
                "Install the matching biomero-shallower in the importer "
                "or rerun the workflow with matching versions") from exc
        if version_mismatch:
            self.logger.warning(
                f"Remote shallower version {receipt.tool_version} "
                f"(image {receipt.image}) differs from installed "
                f"version {shallower_version}; {root} validated "
                "with the installed package"
            )
        return report

    def _set_aside_remote_attempt(self, root: Path) -> None:
        """Handle a remote report that has no receipt.

        Raises:
            ValueError: If the remote helper already pruned the result, since
                it cannot be trusted or restored without its receipt.
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
        """Compare the returned store with its canonical inputs."""
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
        """Prune a verified duplicate result, keeping the full result on failure."""
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
        """Roll back a failed prune and import the full result instead."""
        root = item.path
        # Idempotent rollback; fails loudly if incomplete.
        recover(root)
        self.logger.warning(
            f"Shallowing {root} failed ({failure}); rolled back and "
            "importing the full result", exc_info=True,
        )
        _write_full_storage(root, operation, f"{NORMALIZATION_FAILED}: {failure}")
        return _full_result_items(root, operation, item.registration)


__all__ = [
    "ImportLifecycleEngine",
    "PreparedImportItem",
    "PreparedImportPlan",
]
