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
"""Transactional storage primitives for canonical BIOMERO Zarrs."""

from contextlib import ExitStack, contextmanager
import errno
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterator, Mapping, Optional, Union

from biomero_schema.zarr import CanonicalPlateSource, CanonicalZarrSource
from biomero_shallower.operations import locked

logger = logging.getLogger(__name__)


CANONICAL_MARKER_NAME = ".biomero-canonical.json"
CANONICAL_MARKER_SCHEMA = 1
CANONICAL_METADATA_DIRECTORY = ".biomero"
CANONICAL_MARKER_SUFFIX = ".canonical.json"
ENV_PROCESSED_DATA_FOLDER = "PROCESSED_DATA_FOLDER"
DEFAULT_PROCESSED_DATA_FOLDER = ".processed"
# Importers before the kernel lock created ``<name>.ome.zarr.lock`` with
# O_EXCL and a PID; a crash left it behind. It is ignored now (see
# ``creation_lock``). The kernel lock file is named by the shallower's
# ``locked`` as ``.<name>.ome.zarr.biomero-lock``.
LEGACY_CREATION_LOCK_SUFFIX = ".lock"


def read_processed_data_folder(
    environment: Mapping[str, str] = os.environ,
) -> str:
    """Read and validate the BIOMERO folder name once.

    The name must be a single path segment (not empty, not absolute, no
    separators, not ``.`` or ``..``). An empty or escaping value would put
    BIOMERO's metadata in, or outside, the user's folder, so it falls back to
    the default with a WARNING.

    Args:
        environment (Mapping[str, str]): Environment to read.

    Returns:
        str: The folder name.
    """
    value = environment.get(
        ENV_PROCESSED_DATA_FOLDER, DEFAULT_PROCESSED_DATA_FOLDER)
    is_single_segment = (
        bool(value)
        and value not in {".", ".."}
        and "\\" not in value
        and not Path(value).is_absolute()
        and Path(value).name == value
    )
    if is_single_segment:
        return value
    logger.warning(
        f"Ignoring invalid {ENV_PROCESSED_DATA_FOLDER}={value!r}: it must be "
        f"one folder name; using {DEFAULT_PROCESSED_DATA_FOLDER!r}")
    return DEFAULT_PROCESSED_DATA_FOLDER


PROCESSED_DATA_FOLDER = read_processed_data_folder()
CanonicalSourceLike = Union[
    CanonicalZarrSource,
    CanonicalPlateSource,
    Mapping[str, Any],
]


def _source_payload(source: CanonicalSourceLike) -> dict[str, Any]:
    """Return the shared schema's stable wire representation."""
    if isinstance(source, (CanonicalZarrSource, CanonicalPlateSource)):
        return source.to_dict()
    if "images" in source and "sourceObjectType" not in source:
        return CanonicalPlateSource.from_dict(source).to_dict()
    return CanonicalZarrSource.from_dict(source).to_dict()


class InvalidCanonicalStore(RuntimeError):
    """A canonical destination exists but cannot safely be adopted."""


class CanonicalCreationLocked(RuntimeError):
    """Another process is creating the same canonical Zarr."""


class CanonicalCreationLockUnavailable(RuntimeError):
    """The lock cannot be taken at all (read-only mount, no lock support).

    Not the same as ``CanonicalCreationLocked``: nobody else holds the lock,
    so waiting or retrying will not help.
    """


# msvcrt.locking reports a lock held by another process as EACCES (a
# PermissionError) or EDEADLK; flock reports it as BlockingIOError.
_WINDOWS_LOCK_BUSY_ERRNOS = (errno.EACCES, errno.EDEADLK)


def _is_lock_busy(error: OSError) -> bool:
    """Return whether a failed lock attempt means another process holds it."""
    if isinstance(error, BlockingIOError):
        return True
    return os.name == "nt" and error.errno in _WINDOWS_LOCK_BUSY_ERRNOS


