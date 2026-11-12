"""Registration-independent lifecycle operations for importer orders."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import errno
import hashlib
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
    ShallowManifest,
    ZarrImportOptions,
)

from .pixel_identity import PixelIdentityError
from .canonical_store import cache_refreshed_inputs
from biomero_schema.shallower import (
    SHALLOW_OPERATION_REPORT,
    RemoteShallowReceipt,
    ShallowOperationReport,
)
from pydantic import ValidationError
from biomero_shallower.operations import locked, validate_report
from biomero_shallower import __version__ as shallower_version
from biomero_shallower.transaction import recover, write_json
from .result_zarr import (
    ReturnedZarrDecision,
    discover_ngff_nodes,
    evaluate_returned_zarr,
    normalize_returned_zarr,
    remove_transfer_input_marker,
    load_managed_storage_roots,
)

# Environment settings of the shallow-Zarr lifecycle.
ENV_SHALLOW_ZARR = "BIOMERO_SHALLOW_ZARR"
ENV_SHALLOW_WORKERS = "BIOMERO_SHALLOW_ZARR_WORKERS"
ENV_REMOTE_SHALLOW_ZARR = "BIOMERO_REMOTE_SHALLOW_ZARR"
DEFAULT_SHALLOW_WORKERS = 1

# Provenance and fallback files written next to a result store.
IMPORT_STORAGE_FILENAME = ".biomero-import-storage.json"
REMOTE_ATTEMPT_FILENAME = ".biomero-remote-attempt.json"
IMPORT_STORAGE_SCHEMA_VERSION = 1

# Values recorded in the import storage file.
STORAGE_SHALLOW = "shallow-zarr"
STORAGE_FULL = "full-zarr"
LOCATION_REMOTE = "remote"
LOCATION_IMPORTER = "importer"

# Result of a remote helper report, and outcome of a local evaluation.
REPORT_NORMALIZED = "normalized"
REPORT_KEPT_FULL = "kept-full"
DECISION_KEEP_FULL = "keep-full"


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


def _require_no_pending_prune(root: Path):
    if next(root.parent.glob(f".{root.name}.biomero-prune-*"), None) is not None:
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
                "Filesystem locking is unsupported for %s; using legacy import "
                "preparation without recovery or concurrency protection", root)
            yield
            return
        # Restore an interrupted prune before inspecting any metadata.
        # Keep the lock through normalization and registration planning.
        recover(root)
        yield


def _load_shallow_manifest(path: Path) -> ShallowManifest | None:
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


def _common_plate_label(
    manifest: ShallowManifest,
    requested: str | None,
) -> str | None:
    labels_per_image = []
    for image in manifest.collection.images:
        if manifest.image_binding(image.node_id).source.source_object_type != "Plate":
            return None
        prefix = PurePosixPath(image.node_path) / "labels"
        names = set()
        paths = (
            label.node_path
            for label in manifest.collection.labels
            if label.source_image_id == image.node_id
        )
        for path in paths:
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


def _items_for_shallow_manifest(
    root: Path,
    manifest: ShallowManifest,
    operation: ShallowZarrImportOperation,
) -> tuple[PreparedImportItem, ...]:
    source_types = {
        binding.source.source_object_type for binding in manifest.bindings.images
    }
    if source_types == {"Plate"}:
        items = [PreparedImportItem(
            path=root,
            registration=ZarrImportOptions(),
            role="primary",
        )]
        if operation.import_plate_label_preview:
            label_name = _common_plate_label(
                manifest,
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
    # The collection root is the user-facing workflow result. Its registered
    # PixelBuffer resolves to the canonical source pixels while the attached
    # shallow reference exposes every result label to label-aware viewers.
    # Label projections remain optional auxiliary objects for workflows that
    # still select an OMERO Image and consume its pixels as a TIFF mask.
    items = [PreparedImportItem(
        path=root, registration=ZarrImportOptions(), role="primary",
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
                path=root.joinpath(*PurePosixPath(path).parts),
                registration=ZarrImportOptions(),
                role="image-label",
            )
            for path in (
                component.logical_node_path for component in local_components
            )
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


def _prune_committed(root: Path) -> bool:
    """Whether a shallow manifest exists and no prune journal is uncommitted."""
    if not (root / SHALLOW_COLLECTION_MANIFEST).exists():
        return False
    for rollback in root.parent.glob(f".{root.name}.biomero-prune-*"):
        journal = rollback / "journal.json"
        if journal.is_file() and not json.loads(
                journal.read_text(encoding="utf-8")).get("committed"):
            return False
    return True


def _sha256_of_file(path: Path) -> str:
    """Return the hex SHA-256 digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _env_enabled(name: str, default: str) -> bool:
    """Whether an environment flag is set to ``true`` (case-insensitive)."""
    return os.getenv(name, default).lower() == "true"


