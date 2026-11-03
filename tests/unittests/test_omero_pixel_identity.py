"""Reader parity regressions using real ISCC-BIO hashing and BioIO decoding."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


SPEC = importlib.util.spec_from_file_location(
    "omero_pixel_identity",
    Path(__file__).parents[2] / "biomero_importer/utils/pixel_identity.py",
)
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


def source(data, pixel_type):
    """Model OMERO's big-endian wire bytes and gateway's decoded plane API."""
    from omero.gateway import PixelsWrapper

    state = {"positions": [], "tiles": [], "closed": False,
             "opened": 0, "closed_count": 0}

    class Store:
        def __init__(self):
            state["opened"] += 1

        def setPixelsId(self, *_args):
            pass

        def getPlane(self, z, c, t):
            raise AssertionError("Whole-plane RPC must not be used")

        def getTile(self, z, c, t, x, y, width, height):
            state["positions"].append((z, c, t))
            state["tiles"].append((x, y, width, height))
            assert width * height * data.dtype.itemsize <= 256 * 1024
            tile = data[t, c, z, y:y + height, x:x + width]
            return tile.astype(data.dtype.newbyteorder(">")).tobytes()

        def close(self):
            state["closed"] = True
            state["closed_count"] += 1

    class Pixels:
        sizeY, sizeX = data.shape[-2:]

        def getId(self):
            return 101

        def getPixelsType(self):
            return SimpleNamespace(
                getValue=lambda: pixel_type, value=pixel_type
            )

        def get_numpy_type(self):
            return data.dtype.type

        def getPlanes(self, positions):
            # Exercise the actual gateway decoding, not a second local codec.
            yield from PixelsWrapper.getPlanes(self, positions)

        def getTiles(self, positions):
            yield from PixelsWrapper.getTiles(self, positions)

        def _prepareRawPixelsStore(self):
            return Store()

    pixels = Pixels()
    image = SimpleNamespace(
        getId=lambda: 17, getName=lambda: "reader parity",
        getPrimaryPixels=lambda: pixels,
        **{f"getSize{axis}": lambda n=n: n
           for axis, n in zip("TCZYX", data.shape)},
    )
    connection = SimpleNamespace(
        getObject=lambda kind, ident: image,
        c=SimpleNamespace(sf=SimpleNamespace(createRawPixelsStore=Store)),
    )
    return connection, pixels, state


def semantics(data):
    return dict(node_path=".", role="image", shape=data.shape,
                dtype=data.dtype.name, axes=("t", "c", "z", "y", "x"))


@pytest.mark.parametrize("dtype,pixel_type", [
    ("u1", "uint8"), ("i1", "int8"), ("u2", "uint16"), ("i2", "int16"),
    ("u4", "uint32"), ("i4", "int32"), ("f4", "float"), ("f8", "double"),
])
def test_omero_identity_matches_bioio_for_wire_pixels(
    tmp_path, dtype, pixel_type
):
    pytest.importorskip("iscc_bio")
    zarr = pytest.importorskip("zarr")
    data = np.arange(96).reshape(2, 2, 2, 3, 4).astype(dtype)
    if data.dtype.kind == "f":
        data = data / 4 - 3.5
    connection, _pixels, state = source(data, pixel_type)
    root = tmp_path / "image.ome.zarr"
    group = zarr.open_group(root, mode="w", zarr_format=2)
    group.create_array("0", data=data, chunks=(1, 1, 1, 3, 4))
    group.attrs["multiscales"] = [{
        "version": "0.4", "axes": [
            {"name": axis, "type": kind} for axis, kind in zip(
                "tczyx", ("time", "channel", "space", "space", "space"))],
        "datasets": [{"path": "0"}],
    }]
    provider = identity.IsccBioIdentityProvider()
    exported = provider.generate(root, **semantics(data))
    original = provider.generate_omero(
        connection, image_id=17, **semantics(data)
    )

    assert identity.pixel_identities_match(original, exported)
    assert original.instance_code == exported.instance_code
    assert state["positions"] == [(z, c, t) for z in range(2)
                                  for c in range(2) for t in range(2)]
    assert state["closed"]
    assert state["opened"] == state["closed_count"] == 8
    assert "omero-gateway-tiles-v1" in original.imagewalk_revision
    assert "omero-gateway-tiles-v1" not in exported.imagewalk_revision
    assert provider.generate(root, **semantics(data)) == exported

    data[0, 0, 0, 0, 0] += 1
    changed = provider.generate_omero(
        connection, image_id=17, **semantics(data)
    )
    assert not identity.pixel_identities_match(changed, exported)


@pytest.mark.parametrize("failure", [
    "short", "shape", "dtype", "exception", "extra",
])
def test_omero_plane_read_fails_closed_and_closes_iterator(failure):
    pytest.importorskip("iscc_bio")
    data = np.arange(24, dtype="uint16").reshape(1, 2, 1, 3, 4)
    connection, pixels, state = source(data, "uint16")

    def broken(positions):
        try:
            _z, c, _t, _tile = list(positions)[0]
            if c == 0:
                yield data[0, 0, 0]
                return
            if failure == "shape":
                yield np.zeros((4, 3), dtype="uint16")
            elif failure == "dtype":
                yield data[0, 1, 0].astype("uint8")
            elif failure == "exception":
                raise OSError("read disconnected")
            elif failure == "extra":
                yield data[0, 1, 0]
                yield data[0, 1, 0]
            # "short" mimics gateway getPlanes swallowing a transport error.
        finally:
            state["closed"] = True

    pixels.getTiles = broken
    with pytest.raises((identity.PixelIdentityError, OSError)):
        identity.IsccBioIdentityProvider().generate_omero(
            connection, image_id=17, **semantics(data))
    assert state["closed"]


def test_gateway_raw_read_failure_closes_store():
    pytest.importorskip("iscc_bio")
    data = np.arange(24, dtype="uint16").reshape(1, 2, 1, 3, 4)
    connection, pixels, state = source(data, "uint16")
    prepare = pixels._prepareRawPixelsStore

    def prepare_with_later_read_failure():
        store = prepare()
        read = store.getTile

        def fail_second_channel(z, c, t, *region):
            if c == 1:
                raise OSError("socket dropped")
            return read(z, c, t, *region)

        store.getTile = fail_second_channel
        return store

    pixels._prepareRawPixelsStore = prepare_with_later_read_failure
    with pytest.raises((identity.PixelIdentityError, OSError)):
        identity.IsccBioIdentityProvider().generate_omero(
            connection, image_id=17, **semantics(data))
    assert state["opened"] == state["closed_count"] == 2


def test_upstream_hash_failure_closes_gateway_iterator(monkeypatch):
    upstream = pytest.importorskip("iscc_bio.biocode")
    data = np.arange(24, dtype="uint16").reshape(1, 2, 1, 3, 4)
    connection, _pixels, state = source(data, "uint16")

    def fail_after_first_plane(planes):
        next(planes)
        raise RuntimeError("hash failed")

    monkeypatch.setattr(upstream, "generate_biocode", fail_after_first_plane)
    with pytest.raises(RuntimeError, match="hash failed"):
        identity.IsccBioIdentityProvider().generate_omero(
            connection, image_id=17, **semantics(data))
    assert state["closed"]
    assert state["opened"] == state["closed_count"] == 1


def test_omero_missing_image_fails_closed():
    pytest.importorskip("iscc_bio")
    connection = SimpleNamespace(getObject=lambda *_args: None)
    with pytest.raises(identity.PixelIdentityError, match="Image"):
        identity.IsccBioIdentityProvider().generate_omero(
            connection, image_id=17, node_path=".", role="image",
            shape=(3, 4), dtype="uint16", axes=("y", "x"))
