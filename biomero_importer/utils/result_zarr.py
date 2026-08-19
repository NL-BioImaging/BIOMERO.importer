"""Discover and compare returned NGFF stores without mutating them.

This module is the decision boundary used before shallow-result normalization.
It deliberately performs no deletion, rewriting, or OMERO registration.  A
caller can therefore run it in keep-only mode and safely retain every result
when discovery or identity matching is incomplete.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
from typing import Literal

from biomero_schema.zarr import (
    CanonicalInput,
    CanonicalInputManifest,
    PixelIdentity,
)

from .pixel_identity import (
    IsccBioIdentityProvider,
    PixelIdentityError,
    pixel_identities_match,
    read_zarr_v2_semantic_guard,
)


@dataclass(frozen=True)
class NgffNode:
    """One explicitly discovered image or label node in a returned store."""

    node_path: str
    role: Literal["image", "label"]
    parent_image_node_path: str | None = None


@dataclass(frozen=True)
class ReturnedZarrDecision:
    """Keep-only result of comparing one returned store with workflow inputs."""

    store_path: Path
    outcome: Literal["eligible", "keep-full"]
    reason: str
    image_identities: tuple[PixelIdentity, ...] = ()
    label_node_paths: tuple[str, ...] = ()
    matched_inputs: tuple[CanonicalInput, ...] = ()

    @property
    def eligible(self) -> bool:
        return self.outcome == "eligible"


def _safe_node_path(value: str) -> str:
    if not value or "\\" in value:
        raise PixelIdentityError(f"Unsafe NGFF node path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise PixelIdentityError(f"Unsafe NGFF node path: {value!r}")
    return value


def _join_node_path(*parts: str) -> str:
    useful = [part for part in parts if part and part != "."]
    return str(PurePosixPath(*useful)) if useful else "."


def _node_directory(root: Path, node_path: str) -> Path:
    if node_path == ".":
        return root
    return root.joinpath(*PurePosixPath(node_path).parts)


def _read_attrs(node: Path) -> dict:
    path = node / ".zattrs"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PixelIdentityError(f"Cannot read NGFF attributes: {path}") from exc
    if not isinstance(value, dict):
        raise PixelIdentityError(f"Invalid NGFF attributes: {path}")
    return value


def _discover_labels(root: Path, image_node_path: str) -> list[NgffNode]:
    image_node = _node_directory(root, image_node_path)
    labels_group = image_node / "labels"
    if not labels_group.is_dir():
        return []
    labels_attrs = _read_attrs(labels_group)
    labels = labels_attrs.get("labels")
    if not isinstance(labels, list):
        raise PixelIdentityError(
            f"NGFF labels metadata has no labels list: {labels_group}"
        )

    discovered = []
    seen = set()
    for label in labels:
        if not isinstance(label, str):
            raise PixelIdentityError("NGFF label names must be strings")
        label = _safe_node_path(label)
        label_node_path = _join_node_path(image_node_path, "labels", label)
        if label_node_path in seen:
            raise PixelIdentityError(
                f"Duplicate NGFF label node: {label_node_path}"
            )
        label_attrs = _read_attrs(_node_directory(root, label_node_path))
        if "multiscales" not in label_attrs:
            raise PixelIdentityError(
                f"NGFF label node has no multiscales metadata: {label_node_path}"
            )
        discovered.append(NgffNode(
            node_path=label_node_path,
            role="label",
            parent_image_node_path=image_node_path,
        ))
        seen.add(label_node_path)
    return discovered


def discover_ngff_nodes(zarr_root: str | Path) -> tuple[NgffNode, ...]:
    """Discover NGFF 0.4 image-level nodes by declared image/plate metadata."""
    root = Path(zarr_root)
    attrs = _read_attrs(root)
    image_paths: list[str] = []

    if "multiscales" in attrs:
        image_paths.append(".")
    elif "plate" in attrs:
        plate = attrs["plate"]
        wells = plate.get("wells") if isinstance(plate, dict) else None
        if not isinstance(wells, list):
            raise PixelIdentityError("NGFF plate metadata has no wells list")
        for well in wells:
            well_path = well.get("path") if isinstance(well, dict) else None
            if not isinstance(well_path, str):
                raise PixelIdentityError("NGFF plate well path is missing")
            well_path = _safe_node_path(well_path)
            well_attrs = _read_attrs(_node_directory(root, well_path))
            well_metadata = well_attrs.get("well")
            images = (
                well_metadata.get("images")
                if isinstance(well_metadata, dict)
                else None
            )
            if not isinstance(images, list):
                raise PixelIdentityError(
                    f"NGFF well metadata has no images list: {well_path}"
                )
            for image in images:
                image_path = image.get("path") if isinstance(image, dict) else None
                if not isinstance(image_path, str):
                    raise PixelIdentityError(
                        f"NGFF well image path is missing: {well_path}"
                    )
                image_paths.append(_join_node_path(
                    well_path,
                    _safe_node_path(image_path),
                ))
    else:
        raise PixelIdentityError(
            "Returned Zarr root is neither an NGFF image nor an NGFF plate"
        )

    nodes: list[NgffNode] = []
    seen_images = set()
    for image_path in image_paths:
        if image_path in seen_images:
            raise PixelIdentityError(f"Duplicate NGFF image node: {image_path}")
        image_attrs = _read_attrs(_node_directory(root, image_path))
        if "multiscales" not in image_attrs:
            raise PixelIdentityError(
                f"NGFF image node has no multiscales metadata: {image_path}"
            )
        nodes.append(NgffNode(node_path=image_path, role="image"))
        nodes.extend(_discover_labels(root, image_path))
        seen_images.add(image_path)
    return tuple(nodes)


def find_returned_zarr_stores(results_path: str | Path) -> tuple[Path, ...]:
    """Find outermost returned ``*.zarr`` stores and prune their internals."""
    base = Path(results_path)
    stores = []
    for current, dirs, _files in os.walk(base):
        zarr_dirs = sorted(name for name in dirs if name.lower().endswith(".zarr"))
        stores.extend(Path(current) / name for name in zarr_dirs)
        dirs[:] = [name for name in dirs if not name.lower().endswith(".zarr")]
    return tuple(stores)


def _identity_for_node(
    root: Path,
    node: NgffNode,
    identity_provider: IsccBioIdentityProvider,
) -> PixelIdentity:
    guard = read_zarr_v2_semantic_guard(root, node.node_path)
    return identity_provider.generate(
        root,
        node_path=node.node_path,
        role=node.role,
        shape=guard.shape,
        dtype=guard.dtype,
        axes=guard.axes,
        coordinate_transformations=guard.coordinate_transformations,
    )


def evaluate_returned_zarr(
    zarr_root: str | Path,
    canonical_inputs: CanonicalInputManifest | None,
    *,
    identity_provider: IsccBioIdentityProvider | None = None,
) -> ReturnedZarrDecision:
    """Compare one returned image store with its workflow-scoped inputs.

    The function only reports eligibility.  It never changes the returned
    store, which makes failures and unsupported cases unconditionally fail
    open to ``keep-full``.
    """
    root = Path(zarr_root)
    if canonical_inputs is None or not canonical_inputs.inputs:
        return ReturnedZarrDecision(
            store_path=root,
            outcome="keep-full",
            reason="no-canonical-input-snapshot",
        )

    try:
        nodes = discover_ngff_nodes(root)
    except PixelIdentityError as exc:
        return ReturnedZarrDecision(
            store_path=root,
            outcome="keep-full",
            reason=f"unsupported-ngff: {exc}",
        )

    image_nodes = tuple(node for node in nodes if node.role == "image")
    label_paths = tuple(node.node_path for node in nodes if node.role == "label")
    if len(image_nodes) != 1:
        return ReturnedZarrDecision(
            store_path=root,
            outcome="keep-full",
            reason="unsupported-multi-image-result",
            label_node_paths=label_paths,
        )
    if not label_paths:
        return ReturnedZarrDecision(
            store_path=root,
            outcome="keep-full",
            reason="no-label-nodes",
        )

    provider = identity_provider or IsccBioIdentityProvider()
    try:
        returned_identity = _identity_for_node(root, image_nodes[0], provider)
    except PixelIdentityError as exc:
        return ReturnedZarrDecision(
            store_path=root,
            outcome="keep-full",
            reason=f"identity-unavailable: {exc}",
            label_node_paths=label_paths,
        )

    artifact_matches = tuple(
        item for item in canonical_inputs.inputs
        if item.transfer_artifact == root.name
    )
    if len(artifact_matches) > 1:
        reason = "ambiguous-transfer-artifact"
        matches = ()
    elif len(artifact_matches) == 1:
        matches = artifact_matches
        reason = (
            "input-image-unchanged"
            if pixel_identities_match(
                returned_identity,
                matches[0].source.pixel_identity,
            )
            else "pixels-changed"
        )
    else:
        matches = tuple(
            item for item in canonical_inputs.inputs
            if pixel_identities_match(
                returned_identity,
                item.source.pixel_identity,
            )
        )
        if len(matches) == 1:
            reason = "input-image-unchanged"
        elif len(matches) > 1:
            reason = "ambiguous-input-identity"
            matches = ()
        else:
            reason = "no-input-identity-match"

    eligible = reason == "input-image-unchanged"
    return ReturnedZarrDecision(
        store_path=root,
        outcome="eligible" if eligible else "keep-full",
        reason=reason,
        image_identities=(returned_identity,),
        label_node_paths=label_paths,
        matched_inputs=matches if eligible else (),
    )


__all__ = [
    "NgffNode",
    "ReturnedZarrDecision",
    "discover_ngff_nodes",
    "evaluate_returned_zarr",
    "find_returned_zarr_stores",
]
