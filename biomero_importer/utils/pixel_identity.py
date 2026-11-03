"""Compatibility adapter; filesystem identity lives in biomero-shallower."""
from typing import Any, Literal, Mapping, Sequence
from importlib import import_module
from contextlib import closing

import numpy as np

from biomero_schema.zarr import PixelIdentity
from biomero_shallower.pixel_identity import *  # noqa: F401,F403
from biomero_shallower.pixel_identity import (
    IsccBioIdentityProvider as FilesystemIdentityProvider,
    PixelIdentityError,
    _validate_node_path,
    os,
)


# Leave room for Ice protocol overhead even with a 1 MiB message limit.
_MAX_TILE_BYTES = 256 * 1024
_MAX_TILE_SIDE = 512


def _tile_regions(size_y, size_x, dtype):
    """Cover a plane with bounded, non-overlapping OMERO tile requests."""
    max_pixels = _MAX_TILE_BYTES // dtype.itemsize
    width = min(size_x, _MAX_TILE_SIDE, max_pixels)
    height = min(size_y, _MAX_TILE_SIDE, max_pixels // width)
    for y in range(0, size_y, height):
        for x in range(0, size_x, width):
            yield x, y, min(width, size_x - x), min(height, size_y - y)


def _read_gateway_tile(pixels, z, c, t, region, dtype):
    # Fully exhaust each request before validation/hashing. Gateway getTiles
    # closes its store after iteration, not in finally: closing a suspended
    # multi-tile generator on an error can leak that store.
    with closing(pixels.getTiles([(z, c, t, region)])) as tiles:
        decoded = list(tiles)
    if len(decoded) != 1:
        # Some gateway versions log/swallow read errors. Never hash an
        # incomplete stream, or silently accept an unexpected extra tile.
        raise PixelIdentityError(
            f"Expected one OMERO Image tile at Z={z}, C={c}, T={t}, "
            f"region={region}; received {len(decoded)}"
        )
    array = np.asarray(decoded[0])
    _x, _y, width, height = region
    if array.shape != (height, width):
        raise PixelIdentityError("OMERO Image tile shape mismatch")
    if array.dtype.name != dtype.name:
        raise PixelIdentityError("OMERO Image tile dtype mismatch")
    return array


def _gateway_planes(image, plane_type):
    """Yield complete decoded OMERO planes in IMAGEWALK Z/C/T order.

    The gateway decodes RawPixelsStore's big-endian wire representation.
    ISCC-BIO 0.2.0's Blitz reader incorrectly interprets it as native-endian.
    Hashing and canonical byte normalization remain entirely upstream.
    """
    pixels = image.getPrimaryPixels()
    size_z, size_c, size_t, size_y, size_x = (
        int(getattr(image, f"getSize{axis}")()) for axis in "ZCTYX"
    )
    if min(size_z, size_c, size_t, size_y, size_x) < 1:
        raise PixelIdentityError("OMERO Image dimensions must be positive")
    dtype = np.dtype(pixels.get_numpy_type())

    def positions():
        return ((z, c, t) for z in range(size_z)
                for c in range(size_c) for t in range(size_t))

    for z, c, t in positions():
        # Keep the upstream full-plane hashing contract; bound the Ice replies.
        array = np.empty((size_y, size_x), dtype=dtype, order="C")
        for region in _tile_regions(size_y, size_x, dtype):
            x, y, width, height = region
            tile = _read_gateway_tile(pixels, z, c, t, region, dtype)
            array[y:y + height, x:x + width] = tile
        yield plane_type(array, 0, z, c, t)


def _generate_gateway_identity(connection, image_id, tool_version):
    image = connection.getObject("Image", image_id)
    if image is None:
        raise PixelIdentityError(f"OMERO Image {image_id} is unavailable")
    generate = import_module("iscc_bio.biocode").generate_biocode
    plane_type = import_module("iscc_bio.imagewalk.common").Plane
    with closing(_gateway_planes(image, plane_type)) as planes:
        parts = generate(planes)
    return {
        "generator": f"iscc-bio - v{tool_version}; omero-gateway-tiles-v1",
        "parts": parts,
    }


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
        """Hash gateway-decoded pixels with upstream IMAGEWALK hashing."""
        _validate_node_path(node_path)
        if (
            not isinstance(image_id, int)
            or isinstance(image_id, bool)
            or image_id < 1
        ):
            raise PixelIdentityError("OMERO image ID must be a positive integer")
        generate, tool_version = self._load_upstream()
        if self._generate_biocode is not None:
            # Preserve the explicit custom-provider API; never fall back to
            # ISCC-BIO's faulty default Blitz reader after a gateway failure.
            result = generate(conn=connection, iid=image_id)
        else:
            result = _generate_gateway_identity(
                connection, image_id, tool_version
            )
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
