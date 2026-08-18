"""ISCC-BIO adapter for BIOMERO pixel identity contracts.

Hashing and IMAGEWALK traversal stay in the upstream ``iscc-bio`` package. This
module only selects one explicit image node, validates the public API result,
and combines it with the semantic guard required by BIOMERO's shared contract.
"""

from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping, Sequence

from biomero_schema.zarr import PixelIdentity
import numpy as np


BiocodeCallable = Callable[..., Sequence[Mapping[str, Any]]]
ISCC_BIO_GIT_REVISION = "c536d7699b7d25592bfe5c91c947b749344b6914"


class PixelIdentityError(RuntimeError):
    """An exact pixel identity could not be calculated unambiguously."""


@dataclass(frozen=True)
class ZarrNodeSemanticGuard:
    """Relevant metadata for the highest-resolution array of one NGFF node."""

    shape: tuple[int, ...]
    dtype: str
    axes: tuple[str, ...]
    coordinate_transformations: tuple[dict[str, Any], ...]
    ngff_version: str
    zarr_format: int


def _validate_node_path(node_path: str) -> PurePosixPath:
    if not node_path or "\\" in node_path:
        raise PixelIdentityError(f"Unsafe Zarr node path: {node_path!r}")
    parsed = PurePosixPath(node_path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise PixelIdentityError(f"Unsafe Zarr node path: {node_path!r}")
    return parsed


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PixelIdentityError(
            f"Cannot read {description} metadata: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise PixelIdentityError(f"Invalid {description} metadata: {path}")
    return value


def read_zarr_v2_semantic_guard(
    zarr_root: str | Path, node_path: str
) -> ZarrNodeSemanticGuard:
    """Read the supported NGFF 0.4/Zarr v2 guard for one explicit image node."""
    parsed_node = _validate_node_path(node_path)
    node = Path(zarr_root)
    if node_path != ".":
        node = node.joinpath(*parsed_node.parts)

    attributes = _read_json_object(node / ".zattrs", "NGFF image")
    multiscales = attributes.get("multiscales")
    if not isinstance(multiscales, list) or len(multiscales) != 1:
        raise PixelIdentityError(
            "NGFF image metadata must contain exactly one multiscales entry"
        )
    multiscale = multiscales[0]
    if not isinstance(multiscale, dict) or multiscale.get("version") != "0.4":
        raise PixelIdentityError("Only NGFF 0.4 image metadata is supported")

    raw_axes = multiscale.get("axes")
    if not isinstance(raw_axes, list) or not raw_axes:
        raise PixelIdentityError("NGFF 0.4 image metadata has no named axes")
    axes = []
    for axis in raw_axes:
        name = axis.get("name") if isinstance(axis, dict) else axis
        if not isinstance(name, str) or not name:
            raise PixelIdentityError("NGFF 0.4 image metadata has an invalid axis")
        axes.append(name)

    datasets = multiscale.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise PixelIdentityError("NGFF 0.4 image metadata has no datasets")
    highest_resolution = datasets[0]
    if not isinstance(highest_resolution, dict):
        raise PixelIdentityError("NGFF 0.4 dataset metadata is invalid")
    dataset_path = highest_resolution.get("path")
    if not isinstance(dataset_path, str):
        raise PixelIdentityError("NGFF 0.4 dataset path is missing")
    parsed_dataset = _validate_node_path(dataset_path)
    array_path = node
    if dataset_path != ".":
        array_path = node.joinpath(*parsed_dataset.parts)

    array = _read_json_object(array_path / ".zarray", "Zarr array")
    if array.get("zarr_format") != 2:
        raise PixelIdentityError("Only Zarr v2 arrays are supported")
    shape = array.get("shape")
    if (
        not isinstance(shape, list)
        or not shape
        or any(not isinstance(size, int) or size < 1 for size in shape)
    ):
        raise PixelIdentityError("Zarr v2 array shape is invalid")
    if len(shape) != len(axes):
        raise PixelIdentityError("Zarr array shape does not match NGFF axes")
    try:
        dtype = np.dtype(array["dtype"]).name
    except (KeyError, TypeError) as exc:
        raise PixelIdentityError("Zarr v2 array dtype is invalid") from exc

    transformations = highest_resolution.get("coordinateTransformations", [])
    if not isinstance(transformations, list) or any(
        not isinstance(item, dict) for item in transformations
    ):
        raise PixelIdentityError("NGFF coordinate transformations are invalid")

    return ZarrNodeSemanticGuard(
        shape=tuple(shape),
        dtype=dtype,
        axes=tuple(axes),
        coordinate_transformations=tuple(dict(item) for item in transformations),
        ngff_version="0.4",
        zarr_format=2,
    )


class IsccBioIdentityProvider:
    """Generate one BIOMERO identity through ``iscc_bio.api.biocode``.

    ``generate_biocode`` and ``tool_version`` are injectable so callers can test
    orchestration without importing the sizeable reader stack. Production use
    loads the public API lazily from the optional ``identity`` dependency.
    """

    def __init__(
        self,
        *,
        generate_biocode: BiocodeCallable | None = None,
        tool_version: str | None = None,
        imagewalk_revision: str | None = None,
    ) -> None:
        self._generate_biocode = generate_biocode
        self._tool_version = tool_version
        self._imagewalk_revision = imagewalk_revision

    def _load_upstream(self) -> tuple[BiocodeCallable, str]:
        generate = self._generate_biocode
        if generate is None:
            try:
                generate = import_module("iscc_bio.api").biocode
            except ImportError as exc:
                raise PixelIdentityError(
                    "ISCC-BIO is unavailable; install biomero-importer with "
                    "the identity extra"
                ) from exc

        tool_version = self._tool_version
        if tool_version is None:
            try:
                tool_version = version("iscc-bio")
            except PackageNotFoundError as exc:
                raise PixelIdentityError(
                    "Cannot determine the installed iscc-bio version"
                ) from exc
        return generate, tool_version

    def generate(
        self,
        zarr_root: str | Path,
        *,
        node_path: str,
        role: Literal["image", "label"],
        shape: Sequence[int],
        dtype: str,
        axes: Sequence[str],
        coordinate_transformations: Sequence[Mapping[str, Any]] = (),
    ) -> PixelIdentity:
        """Hash exactly one Zarr image node and attach its semantic guard."""
        parsed_node = _validate_node_path(node_path)
        target = Path(zarr_root)
        if node_path != ".":
            target = target.joinpath(*parsed_node.parts)

        generate, tool_version = self._load_upstream()
        # Use iscc-bio's BioIO IMAGEWALK implementation for explicit image
        # nodes.  iscc-bio 0.1.0's direct NGFF reader mistakes the numeric
        # pyramid arrays of a normal multiscale image for bioformats2raw
        # series. BioIO follows the same upstream IMAGEWALK contract without
        # duplicating hashing or traversal here.
        results = list(generate(target, source_type="bioio"))
        return self._build_identity(
            results,
            source_description="An explicit Zarr node",
            tool_version=tool_version,
            node_path=node_path,
            role=role,
            shape=shape,
            dtype=dtype,
            axes=axes,
            coordinate_transformations=coordinate_transformations,
        )

    def generate_omero(
        self,
        connection: Any,
        *,
        image_id: int,
        node_path: str,
        role: Literal["image", "label"],
        shape: Sequence[int],
        dtype: str,
        axes: Sequence[str],
        coordinate_transformations: Sequence[Mapping[str, Any]] = (),
    ) -> PixelIdentity:
        """Hash one OMERO Image through the upstream Blitz IMAGEWALK reader."""
        _validate_node_path(node_path)
        if not isinstance(image_id, int) or isinstance(image_id, bool) or image_id < 1:
            raise PixelIdentityError("OMERO image ID must be a positive integer")
        generate, tool_version = self._load_upstream()
        results = list(generate(conn=connection, iid=image_id))
        return self._build_identity(
            results,
            source_description=f"OMERO Image {image_id}",
            tool_version=tool_version,
            node_path=node_path,
            role=role,
            shape=shape,
            dtype=dtype,
            axes=axes,
            coordinate_transformations=coordinate_transformations,
        )

    def _build_identity(
        self,
        results: Sequence[Mapping[str, Any]],
        *,
        source_description: str,
        tool_version: str,
        node_path: str,
        role: Literal["image", "label"],
        shape: Sequence[int],
        dtype: str,
        axes: Sequence[str],
        coordinate_transformations: Sequence[Mapping[str, Any]],
    ) -> PixelIdentity:
        """Validate one public API result and produce the shared contract."""
        if len(results) != 1:
            raise PixelIdentityError(
                f"{source_description} must produce exactly one scene; "
                f"iscc-bio returned {len(results)}"
            )

        result = results[0]
        units = result.get("units")
        if not isinstance(units, Sequence) or isinstance(units, (str, bytes)):
            units = ()
        if len(units) != 2:
            raise PixelIdentityError(
                "iscc-bio must return ordered Data and Instance code units"
            )

        try:
            return PixelIdentity(
                node_path=node_path,
                role=role,
                iscc_code=result["iscc_code"],
                data_code=units[0],
                instance_code=units[1],
                tool_version=tool_version,
                imagewalk_revision=(
                    self._imagewalk_revision
                    or f"iscc-bio/{tool_version}@{ISCC_BIO_GIT_REVISION}"
                ),
                shape=tuple(shape),
                dtype=dtype,
                axes=tuple(axes),
                coordinate_transformations=tuple(
                    dict(item) for item in coordinate_transformations
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PixelIdentityError(
                "iscc-bio returned an invalid pixel identity result"
            ) from exc


def pixel_identities_match(left: PixelIdentity, right: PixelIdentity) -> bool:
    """Return whether exact bytes and their NGFF semantic guards agree.

    Node paths and aggregate/data codes are intentionally excluded: a raw scene
    and its canonical Zarr node can live at different paths, while the Instance
    Code is the exact byte identity used for deduplication.
    """
    return (
        left.instance_code == right.instance_code
        and left.role == right.role
        and left.shape == right.shape
        and left.dtype == right.dtype
        and left.axes == right.axes
        and left.coordinate_transformations == right.coordinate_transformations
    )


__all__ = [
    "IsccBioIdentityProvider",
    "ISCC_BIO_GIT_REVISION",
    "PixelIdentityError",
    "ZarrNodeSemanticGuard",
    "pixel_identities_match",
    "read_zarr_v2_semantic_guard",
]
