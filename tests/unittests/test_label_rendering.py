from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import zarr

from biomero_importer.utils import register


def label_attrs():
    return {
        "image-label": {"version": "0.4", "source": {"image": "../../"}},
        "multiscales": [{
            "axes": [{"name": name} for name in ("t", "c", "z", "y", "x")],
            "datasets": [
                {"path": "s0", "coordinateTransformations": [
                    {"type": "scale", "scale": [1, 1, 1, 1, 1]}
                ]},
                {"path": "s1"},
            ],
        }],
    }


def fake_connection():
    conn = MagicMock()
    conn.getPixelsService().createImage.return_value.getValue.return_value = 42
    image = conn.getObject.return_value
    image.id = 42
    image.getPrimaryPixels().getId.return_value = 43
    image._obj = MagicMock()
    from omero.model import FamilyI, RenderingModelI
    from omero.rtypes import rstring
    family = FamilyI()
    family.value = rstring("linear")
    model = RenderingModelI()
    model.value = rstring("rgb")
    return conn, [family], [model]


@pytest.mark.parametrize("zarr_format", [2, 3])
@pytest.mark.parametrize("explicit_display", [False, True])
@pytest.mark.parametrize("image_label_metadata", [False, True])
def test_register_label_saves_useful_window_without_changing_pixels(
        tmp_path, monkeypatch, zarr_format, explicit_display,
        image_label_metadata):
    path = tmp_path / "source.ome.zarr"
    root = zarr.open_group(str(path), mode="w", zarr_format=zarr_format)
    node_path = "A/1/0/labels/cells"
    label = root.create_group(node_path)
    attrs = label_attrs()
    if explicit_display:
        attrs["omero"] = {"channels": [{
            "active": False, "color": "FF00FF",
            "window": {"start": 12, "end": 200000},
        }]}
    if not image_label_metadata:
        del attrs["image-label"]
    if zarr_format == 3:
        attrs["version"] = "0.5"
    if zarr_format == 3 and image_label_metadata:
        attrs["image-label"]["version"] = "0.5"
    if zarr_format == 3:
        label.attrs["ome"] = attrs
    else:
        label.attrs.update(attrs)
    pixels = np.zeros((1, 1, 1, 5, 7), dtype=np.uint32)
    pixels[0, 0, 0, 0, 0] = 65536
    pixels[0, 0, 0, -1, -1] = 407272
    label.create_array("s0", data=pixels, chunks=(1, 1, 1, 2, 3))
    # Coarse levels can lose the highest ID: they must not define the range.
    label.create_array("s1", data=np.zeros((1, 1, 1, 2, 3), dtype=np.uint32))
    before = {p.relative_to(path): p.read_bytes()
              for p in path.rglob("*") if p.is_file()}
    conn, families, models = fake_connection()
    monkeypatch.setattr(register, "load_families", lambda _: families)
    monkeypatch.setattr(register, "load_models", lambda _: models)
    monkeypatch.setattr(register, "set_external_info", lambda *args: None)
    args = SimpleNamespace(uri=str(path), name="source_cells")

    register.register_image(
        conn, zarr.storage.LocalStore(str(path), read_only=True), args,
        image_path=node_path,
    )

    saved = conn.getUpdateService().saveAndReturnObject.call_args_list
    rendering = saved[-1].args[0]
    binding = rendering.getChannelBinding(0)
    assert binding.inputStart.val == (12 if explicit_display else 0)
    assert binding.inputEnd.val == (200000 if explicit_display else 407272)
    assert binding.active.val is (not explicit_display)
    assert binding.red.val == binding.blue.val == 255
    assert binding.green.val == (0 if explicit_display else 255)
    assert rendering.pixels.id.val == 43
    assert {p.relative_to(path): p.read_bytes()
            for p in path.rglob("*") if p.is_file()} == before
    np.testing.assert_array_equal(label["s0"][:], pixels)


def test_explicit_label_window_does_not_scan(monkeypatch):
    attrs = label_attrs()
    attrs["omero"] = {"channels": [{
        "label": "Objects", "active": False, "color": "FF00FF",
        "window": {"start": 12, "end": 200000},
    }], "rdefs": {"defaultZ": 1}}
    before = deepcopy(attrs)
    monkeypatch.setattr(register, "load_array", MagicMock(
        side_effect=AssertionError("Valid display metadata must avoid scanning")))
    result = register.label_rendering_metadata(None, attrs)
    assert result == before
    assert attrs == before


