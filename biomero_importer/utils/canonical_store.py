"""Transactional storage primitives for canonical BIOMERO Zarrs."""

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterator, Mapping, Optional, Union

from biomero_schema.zarr import CanonicalZarrSource


CANONICAL_MARKER_NAME = ".biomero-canonical.json"
CANONICAL_MARKER_SCHEMA = 1
PROCESSED_DATA_FOLDER = ".processed"
CanonicalSourceLike = Union[CanonicalZarrSource, Mapping[str, Any]]


def _source_payload(source: CanonicalSourceLike) -> dict[str, Any]:
    """Return the shared schema's stable wire representation."""
    if isinstance(source, CanonicalZarrSource):
        return source.to_dict()
    return CanonicalZarrSource.from_dict(source).to_dict()


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
        return f"{object_type}-{object_id}.g{source_generation}.ome.zarr"

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
            str(source_payload["sourceObjectType"]),
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
        if marker.get("source") != source_payload:
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