def _write_import_storage(
    root: Path, operation: ShallowZarrImportOperation, *,
    storage: str, location: str, **details,
) -> None:
    """Record how a result was stored, for later imports and troubleshooting."""
    write_json(root / IMPORT_STORAGE_FILENAME, {
        "schema": IMPORT_STORAGE_SCHEMA_VERSION,
        "workflow_id": str(operation.canonical_inputs.workflow_id),
        "storage": storage,
        "location": location,
        **details,
    })


def _write_full_storage(
    root: Path, operation: ShallowZarrImportOperation, reason: str
) -> None:
    """Record that the importer kept the full result, and why."""
    _write_import_storage(
        root, operation, storage=STORAGE_FULL, location=LOCATION_IMPORTER,
        tool_version=shallower_version, reason=reason,
    )


# keep-full reasons that signal a failure rather than an expected comparison.
_KEEP_FULL_FAILURE_PREFIXES = (
    "normalization-failed", "unsupported-", "identity-unavailable",
    "label-identity-unavailable", "invalid-", "untrusted-", "ambiguous-",
    "no-canonical-input-snapshot",
)


def _keep_full_level(reason: str) -> int:
    """WARNING for genuine failures, INFO for expected keep-full outcomes."""
    return (logging.WARNING if reason.startswith(_KEEP_FULL_FAILURE_PREFIXES)
            else logging.INFO)


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
        prepared = []
        decisions = []
        for item in current:
            if not _is_zarr(item.path):
                self.logger.debug(
                    f"Not a Zarr store, skipping shallowing: {item.path}"
                )
                prepared.append(item)
                continue
            with _recover_result_store(item.path):
                store_items, decision = self._prepare_store(
                    item, operation, identity_workers, used_receipts
                )
            prepared.extend(store_items)
            if decision is not None:
                decisions.append(decision)
        expected_receipts = {
            receipt.artifact_path for receipt in operation.remote_receipts
        }
        if used_receipts != expected_receipts:
            raise ValueError("Remote shallow receipt has no matching import file")
        return tuple(prepared), tuple(decisions)

    def _require_shallow_enabled(
        self, operation: ShallowZarrImportOperation
    ) -> None:
        """Reject operations that the administrator has not enabled."""
        if not _env_enabled(ENV_SHALLOW_ZARR, default="false"):
            raise ValueError(
                f"biomero.shallow-zarr requested but {ENV_SHALLOW_ZARR} is disabled"
            )
        if operation.remote_receipts and not _env_enabled(
            ENV_REMOTE_SHALLOW_ZARR, default="true"
        ):
            raise ValueError("Remote shallow receipt consumption is disabled")

    def _prepare_store(
        self,
        item: PreparedImportItem,
        operation: ShallowZarrImportOperation,
        identity_workers: int,
        used_receipts: set[str],
    ) -> tuple[tuple[PreparedImportItem, ...], ReturnedZarrDecision | None]:
        """Prepare one locked Zarr store: remote receipt, reuse or local shallowing.

        Returns:
            The items to register and the local decision, if one was made.
        """
        root = item.path
        existing_manifest = _load_shallow_manifest(root)
        receipt = self._match_receipt(root, operation, used_receipts)
        remote_report_path = root / SHALLOW_OPERATION_REPORT
        if receipt is not None:
            full_remote_items = self._import_remote_result(
                item, receipt, operation
            )
            if full_remote_items is not None:
                return full_remote_items, None
        elif remote_report_path.exists():
            self._set_aside_remote_attempt(root)
        else:
            self.logger.debug(
                f"No remote receipt or report for {root}; using local shallowing"
            )
        if existing_manifest is not None:
            self.logger.debug(f"Reusing existing shallow Zarr manifest for {root}")
            return _items_for_shallow_manifest(
                root, existing_manifest, operation
            ), None
        decision = self._evaluate_locally(root, operation, identity_workers)
        if not decision.eligible:
            _write_full_storage(root, operation, decision.reason)
            return _full_result_items(root, operation, item.registration), decision
        return self._shallow_locally(item, decision, operation), decision

    def _match_receipt(
        self,
        root: Path,
        operation: ShallowZarrImportOperation,
        used_receipts: set[str],
    ) -> RemoteShallowReceipt | None:
        """Find the remote receipt of ``root`` and mark it as used.

        A receipt matches by artifact path or, after the user renamed the
        result, by the checksum of the unchanged remote report.

        Raises:
            ValueError: If several receipts match or the receipt was already
                used for another store.
        """
        receipts = [
            receipt for receipt in operation.remote_receipts
            if root.as_posix().endswith("/" + receipt.artifact_path)
        ]
        report_path = root / SHALLOW_OPERATION_REPORT
        renamed_remote_result = (
            not receipts and operation.remote_receipts and report_path.is_file()
        )
        if renamed_remote_result:
            # User result renaming changes the directory name after transfer.
            # Bind to the unchanged trusted report instead of rewriting it.
            report_checksum = _sha256_of_file(report_path)
            receipts = [
                receipt for receipt in operation.remote_receipts
                if receipt.report_sha256 == report_checksum
            ]
        if len(receipts) > 1:
            raise ValueError("Ambiguous remote shallow receipt")
        self.logger.debug(
            f"Matched {len(receipts)} remote receipt(s) for {root} by "
            "artifact path or report checksum"
        )
        if not receipts:
            return None
        matched_receipt = receipts[0]
        if matched_receipt.artifact_path in used_receipts:
            raise ValueError("Remote shallow receipt matches multiple stores")
        used_receipts.add(matched_receipt.artifact_path)
        return matched_receipt

    def _import_remote_result(
        self,
        item: PreparedImportItem,
        receipt: RemoteShallowReceipt,
        operation: ShallowZarrImportOperation,
    ) -> tuple[PreparedImportItem, ...] | None:
        """Validate and record a remotely shallowed result.

        Returns:
            The items of the full result when the helper kept it full, or
            None when a shallow result was accepted and must be registered.
        """
        root = item.path
        report = self._validate_remote_receipt(root, receipt, operation)
        if report.refreshed_inputs is not None:
            cache_refreshed_inputs(
                operation.canonical_inputs, report.refreshed_inputs.inputs,
                load_managed_storage_roots(),
            )
        remote_kept_full = report.result == REPORT_KEPT_FULL
        _write_import_storage(
            root, operation,
            storage=STORAGE_FULL if remote_kept_full else STORAGE_SHALLOW,
            location=LOCATION_REMOTE,
            decision_basis=report.reason,
            container=receipt.image,
            tool_version=receipt.tool_version,
            task_id=str(receipt.task_id),
            job_id=receipt.slurm_job_id,
            report_sha256=receipt.report_sha256,
        )
        if remote_kept_full:
            self.logger.log(
                _keep_full_level(report.reason),
                f"Remote shallowing kept the full result for {root}: "
                f"{report.reason}"
            )
            remove_transfer_input_marker(root)
            return _full_result_items(root, operation, item.registration)
        self.logger.info(
            f"Accepted remote shallow result for {root} ({report.reason})"
        )
        return None

    def _validate_remote_receipt(
        self,
        root: Path,
        receipt: RemoteShallowReceipt,
        operation: ShallowZarrImportOperation,
    ) -> ShallowOperationReport:
        """Check a remote result against its receipt and the installed shallower.

        The trust anchor is the receipt's report checksum plus validation of
        the report and manifest by the installed package; the receipt's image
        and version are logged, not compared with a configured value.

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
        version_mismatch = receipt.tool_version != shallower_version
        if _sha256_of_file(root / SHALLOW_OPERATION_REPORT) != receipt.report_sha256:
            raise ValueError("Remote shallow report checksum mismatch")
        self.logger.debug(
            f"Remote shallow report checksum matches receipt for {root}"
        )
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
        report_matches_receipt = (
            report.result in {REPORT_NORMALIZED, REPORT_KEPT_FULL}
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

    def _set_aside_remote_attempt(self, root: Path) -> None:
        """Handle a remote report that has no receipt.

        Raises:
            ValueError: If the remote helper already pruned the result, since
                it cannot be trusted or restored without its receipt.
        """
        report_path = root / SHALLOW_OPERATION_REPORT
        remote_attempt_report = ShallowOperationReport.from_dict(json.loads(
            report_path.read_text(encoding="utf-8")))
        if remote_attempt_report.result == REPORT_NORMALIZED:
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
            if decision.outcome == DECISION_KEEP_FULL else logging.INFO
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
            if not _prune_committed(root):
                return self._keep_full_after_failed_shallowing(
                    item, operation, exc
                )
            # Failed after the commit point (e.g. cleanup): the store is a
            # valid shallow result if its manifest loads.
            self.logger.warning(
                f"Shallowing {root} failed after commit ({exc}); using the "
                "committed shallow result (a leftover prune folder may "
                "remain)", exc_info=True,
            )
            manifest = _load_shallow_manifest(root)
        _write_import_storage(
            root, operation,
            storage=STORAGE_SHALLOW, location=LOCATION_IMPORTER,
            tool_version=shallower_version, decision_basis=decision.reason,
        )
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
        _write_full_storage(root, operation, f"normalization-failed: {failure}")
        return _full_result_items(root, operation, item.registration)


__all__ = [
    "ImportLifecycleEngine",
    "PreparedImportItem",
    "PreparedImportPlan",
]