class CanonicalStore:
    """Resolves and commits canonical Zarrs beneath one managed storage root."""

    def __init__(self, storage_root: Union[str, Path]):
        self.storage_root = Path(storage_root).resolve()

    @staticmethod
    def canonical_name(
        object_type: str, object_id: int, source_generation: int
    ) -> str:
        if object_type not in {"Image", "Plate"}:
            raise ValueError("object_type must be Image or Plate")
        if object_id < 1 or source_generation < 1:
            raise ValueError("object_id and source_generation must be positive")
        return f"{object_type}-{object_id}.ome.zarr"

    def relative_path_for(
        self,
        source_directory: Union[str, Path],
        object_type: str,
        object_id: int,
        source_generation: int,
    ) -> Path:
        source_directory = Path(source_directory)
        if source_directory.is_absolute() or ".." in source_directory.parts:
            raise ValueError("source_directory must be relative to managed storage")
        return (
            source_directory
            / PROCESSED_DATA_FOLDER
            / self.canonical_name(object_type, object_id, source_generation)
        )

    def resolve(self, relative_path: Union[str, Path]) -> Path:
        relative_path = Path(relative_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("Path escapes the managed storage root")
        candidate = (self.storage_root / relative_path).resolve()
        try:
            candidate.relative_to(self.storage_root)
        except ValueError as exc:
            raise ValueError("Path escapes the managed storage root") from exc
        return candidate

    def destination_for(self, source: CanonicalSourceLike) -> Path:
        source_payload = _source_payload(source)
        destination = self.resolve(str(source_payload["relativePath"]))
        expected_name = self.canonical_name(
            str(source_payload.get("sourceObjectType", "Plate")),
            int(source_payload["sourceObjectId"]),
            int(source_payload["sourceGeneration"]),
        )
        if destination.name != expected_name:
            raise ValueError(
                f"Canonical path must end with {expected_name}, got "
                f"{destination.name}"
            )
        return destination

    @contextmanager
    def creation_lock(
        self, source: CanonicalSourceLike
    ) -> Iterator[Path]:
        """Hold the creation lock of one canonical Zarr.

        The lock is a kernel file lock (``flock``, or ``msvcrt`` on Windows)
        taken through the shallower's ``locked``, so the operating system
        releases it when the holder exits, also after a crash or kill. The lock
        file that stays behind carries no state and never blocks a later
        creation. The lock is taken where the canonical Zarr is created, which
        is the OMERO script worker container (biomero-scripts call
        ``commit``), not the importer container. A busy lock is not waited
        for: the holder is creating the same Zarr, so the caller fails fast
        and can adopt its result later.

        Args:
            source (CanonicalSourceLike): Canonical source to be created.

        Yields:
            Path: The canonical destination to create.

        Raises:
            CanonicalCreationLocked: If another process holds the lock.
            CanonicalCreationLockUnavailable: If the lock cannot be taken for
                another reason (permissions, unsupported file locks).
        """
        destination = self.destination_for(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._ignore_legacy_creation_lock(destination)
        # ExitStack wraps the shallower's context manager so that taking the
        # lock can fail on its own, apart from the caller's body, and so that
        # "released" is logged after the lock is really released.
        lock_stack = ExitStack()
        logger.debug(f"Acquiring canonical creation lock for {destination}")
        try:
            lock_stack.enter_context(locked(destination))
        except OSError as exc:
            if _is_lock_busy(exc):
                logger.debug(
                    f"Canonical creation lock for {destination} is busy")
                raise CanonicalCreationLocked(
                    f"Canonical creation is already locked by another "
                    f"process, not waiting: {destination}"
                ) from exc
            raise CanonicalCreationLockUnavailable(
                f"Cannot take the canonical creation lock for "
                f"{destination} (errno {exc.errno}: {exc}); the storage "
                f"may be read-only or lack file lock support"
            ) from exc
        logger.info(f"Acquired canonical creation lock for {destination}")
        try:
            yield destination
        finally:
            lock_stack.close()
            logger.debug(
                f"Released canonical creation lock for {destination}")

    @staticmethod
    def _ignore_legacy_creation_lock(destination: Path) -> None:
        """Log a PID lock file of an older importer; it no longer blocks.

        The old lock was the existence of the file itself, so a leftover from
        a crash blocked creation until someone removed it. Nothing reads it
        now. It is left in place (BIOMERO's own folder, harmless) so that an
        older importer still running during a rolling upgrade keeps its lock.
        """
        legacy_lock = destination.with_name(
            destination.name + LEGACY_CREATION_LOCK_SUFFIX)
        if legacy_lock.exists():
            logger.info(
                f"Ignoring legacy canonical creation lock {legacy_lock} left "
                f"by an older importer or a crash; it is safe to delete")

    def adopt(self, source: CanonicalSourceLike) -> Optional[Path]:
        """Returns a matching committed canonical, or None when absent."""
        source_payload = _source_payload(source)
        destination = self.destination_for(source)
        if not destination.exists():
            return None
        self._validate_zarr(destination)
        marker_path = destination / CANONICAL_MARKER_NAME
        if not marker_path.is_file():
            raise InvalidCanonicalStore(
                f"Canonical store has no committed marker: {destination}"
            )
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidCanonicalStore(
                f"Canonical marker is unreadable: {marker_path}"
            ) from exc
        if (
            marker.get("markerSchema") != CANONICAL_MARKER_SCHEMA
            or marker.get("state") != "committed"
        ):
            raise InvalidCanonicalStore(
                f"Canonical marker is not committed: {marker_path}"
            )
        if _source_payload(marker.get("source", {})) != source_payload:
            raise InvalidCanonicalStore(
                f"Canonical marker does not match requested source: {destination}"
            )
        return destination

    def commit(
        self,
        staging_path: Union[str, Path],
        source: CanonicalSourceLike,
    ) -> Path:
        """Atomically promotes a verified staging Zarr with its recovery marker."""
        staging_path = Path(staging_path)
        if not staging_path.is_absolute():
            staging_path = staging_path.resolve()
        self._validate_zarr(staging_path)

        with self.creation_lock(source) as destination:
            existing = self.adopt(source)
            if existing is not None:
                return existing
            if destination.exists():
                raise InvalidCanonicalStore(
                    f"Uncommitted canonical destination already exists: {destination}"
                )
            marker = {
                "markerSchema": CANONICAL_MARKER_SCHEMA,
                "state": "committed",
                "source": _source_payload(source),
            }
            marker_tmp = staging_path / f"{CANONICAL_MARKER_NAME}.tmp"
            marker_path = staging_path / CANONICAL_MARKER_NAME
            marker_tmp.write_text(
                json.dumps(marker, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(marker_tmp, marker_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging_path, destination)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                self._copy_across_filesystems(staging_path, destination)
            return destination

    @staticmethod
    def _copy_across_filesystems(staging_path: Path, destination: Path) -> None:
        """Copy beside the destination, atomically publish, then remove source."""
        destination_staging = Path(tempfile.mkdtemp(
            prefix=f".{destination.name}.staging-",
            dir=destination.parent,
        ))
        try:
            shutil.copytree(
                staging_path,
                destination_staging,
                dirs_exist_ok=True,
            )
            os.replace(destination_staging, destination)
        except Exception:
            shutil.rmtree(destination_staging, ignore_errors=True)
            raise
        shutil.rmtree(staging_path)

    @staticmethod
    def _validate_zarr(path: Path) -> None:
        if not path.is_dir():
            raise InvalidCanonicalStore(f"Canonical Zarr is not a directory: {path}")
        if (path / ".zgroup").is_file() or (path / "zarr.json").is_file():
            return
        metadata = path / "OME" / "METADATA.ome.xml"
        has_numbered_series = any(
            child.is_dir()
            and child.name.isdigit()
            and (
                (child / ".zgroup").is_file()
                or (child / "zarr.json").is_file()
            )
            for child in path.iterdir()
        )
        if metadata.is_file() and has_numbered_series:
            return
        raise InvalidCanonicalStore(
            f"Path is not a supported Zarr or bioformats2raw layout: {path}"
        )


def external_canonical_marker_path(
    zarr_path: Union[str, Path],
    processed_folder: str = PROCESSED_DATA_FOLDER,
) -> Path:
    """Return where BIOMERO stores the sidecar of a Zarr it indexes in place.

    The sidecar lives in the BIOMERO folder next to the Zarr's directory, the
    same per-directory ``.processed`` folder that BIOMERO uses for its other
    outputs: ``<zarr parent>/.processed/.biomero/<zarr name>.canonical.json``.
    It never sits next to the (user-owned) input, and it moves or disappears
    with the user's folder. This function is the single owner of the location;
    it needs no storage roots, so every component derives the same path.

    Args:
        zarr_path (Union[str, Path]): Indexed Zarr directory.
        processed_folder (str): Name of the BIOMERO folder, resolved once in
            ``PROCESSED_DATA_FOLDER``.

    Returns:
        Path: Sidecar location (not necessarily existing).

    Raises:
        ValueError: If the path has no name (for example a filesystem root).
    """
    zarr_path = _require_named(zarr_path)
    if zarr_path.parent.name == processed_folder:
        # Already inside a BIOMERO folder: do not nest .processed/.processed.
        biomero_folder = zarr_path.parent
    else:
        biomero_folder = zarr_path.parent / processed_folder
    return (
        biomero_folder
        / CANONICAL_METADATA_DIRECTORY
        / f"{zarr_path.name}{CANONICAL_MARKER_SUFFIX}"
    )


def _require_named(zarr_path: Union[str, Path]) -> Path:
    """Return the path, refusing one with no name to key a sidecar by."""
    zarr_path = Path(zarr_path)
    if not zarr_path.name:
        raise ValueError(
            f"Zarr path has no name to key a sidecar: {zarr_path!r}")
    return zarr_path


def legacy_canonical_marker_path(zarr_path: Union[str, Path]) -> Path:
    """Return the beta location next to the input: ``<parent>/.biomero/``.

    Kept only to read and migrate sidecars written by earlier betas.
    BIOMERO no longer writes here.
    """
    zarr_path = _require_named(zarr_path)
    return (
        zarr_path.parent
        / CANONICAL_METADATA_DIRECTORY
        / f"{zarr_path.name}{CANONICAL_MARKER_SUFFIX}"
    )


def load_canonical_marker(
    zarr_path: Union[str, Path],
    processed_folder: str = PROCESSED_DATA_FOLDER,
) -> Optional[Union[CanonicalZarrSource, CanonicalPlateSource]]:
    """Load a committed internal marker or an external sidecar of a Zarr.

    Reads the internal marker, then the BIOMERO-folder sidecar, then the
    legacy sidecar next to the input. Reading never moves or deletes files.

    Raises:
        InvalidCanonicalStore: If the marker is unreadable or not committed.
    """
    marker_path = _find_canonical_marker_path(zarr_path, processed_folder)
    if marker_path is None:
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidCanonicalStore(
            f"Canonical marker is unreadable: {marker_path}"
        ) from exc
    if (
        marker.get("markerSchema") != CANONICAL_MARKER_SCHEMA
        or marker.get("state") != "committed"
        or not isinstance(marker.get("source"), dict)
    ):
        raise InvalidCanonicalStore(
            f"Canonical marker is not committed: {marker_path}"
        )
    payload = marker["source"]
    if "images" in payload and "sourceObjectType" not in payload:
        return CanonicalPlateSource.from_dict(payload)
    return CanonicalZarrSource.from_dict(payload)


def _find_canonical_marker_path(
    zarr_path: Union[str, Path],
    processed_folder: str,
) -> Optional[Path]:
    """Return the existing marker of a Zarr, or None when it has none.

    Lookup order: internal marker, BIOMERO-folder sidecar (authoritative when
    both sidecars exist), legacy sidecar next to the input (read-only
    fallback for beta installs).
    """
    zarr_path = Path(zarr_path).resolve()
    internal = zarr_path / CANONICAL_MARKER_NAME
    sidecar = external_canonical_marker_path(zarr_path, processed_folder)
    legacy = legacy_canonical_marker_path(zarr_path)
    if _is_readable_file(internal):
        logger.debug(f"Using internal canonical marker {internal}")
        return internal
    if _is_readable_file(sidecar):
        logger.debug(f"Using canonical sidecar {sidecar}")
        return sidecar
    if _is_readable_file(legacy):
        logger.debug(
            f"Using legacy canonical sidecar {legacy}; it moves to the "
            f"BIOMERO folder on the next write")
        return legacy
    logger.debug(f"No canonical marker found for {zarr_path}")
    return None


def _is_readable_file(path: Path) -> bool:
    """Return whether a marker file exists; an unreadable folder means no.

    ``Path.is_file`` raises ``PermissionError`` when a parent folder cannot be
    traversed. A marker that cannot be reached is the same as no marker: the
    caller falls back to the normal path, so log the reason and go on.
    """
    try:
        return path.is_file()
    except OSError as exc:
        logger.warning(
            f"Cannot look for canonical marker {path}: {exc}; treating it as "
            f"absent")
        return False


def write_indexed_canonical_marker(
    zarr_path: Union[str, Path],
    source: CanonicalSourceLike,
    processed_folder: str = PROCESSED_DATA_FOLDER,
) -> Path:
    """Atomically persist detailed identities of an indexed Zarr.

    An internal marker (a Zarr BIOMERO created) is updated in place. For a Zarr
    indexed in place, the sidecar is written to the BIOMERO folder, and a
    sidecar left next to the input by an earlier beta is removed afterwards.
    The sidecar is an optimisation: callers treat a failure as "this input is
    not reusable as a canonical source" and fall back to the normal path.

    Args:
        zarr_path (Union[str, Path]): Indexed Zarr directory.
        source (CanonicalSourceLike): Identities to persist.
        processed_folder (str): Name of the BIOMERO folder.

    Returns:
        Path: The marker that was written.

    Raises:
        OSError: If the BIOMERO folder cannot be created or written (for
            example a read-only mount). The old sidecar is then left in place.
        InvalidCanonicalStore: If the path is not a supported Zarr.
    """
    zarr_path = Path(zarr_path).resolve()
    CanonicalStore._validate_zarr(zarr_path)
    internal = zarr_path / CANONICAL_MARKER_NAME
    if internal.is_file():
        marker_path = internal
        logger.debug(f"Updating internal canonical marker {internal}")
    else:
        marker_path = external_canonical_marker_path(
            zarr_path, processed_folder)
        logger.info(f"Writing canonical sidecar {marker_path} for {zarr_path}")
    marker = {
        "markerSchema": CANONICAL_MARKER_SCHEMA,
        "state": "committed",
        "source": _source_payload(source),
    }
    _write_marker_file(marker_path, marker)
    if marker_path != internal:
        # Only after the new file is safely in place, so a failed write
        # leaves the old sidecar readable.
        remove_legacy_canonical_marker(zarr_path, processed_folder)
    return marker_path


def _write_marker_file(marker_path: Path, marker: Mapping[str, Any]) -> None:
    """Write a marker through a temporary file and an atomic replace."""
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{marker_path.name}.",
        suffix=".tmp",
        dir=marker_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, marker_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def remove_legacy_canonical_marker(
    zarr_path: Union[str, Path],
    processed_folder: str = PROCESSED_DATA_FOLDER,
) -> bool:
    """Remove BIOMERO's own old sidecar and its folder if that is now empty.

    Only the exact ``<name>.canonical.json`` file is removed; the ``.biomero``
    folder is removed only when empty, so anything else in it is left alone.
    A failure (read-only folder, race with another process) is logged and
    ignored, because the sidecar in the BIOMERO folder is authoritative.

    For a Zarr that sits inside a BIOMERO folder the legacy and current
    locations are the same file; that file is never removed here.

    Returns:
        bool: True if a legacy sidecar was removed.
    """
    legacy = legacy_canonical_marker_path(zarr_path)
    if legacy == external_canonical_marker_path(zarr_path, processed_folder):
        logger.debug(f"Legacy location {legacy} is the current sidecar; kept")
        return False
    return _remove_sidecar(legacy)


def remove_external_canonical_markers(
    zarr_path: Union[str, Path],
    processed_folder: str = PROCESSED_DATA_FOLDER,
) -> None:
    """Remove the sidecars (current and legacy) of a Zarr BIOMERO deleted."""
    _remove_sidecar(
        external_canonical_marker_path(zarr_path, processed_folder))
    remove_legacy_canonical_marker(zarr_path, processed_folder)


def _remove_sidecar(marker_path: Path) -> bool:
    """Delete one sidecar file, then its ``.biomero`` folder if now empty."""
    try:
        existed = marker_path.exists()
        marker_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            f"Could not remove canonical sidecar {marker_path}: {exc}; "
            f"the sidecar in the BIOMERO folder stays authoritative")
        return False
    if not existed:
        logger.debug(f"No canonical sidecar to remove at {marker_path}")
        return False
    logger.info(f"Removed canonical sidecar {marker_path}")
    _remove_folder_if_empty(marker_path.parent)
    return True


def _remove_folder_if_empty(folder: Path) -> None:
    """Remove a folder only if nothing else is in it (``rmdir`` semantics)."""
    try:
        folder.rmdir()
    except OSError:
        logger.debug(f"Kept {folder}: not empty or not removable")
    else:
        logger.info(f"Removed empty folder {folder}")


def move_canonical_marker(
    source_zarr: Union[str, Path],
    destination_zarr: Union[str, Path],
    processed_folder: str = PROCESSED_DATA_FOLDER,
) -> Optional[Path]:
    """Move the sidecar of a Zarr that was renamed from source to destination.

    The sidecar is keyed by the Zarr's directory and name, so renaming the
    Zarr must rename its sidecar. Call this after the Zarr itself has been
    moved. An internal marker moves with the Zarr and needs no action. An
    existing sidecar at the destination is never overwritten: it is kept and
    the source sidecar stays where it is for the caller to rewrite or remove.

    Args:
        source_zarr (Union[str, Path]): Former Zarr location.
        destination_zarr (Union[str, Path]): New Zarr location.
        processed_folder (str): Name of the BIOMERO folder.

    Returns:
        Optional[Path]: The new sidecar location, or None when nothing moved.
    """
    source_zarr = Path(source_zarr).resolve()
    destination_zarr = Path(destination_zarr).resolve()
    candidates = (
        external_canonical_marker_path(source_zarr, processed_folder),
        legacy_canonical_marker_path(source_zarr),
    )
    old_marker = next((path for path in candidates if path.is_file()), None)
    if old_marker is None:
        logger.debug(
            f"No external canonical sidecar to move for {source_zarr}")
        return None
    new_marker = external_canonical_marker_path(
        destination_zarr, processed_folder)
    if new_marker == old_marker:
        logger.debug(
            f"Canonical sidecar {old_marker} already at its destination")
        return new_marker
    if new_marker.exists():
        logger.warning(
            f"Not moving canonical sidecar {old_marker}: {new_marker} already "
            f"exists and is kept")
        return None
    new_marker.parent.mkdir(parents=True, exist_ok=True)
    # shutil.move renames, and copies then removes across filesystems.
    shutil.move(str(old_marker), str(new_marker))
    logger.info(f"Moved canonical sidecar {old_marker} to {new_marker}")
    _remove_folder_if_empty(old_marker.parent)
    return new_marker


def cache_refreshed_inputs(expected, refreshed_inputs, storage_roots):
    """Persist measured comparison metadata without changing source bindings.

    The workflow snapshot remains immutable. This replaces the current managed
    source cache so later runs reuse it, including after a keep-full decision.
    """
    def binding(source):
        return source.model_dump(exclude={
            "pixel_identity", "pixel_identity_origin", "canonical_pixel_verified",
            "image_metadata",
        })

    expected_by_artifact = {item.transfer_artifact: item for item in expected.inputs}
    written = 0
    for refreshed in refreshed_inputs:
        previous = expected_by_artifact.get(refreshed.transfer_artifact)
        if previous is None:
            raise ValueError("Comparison refresh has an unknown input artifact")
        if (previous.model_dump(exclude={"source", "plate_source"})
                != refreshed.model_dump(exclude={"source", "plate_source"})):
            raise ValueError("Comparison refresh changed selected input binding")
        previous_sources = ({i.image_node_path: i.source for i in previous.plate_source.images}
                            if previous.plate_source else {previous.source.node_path: previous.source})
        current_sources = ({i.image_node_path: i.source for i in refreshed.plate_source.images}
                           if refreshed.plate_source else {refreshed.source.node_path: refreshed.source})
        if previous_sources.keys() != current_sources.keys():
            raise ValueError("Comparison refresh changed input image layout")
        changed = False
        for node_path, source in current_sources.items():
            original = previous_sources[node_path]
            if binding(original) != binding(source):
                raise ValueError("Comparison refresh changed canonical source binding")
            if (original.image_metadata and source.image_metadata
                    and original.image_metadata != source.image_metadata):
                raise ValueError("Comparison refresh changed original image metadata")
            if source.pixel_identity is not None and source.pixel_identity.is_current_profile:
                changed = changed or source.pixel_identity != original.pixel_identity
        if not changed:
            continue
        updated = refreshed.plate_source or refreshed.source
        root = storage_roots.get(updated.storage_root)
        if root is None:
            raise ValueError("Unknown managed storage root for comparison refresh")
        store = CanonicalStore(root)
        path = store.resolve(updated.relative_path)
        if not path.is_dir():
            raise ValueError("Canonical comparison backing store is unavailable")
        # Retain the original label inventory. This operation refreshes only
        # original-image comparisons, never label ownership or provenance.
        if refreshed.plate_source:
            updated = previous.plate_source.model_copy(update={"images": tuple(
                image.model_copy(update={"source": current_sources[image.image_node_path]})
                for image in previous.plate_source.images)})
        try:
            write_indexed_canonical_marker(path, updated)
        except (OSError, InvalidCanonicalStore) as exc:
            # The sidecar is an optimisation; the validated result stands.
            logger.warning(
                f"Could not persist optional comparison metadata for {path}: "
                f"{exc}; continuing with the validated result", exc_info=True)
            continue
        written += 1
    return written
