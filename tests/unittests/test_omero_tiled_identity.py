"""Bounded OMERO RPC regressions with actual gateway decoding and hashing."""
import struct

import numpy as np
import pytest

from test_omero_pixel_identity import identity, semantics, source


@pytest.mark.parametrize("dtype,pixel_type", [
    ("u1", "uint8"), ("u2", "uint16"), ("i2", "int16"),
    ("u4", "uint32"), ("f4", "float"), ("f8", "double"),
])
def test_tiled_rpc_identity_matches_bioio_with_edges(
    tmp_path, monkeypatch, dtype, pixel_type
):
    pytest.importorskip("iscc_bio")
    zarr = pytest.importorskip("zarr")
    # Force tiny message limits with both horizontal and vertical edge tiles.
    monkeypatch.setattr(identity, "_MAX_TILE_BYTES", 64, raising=False)
    monkeypatch.setattr(identity, "_MAX_TILE_SIDE", 5, raising=False)
    data = np.arange(2 * 2 * 2 * 7 * 11).reshape(2, 2, 2, 7, 11)
    if np.dtype(dtype).kind in "if":
        data = data - 200
    data = data.astype(dtype)
    if data.dtype.kind == "f":
        data = data / 8
    connection, pixels, state = source(data, pixel_type)
    prepare = pixels._prepareRawPixelsStore

    def prepare_limited():
        store = prepare()
        read = store.getTile

        def limited(z, c, t, x, y, width, height):
            assert width * height * data.dtype.itemsize <= 64
            return read(z, c, t, x, y, width, height)

        store.getTile = limited
        return store

    pixels._prepareRawPixelsStore = prepare_limited
    root = tmp_path / "export.ome.zarr"
    group = zarr.open_group(root, mode="w", zarr_format=2)
    group.create_array("0", data=data, chunks=(1, 1, 1, 3, 4))
    group.attrs["multiscales"] = [{
        "version": "0.4", "axes": [
            {"name": axis, "type": kind} for axis, kind in zip(
                "tczyx", ("time", "channel", "space", "space", "space"))],
        "datasets": [{"path": "0"}],
    }]
    provider = identity.IsccBioIdentityProvider()
    expected = provider.generate(root, **semantics(data))
    actual = provider.generate_omero(
        connection, image_id=17, **semantics(data)
    )
    assert identity.pixel_identities_match(actual, expected)
    assert len(state["tiles"]) > 8
    assert any(width < 5 for _x, _y, width, _height in state["tiles"])
    assert any(y > 0 for _x, y, _width, _height in state["tiles"])
    assert state["opened"] == state["closed_count"] == len(state["tiles"])


@pytest.mark.parametrize("dtype", ["uint8", "uint16", "float64"])
def test_default_request_bound_and_issue_three_geometry(dtype):
    # No large arrays: check all rectangles covering the upstream 568MB plane.
    dtype = np.dtype(dtype)
    rectangles = list(identity._tile_regions(26348, 21549, dtype))
    assert len(rectangles) > 1
    assert sum(width * height for _x, _y, width, height in rectangles) == (
        26348 * 21549
    )
    assert max(width * height * dtype.itemsize
               for _x, _y, width, height in rectangles) <= (
        256 * 1024
    )
    assert all(0 <= x < x + width <= 21549 and
               0 <= y < y + height <= 26348
               for x, y, width, height in rectangles)


def test_later_tile_wire_failure_closes_every_store(monkeypatch):
    pytest.importorskip("iscc_bio")
    monkeypatch.setattr(identity, "_MAX_TILE_SIDE", 3, raising=False)
    data = np.arange(35, dtype="uint16").reshape(1, 1, 1, 5, 7)
    connection, pixels, state = source(data, "uint16")
    prepare = pixels._prepareRawPixelsStore

    def prepare_failing():
        store = prepare()
        read = store.getTile

        def truncated_wire(z, c, t, x, y, width, height):
            wire = read(z, c, t, x, y, width, height)
            return wire[:-1] if x > 0 else wire

        store.getTile = truncated_wire
        return store

    pixels._prepareRawPixelsStore = prepare_failing
    with pytest.raises((struct.error, identity.PixelIdentityError)):
        identity.IsccBioIdentityProvider().generate_omero(
            connection, image_id=17, **semantics(data)
        )
    assert state["opened"] == state["closed_count"] == 2


def test_default_tiles_avoid_simulated_ice_message_limit():
    Ice = pytest.importorskip("Ice")
    upstream = pytest.importorskip("iscc_bio.biocode")
    from iscc_bio.imagewalk.common import Plane

    data = np.arange(513 * 517, dtype="uint16").reshape(1, 1, 1, 513, 517)
    connection, pixels, state = source(data, "uint16")
    prepare = pixels._prepareRawPixelsStore
    state["whole_plane"] = 0
    message_limit = 256 * 1024 + 64
    protocol_overhead = 32
    assert data.nbytes + protocol_overhead > message_limit

    def prepare_limited():
        store = prepare()
        read = store.getTile

        def oversized_plane(*_args):
            state["whole_plane"] += 1
            raise Ice.UnknownLocalException("Ice::MemoryLimitException")

        def bounded_tile(z, c, t, x, y, width, height):
            wire = read(z, c, t, x, y, width, height)
            if len(wire) + protocol_overhead > message_limit:
                raise Ice.UnknownLocalException("Ice::MemoryLimitException")
            return wire

        store.getPlane = oversized_plane
        store.getTile = bounded_tile
        return store

    pixels._prepareRawPixelsStore = prepare_limited
    result = identity.IsccBioIdentityProvider().generate_omero(
        connection, image_id=17, **semantics(data)
    )
    expected = upstream.generate_biocode([Plane(data[0, 0, 0], 0, 0, 0, 0)])
    assert result.instance_code == expected[0]["units"][1]
    assert state["whole_plane"] == 0
    assert state["opened"] == state["closed_count"] == 6
