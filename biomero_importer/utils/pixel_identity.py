"""Compatibility adapter; filesystem identity lives in biomero-shallower."""
from typing import Any, Literal, Mapping, Sequence
from importlib import import_module

from biomero_schema.zarr import PixelIdentity
from biomero_shallower.pixel_identity import *  # noqa: F401,F403
from biomero_shallower.pixel_identity import (
    IsccBioIdentityProvider as FilesystemIdentityProvider,
    _validate_node_path,
    os,
)


class IsccBioIdentityProvider(FilesystemIdentityProvider):
    """Add the OMERO source adapter to the shared filesystem provider."""

    def _import_upstream(self):
        return import_module("iscc_bio.api")

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
        if (
            not isinstance(image_id, int)
            or isinstance(image_id, bool)
            or image_id < 1
        ):
            raise PixelIdentityError("OMERO image ID must be a positive integer")
        generate, tool_version = self._load_upstream()
        result = generate(conn=connection, iid=image_id)
        return self._build_identity(
            result,
            source_description=f"OMERO Image {image_id}",
            tool_version=tool_version,
            node_path=node_path,
            role=role,
            shape=shape,
            dtype=dtype,
            axes=axes,
            coordinate_transformations=coordinate_transformations,
        )