@pytest.mark.parametrize("value, expected", [
    (0, (0, 1)), (7, (0, 7)), (407272, (0, 407272)),
    (4294967295, (0, 4294967295)),
])
def test_fallback_reads_chunks_and_handles_constant_labels(
        monkeypatch, value, expected):
    attrs = label_attrs()
    data = np.full((1, 1, 1, 5, 7), value, dtype=np.uint32)
    selections = []

    class ChunkedArray:
        shape = data.shape
        chunks = (1, 1, 1, 2, 3)

        def __getitem__(self, selection):
            result = data[selection]
            assert result.size <= 6
            selections.append(selection)
            return result

    load = MagicMock(return_value=ChunkedArray())
    monkeypatch.setattr(register, "load_array", load)
    result = register.label_rendering_metadata(None, attrs, "A/1/0/labels/cells")
    window = result["omero"]["channels"][0]["window"]
    assert (window["start"], window["end"]) == expected
    assert len(selections) == 9
    load.assert_called_once_with(None, "A/1/0/labels/cells/s0")
    assert "omero" not in attrs


def test_preserves_valid_channel_and_repairs_only_missing_window(monkeypatch):
    attrs = label_attrs()
    supplied = {
        "label": "Existing", "color": "00FF00", "active": False,
        "window": {"start": 1, "end": 99},
    }
    attrs["omero"] = {"channels": [supplied, {"label": "Missing"}]}
    data = np.array([[[[[1, 99]]], [[[0, 90000]]]]], dtype=np.uint32)

    class ChunkedArray:
        shape = data.shape
        chunks = (1, 1, 1, 1, 2)

        def __getitem__(self, selection):
            assert selection[1] == slice(1, 2)
            return data[selection]

    monkeypatch.setattr(register, "load_array", lambda *args: ChunkedArray())
    result = register.label_rendering_metadata(None, attrs)
    assert result["omero"]["channels"][0] == supplied
    repaired = result["omero"]["channels"][1]
    assert repaired["window"] == {"start": 0, "end": 90000}
    assert repaired["label"] == "Missing"
    assert repaired["active"] is True
    assert repaired["color"] == "FFFFFF"


@pytest.mark.parametrize("window", [
    {}, {"start": 0, "end": 0}, {"start": 10, "end": 2},
    {"start": 0, "end": None}, {"start": 0, "end": float("nan")},
])
def test_invalid_windows_get_data_derived_fallback(monkeypatch, window):
    attrs = label_attrs()
    attrs["omero"] = {"channels": [{"window": window}]}
    data = np.array([[[[[0, 123456]]]]], dtype=np.uint32)
    array = MagicMock(shape=data.shape, chunks=data.shape)
    array.__getitem__.side_effect = data.__getitem__
    monkeypatch.setattr(register, "load_array", lambda *args: array)
    result = register.label_rendering_metadata(None, attrs)
    assert result["omero"]["channels"][0]["window"] == {
        "start": 0, "end": 123456
    }


def test_non_label_metadata_is_unchanged(monkeypatch):
    attrs = label_attrs()
    del attrs["image-label"]
    monkeypatch.setattr(register, "load_array", MagicMock(
        side_effect=AssertionError("Ordinary intensities must not be scanned")))
    assert register.label_rendering_metadata(None, attrs) is attrs


def test_two_dimensional_labels_without_channel_axis(monkeypatch):
    attrs = label_attrs()
    attrs["multiscales"][0]["axes"] = ["y", "x"]
    data = np.array([[0, 2, 0], [4, 0, 400000]], dtype=np.uint32)
    array = MagicMock(shape=data.shape, chunks=(1, 2))
    array.__getitem__.side_effect = data.__getitem__
    monkeypatch.setattr(register, "load_array", lambda *args: array)
    result = register.label_rendering_metadata(None, attrs)
    assert len(result["omero"]["channels"]) == 1
    assert result["omero"]["channels"][0]["window"]["end"] == 400000


def test_scan_failure_prevents_image_creation(monkeypatch):
    conn, families, models = fake_connection()
    monkeypatch.setattr(register, "parse_image_metadata",
                        lambda *args: ({"x": 10, "y": 10}, {}, "uint32"))
    monkeypatch.setattr(register, "load_array",
                        MagicMock(side_effect=OSError("Unreadable chunk")))
    with pytest.raises(OSError, match="Unreadable chunk"):
        register.create_image(
            conn, None, label_attrs(), "source_cells", families, models,
            SimpleNamespace(uri="/data/source.zarr/labels/cells"),
        )
    conn.getPixelsService().createImage.assert_not_called()


def test_label_root_uri_without_optional_image_label_metadata(monkeypatch):
    conn, families, models = fake_connection()
    attrs = label_attrs()
    del attrs["image-label"]
    data = np.array([[[[[0, 90000]]]]], dtype=np.uint32)
    array = MagicMock(shape=data.shape, chunks=data.shape)
    array.__getitem__.side_effect = data.__getitem__
    monkeypatch.setattr(register, "load_array", lambda *args: array)
    monkeypatch.setattr(register, "parse_image_metadata",
                        lambda *args: ({"x": 2, "y": 1}, {}, "uint32"))
    monkeypatch.setattr(register, "set_external_info", lambda *args: None)
    _, rendering = register.create_image(
        conn, None, attrs, "source_cells", families, models,
        SimpleNamespace(uri="/data/source.zarr/labels/cells"),
    )
    assert rendering.getChannelBinding(0).inputEnd.val == 90000
