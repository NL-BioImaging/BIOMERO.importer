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

from contextlib import contextmanager
import errno
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterator, Mapping, NamedTuple, Optional, Union

from biomero_schema.zarr import CanonicalPlateSource, CanonicalZarrSource
from biomero_shallower.result_zarr import load_managed_storage_roots

logger = logging.getLogger(__name__)


CANONICAL_MARKER_NAME = ".biomero-canonical.json"
CANONICAL_MARKER_SCHEMA = 1
CANONICAL_METADATA_DIRECTORY = ".biomero"
CANONICAL_MARKER_SUFFIX = ".canonical.json"
PROCESSED_DATA_FOLDER = os.getenv(
    "PROCESSED_DATA_FOLDER",
    ".processed",
)
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


class ManagedZarrLocation(NamedTuple):
    """Where a Zarr sits within BIOMERO managed storage."""

    storage_id: str
    root: Path
    relative_path: Path


class InvalidCanonicalStore(RuntimeError):
    """A canonical destination exists but cannot safely be adopted."""


class CanonicalCreationLocked(RuntimeError):
    """Another process is creating the same canonical Zarr."""


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
        destination = self.destination_for(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        lock_path = destination.with_name(destination.name + ".lock")
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            raise CanonicalCreationLocked(
                f"Canonical creation is already locked: {destination}"
            ) from exc
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.close(descriptor)
            descriptor = None
            yield destination
        finally:
            if descriptor is not None:
                os.close(descriptor)
            lock_path.unlink(missing_ok=True)

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


def locate_managed_zarr(
    zarr_path: Union[str, Path],
    storage_roots: Mapping[str, Union[str, Path]],
) -> ManagedZarrLocation:
    """Find the most specific managed storage root that contains a Zarr.

    A group root beneath the mount-wide root wins, because it is the deepest.
    This mirrors how the transfer script records ``storageRoot`` for a Zarr
    that is indexed in place.

    Args:
        zarr_path (Union[str, Path]): Zarr directory to locate.
        storage_roots (Mapping[str, Union[str, Path]]): Storage id to root
            directory, as returned by ``load_managed_storage_roots``.

    Returns:
        ManagedZarrLocation: Storage id, root and path relative to the root.

    Raises:
        ValueError: If the Zarr is outside every managed storage root.
    """
    resolved_path = Path(zarr_path).resolve()
    candidates = []
    for storage_id, root in storage_roots.items():
        resolved_root = Path(root).resolve()
        try:
            relative_path = resolved_path.relative_to(resolved_root)
        except ValueError:
            continue
        candidates.append(ManagedZarrLocation(
            storage_id, resolved_root, relative_path))
    if not candidates:
        raise ValueError(
            f"Zarr is outside BIOMERO managed storage: {resolved_path}"
        )
    return max(candidates, key=lambda candidate: len(candidate.root.parts))


def external_canonical_marker_path(
    zarr_path: Union[str, Path],
    storage_roots: Optional[Mapping[str, Union[str, Path]]] = None,
    processed_folder: str = PROCESSED_DATA_FOLDER,
) -> Path:
    """Return where BIOMERO stores the sidecar of a Zarr it indexes in place.

    The sidecar lives in the BIOMERO folder of the managed storage root that
    contains the Zarr, never next to the (user-owned) input:
    ``<root>/.processed/.biomero/<storage id>/<relative path>.canonical.json``.
    This is the single owner of that location. Users may move or delete their
    own data at any time, so nothing is written into their folders.

    Args:
        zarr_path (Union[str, Path]): Indexed Zarr directory.
        storage_roots (Optional[Mapping[str, Union[str, Path]]]): Storage id
            to root directory. Defaults to ``load_managed_storage_roots()``.
        processed_folder (str): Name of the BIOMERO folder in each root.

    Returns:
        Path: Sidecar location (not necessarily existing).

    Raises:
        ValueError: If the Zarr is outside every managed storage root.
    """
    if storage_roots is None:
        storage_roots = load_managed_storage_roots()
    location = locate_managed_zarr(zarr_path, storage_roots)
    relative_marker = location.relative_path.with_name(
        f"{location.relative_path.name}{CANONICAL_MARKER_SUFFIX}")
    return (
        location.root
        / processed_folder
        / CANONICAL_METADATA_DIRECTORY
        / location.storage_id
        / relative_marker
    )


def legacy_canonical_marker_path(zarr_path: Union[str, Path]) -> Path:
    """Return the beta location next to the input: ``<parent>/.biomero/``.

    Kept only to read and migrate sidecars written by earlier betas.
    BIOMERO no longer writes here.
    """
    zarr_path = Path(zarr_path)
    return (
        zarr_path.parent
        / CANONICAL_METADATA_DIRECTORY
        / f"{zarr_path.name}{CANONICAL_MARKER_SUFFIX}"
    )


def find_canonical_marker_path(
    zarr_path: Union[str, Path],
    storage_roots: Optional[Mapping[str, Union[str, Path]]] = None,
) -> Optional[Path]:
    """Return the existing marker of a Zarr, or None when it has none.

    Lookup order: internal marker, BIOMERO-folder sidecar, legacy sidecar
    next to the input (read-only fallback for beta installs).
    """
    zarr_path = Path(zarr_path).resolve()
    internal = zarr_path / CANONICAL_MARKER_NAME
    if internal.is_file():
        logger.debug(f"Using internal canonical marker {internal}")
        return internal
    sidecar = _sidecar_path_or_none(zarr_path, storage_roots)
    if sidecar is not None and sidecar.is_file():
        logger.debug(f"Using canonical sidecar {sidecar}")
        return sidecar
    legacy = legacy_canonical_marker_path(zarr_path)
    if legacy.is_file():
        logger.debug(
            f"Using legacy canonical sidecar {legacy}; it moves to the "
            f"BIOMERO folder on the next write")
        return legacy
    logger.debug(f"No canonical marker found for {zarr_path}")
    return None


def remove_legacy_canonical_marker(zarr_path: Union[str, Path]) -> bool:
    """Remove BIOMERO's own old sidecar and its folder if that is now empty.

    Only the exact ``<name>.canonical.json`` file is removed; the ``.biomero``
    folder is removed only when empty, so anything else in it is left alone.

    Returns:
        bool: True if a legacy sidecar was removed.
    """
    legacy = legacy_canonical_marker_path(zarr_path)
    if not legacy.is_file():
        logger.debug(f"No legacy canonical sidecar to remove at {legacy}")
        return False
    legacy.unlink()
    logger.info(f"Removed legacy canonical sidecar {legacy}")
    _remove_folder_if_empty(legacy.parent)
    return True


def _remove_folder_if_empty(folder: Path) -> None:
    """Remove a folder only when nothing else is in it (``rmdir`` semantics)."""
    try:
        folder.rmdir()
    except OSError:
        logger.debug(f"Kept {folder}: not empty or not removable")
    else:
        logger.info(f"Removed empty folder {folder}")


def move_canonical_marker(
    source_zarr: Union[str, Path],
    destination_zarr: Union[str, Path],
    storage_roots: Optional[Mapping[str, Union[str, Path]]] = None,
) -> Optional[Path]:
    """Move the sidecar of a Zarr that was renamed from source to destination.

    The sidecar is keyed by the Zarr's location, so renaming the Zarr must
    rename its sidecar. Call this after the Zarr itself has been moved. An
    internal marker moves with the Zarr and needs no action.

    Args:
        source_zarr (Union[str, Path]): Former Zarr location.
        destination_zarr (Union[str, Path]): New Zarr location.
        storage_roots (Optional[Mapping[str, Union[str, Path]]]): Storage id
            to root directory. Defaults to ``load_managed_storage_roots()``.

    Returns:
        Optional[Path]: The new sidecar location, or None when the source
        had no sidecar.
    """
    source_zarr = Path(source_zarr).resolve()
    destination_zarr = Path(destination_zarr).resolve()
    legacy = legacy_canonical_marker_path(source_zarr)
    candidates = (_sidecar_path_or_none(source_zarr, storage_roots), legacy)
    old_marker = next(
        (path for path in candidates if path is not None and path.is_file()),
        None)
    if old_marker is None:
        logger.debug(f"No external canonical sidecar to move for {source_zarr}")
        return None
    new_marker = external_canonical_marker_path(destination_zarr, storage_roots)
    new_marker.parent.mkdir(parents=True, exist_ok=True)
    os.replace(old_marker, new_marker)
    logger.info(f"Moved canonical sidecar {old_marker} to {new_marker}")
    if old_marker == legacy:
        _remove_folder_if_empty(legacy.parent)
    return new_marker


def _sidecar_path_or_none(
    zarr_path: Path,
    storage_roots: Optional[Mapping[str, Union[str, Path]]],
) -> Optional[Path]:
    """Return the BIOMERO-folder sidecar path, or None outside managed storage."""
    try:
        return external_canonical_marker_path(zarr_path, storage_roots)
    except ValueError as exc:
        logger.debug(f"No BIOMERO-folder sidecar location for {zarr_path}: {exc}")
        return None


def write_indexed_canonical_marker(
    zarr_path: Union[str, Path],
    source: CanonicalSourceLike,
    storage_roots: Optional[Mapping[str, Union[str, Path]]] = None,
) -> Path:
    """Atomically persist detailed identities of an indexed managed Zarr.

    An internal marker (a Zarr BIOMERO created) is updated in place. For a Zarr
    indexed in place, the sidecar is written to the BIOMERO folder, and a
    sidecar left next to the input by an earlier beta is removed afterwards.

    Args:
        zarr_path (Union[str, Path]): Indexed Zarr directory.
        source (CanonicalSourceLike): Identities to persist.
        storage_roots (Optional[Mapping[str, Union[str, Path]]]): Storage id
            to root directory. Defaults to ``load_managed_storage_roots()``.

    Returns:
        Path: The marker that was written.

    Raises:
        ValueError: If an in-place Zarr is outside every managed storage root.
    """
    zarr_path = Path(zarr_path).resolve()
    CanonicalStore._validate_zarr(zarr_path)
    internal = zarr_path / CANONICAL_MARKER_NAME
    if internal.is_file():
        marker_path = internal
        logger.debug(f"Updating internal canonical marker {internal}")
    else:
        marker_path = external_canonical_marker_path(zarr_path, storage_roots)
        logger.info(f"Writing canonical sidecar {marker_path} for {zarr_path}")
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "markerSchema": CANONICAL_MARKER_SCHEMA,
        "state": "committed",
        "source": _source_payload(source),
    }
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
    if marker_path != internal:
        remove_legacy_canonical_marker(zarr_path)
    return marker_path


def load_canonical_marker(
    zarr_path: Union[str, Path],
    storage_roots: Optional[Mapping[str, Union[str, Path]]] = None,
) -> Optional[Union[CanonicalZarrSource, CanonicalPlateSource]]:
    """Load a committed internal marker or an external sidecar of a Zarr.

    Reads the internal marker, then the BIOMERO-folder sidecar, then the
    legacy sidecar next to the input. Reading never moves or deletes files.
    """
    marker_path = find_canonical_marker_path(zarr_path, storage_roots)
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
            write_indexed_canonical_marker(path, updated, storage_roots)
        except OSError:
            logging.getLogger(__name__).warning(
                "Could not persist optional comparison metadata for %s; "
                "continuing with the validated result", path, exc_info=True)
            continue
        written += 1
    return written
