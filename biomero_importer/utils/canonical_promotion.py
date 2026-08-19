"""Verified promotion of workflow-exported Zarrs into canonical storage."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from biomero_schema.zarr import CanonicalZarrSource, PixelIdentity


class CanonicalStoreLike(Protocol):
    def relative_path_for(
        self,
        source_directory: str | Path,
        object_type: str,
        object_id: int,
        source_generation: int,
    ) -> Path: ...

    def commit(
        self,
        staging_path: str | Path,
        source: CanonicalZarrSource,
    ) -> Path: ...


class CanonicalPixelMismatch(RuntimeError):
    """An exported candidate does not represent the selected original pixels."""


@dataclass(frozen=True)
class CanonicalPromotionResult:
    """The committed source record and its local managed path."""

    source: CanonicalZarrSource
    path: Path


class CanonicalPromotionService:
    """Verify and transactionally commit one deterministic canonical Zarr."""

    def __init__(
        self,
        *,
        storage_root_id: str,
        storage_root: str | Path,
        canonical_store_factory: Callable[[str | Path], CanonicalStoreLike] | None = None,
        identities_match: Callable[[PixelIdentity, PixelIdentity], bool] | None = None,
    ) -> None:
        if not storage_root_id:
            raise ValueError("storage_root_id must not be empty")
        if canonical_store_factory is None:
            from biomero_importer.utils.canonical_store import CanonicalStore

            canonical_store_factory = CanonicalStore
        if identities_match is None:
            from biomero_importer.utils.pixel_identity import pixel_identities_match

            identities_match = pixel_identities_match
        self.storage_root_id = storage_root_id
        self.store = canonical_store_factory(storage_root)
        self.identities_match = identities_match

    def promote(
        self,
        staging_path: str | Path,
        *,
        source_directory: str | Path,
        source_object_type: Literal["Image", "Plate"],
        source_object_id: int,
        source_generation: int,
        node_path: str,
        original_identity: PixelIdentity,
        exported_identity: PixelIdentity,
        pixel_identity_origin: Literal[
            "raw", "omero-pixels", "canonical-bootstrap"
        ],
        interchange_profile: str = "ngff-0.4-zarr-v2",
        store_identity: str | None = None,
    ) -> CanonicalPromotionResult:
        """Commit ``staging_path`` only after exact cross-source verification."""
        if (
            original_identity.node_path != node_path
            or exported_identity.node_path != node_path
        ):
            raise CanonicalPixelMismatch(
                "Original and exported identity node paths must match the "
                f"canonical node path {node_path!r}"
            )
        if not self.identities_match(original_identity, exported_identity):
            raise CanonicalPixelMismatch(
                f"Exported pixels do not match {source_object_type} "
                f"{source_object_id} generation {source_generation}"
            )

        relative_path = self.store.relative_path_for(
            source_directory,
            source_object_type,
            source_object_id,
            source_generation,
        ).as_posix()
        source = CanonicalZarrSource(
            storage_root=self.storage_root_id,
            relative_path=relative_path,
            node_path=node_path,
            source_object_type=source_object_type,
            source_object_id=source_object_id,
            source_generation=source_generation,
            interchange_profile=interchange_profile,
            pixel_identity=original_identity,
            pixel_identity_origin=pixel_identity_origin,
            canonical_pixel_verified=True,
            store_identity=store_identity,
        )
        path = self.store.commit(staging_path, source)
        return CanonicalPromotionResult(source=source, path=path)

    def index_existing(
        self,
        existing_path: str | Path,
        *,
        relative_path: str | Path,
        source_object_type: Literal["Image", "Plate"],
        source_object_id: int,
        source_generation: int,
        node_path: str,
        original_identity: PixelIdentity,
        existing_identity: PixelIdentity,
        pixel_identity_origin: Literal[
            "raw", "omero-pixels", "canonical-bootstrap"
        ],
        interchange_profile: str = "ngff-0.4-zarr-v2",
        store_identity: str | None = None,
    ) -> CanonicalPromotionResult:
        """Index a verified Zarr already inside managed storage in place."""
        if (
            original_identity.node_path != node_path
            or existing_identity.node_path != node_path
        ):
            raise CanonicalPixelMismatch(
                "Original and existing identity node paths must match the "
                f"canonical node path {node_path!r}"
            )
        if not self.identities_match(original_identity, existing_identity):
            raise CanonicalPixelMismatch(
                f"Existing pixels do not match {source_object_type} "
                f"{source_object_id} generation {source_generation}"
            )

        path = Path(existing_path).resolve()
        managed_path = self.store.resolve(relative_path)
        if path != managed_path:
            raise ValueError(
                "Existing Zarr path does not match its managed relative path"
            )
        if not path.is_dir():
            raise ValueError(f"Existing Zarr is not a directory: {path}")

        source = CanonicalZarrSource(
            storage_root=self.storage_root_id,
            relative_path=Path(relative_path).as_posix(),
            node_path=node_path,
            source_object_type=source_object_type,
            source_object_id=source_object_id,
            source_generation=source_generation,
            interchange_profile=interchange_profile,
            pixel_identity=original_identity,
            pixel_identity_origin=pixel_identity_origin,
            canonical_pixel_verified=True,
            store_identity=store_identity,
        )
        return CanonicalPromotionResult(source=source, path=path)


__all__ = [
    "CanonicalPixelMismatch",
    "CanonicalPromotionResult",
    "CanonicalPromotionService",
]
