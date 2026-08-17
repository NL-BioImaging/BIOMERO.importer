"""ISCC-BIO adapter for BIOMERO pixel identity contracts.

Hashing and IMAGEWALK traversal stay in the upstream ``iscc-bio`` package. This
module only selects one explicit image node, validates the public API result,
and combines it with the semantic guard required by BIOMERO's shared contract.
"""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping, Sequence

from biomero_schema.zarr import PixelIdentity


BiocodeCallable = Callable[..., Sequence[Mapping[str, Any]]]
ISCC_BIO_GIT_REVISION = "c536d7699b7d25592bfe5c91c947b749344b6914"


class PixelIdentityError(RuntimeError):
    """An exact pixel identity could not be calculated unambiguously."""


def _validate_node_path(node_path: str) -> PurePosixPath:
    if not node_path or "\\" in node_path:
        raise PixelIdentityError(f"Unsafe Zarr node path: {node_path!r}")
    parsed = PurePosixPath(node_path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise PixelIdentityError(f"Unsafe Zarr node path: {node_path!r}")
    return parsed


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
        results = list(generate(target, source_type="zarr"))
        if len(results) != 1:
            raise PixelIdentityError(
                "An explicit Zarr node must produce exactly one scene; "
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
    "pixel_identities_match",
]
