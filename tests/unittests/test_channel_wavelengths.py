from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import zarr
from omero.model import FamilyI, LengthI, LogicalChannelI, RenderingModelI
from omero.model.enums import UnitsLength
from omero.rtypes import rstring

from biomero_importer.utils import register


def channel_context(count=2):
    conn = MagicMock()
    conn.getPixelsService().createImage.return_value.getValue.return_value = 42
    image = conn.getObject.return_value
    image.id = 42
    image.getPrimaryPixels().getId.return_value = 43
    logical = [LogicalChannelI() for _ in range(count)]
    channels = []
    for obj in logical:
        channel = MagicMock()
        channel.getLogicalChannel.return_value._obj = obj
        channels.append(channel)
    image.getChannels.return_value = channels
    family = FamilyI()
    family.value = rstring("linear")
    model = RenderingModelI()
    model.value = rstring("rgb")
    return conn, image, logical, [family], [model]


@pytest.mark.parametrize("zarr_format", [2, 3])
def test_registration_persists_channel_wavelengths_without_changing_store(
        tmp_path, monkeypatch, zarr_format):
    path = tmp_path / "source.ome.zarr"
    root = zarr.open_group(path, mode="w", zarr_format=zarr_format)
    pixels = np.arange(24, dtype=np.uint16).reshape(1, 2, 1, 3, 4)
    root.create_array("s0", data=pixels)
    attrs = {
        "multiscales": [{
            "axes": [{"name": name} for name in ("t", "c", "z", "y", "x")],
            "datasets": [{"path": "s0", "coordinateTransformations": [
                {"type": "scale", "scale": [1, 1, 1, 1, 1]},
            ]}],
        }],
        "omero": {"channels": [
            {"label": "Green", "excitation_wavelength": 488,
             "emission_wavelength": 520, "emission_wavelength_unit": "nm"},
            {"label": "Red", "excitationWavelength": "561",
             "EmissionWavelength": 0.6,
             "EmissionWavelengthUnit": "micrometer"},
        ]},
    }
    if zarr_format == 3:
        attrs["version"] = "0.5"
        root.attrs["ome"] = attrs
    else:
        attrs["multiscales"][0]["version"] = "0.4"
        root.attrs.update(attrs)
    before = {p.relative_to(path): p.read_bytes()
              for p in path.rglob("*") if p.is_file()}
    conn, image, logical, families, models = channel_context()
    monkeypatch.setattr(register, "load_families", lambda _: families)
    monkeypatch.setattr(register, "load_models", lambda _: models)
    monkeypatch.setattr(register, "set_external_info", lambda *args: None)

    register.register_image(
        conn, zarr.storage.LocalStore(path, read_only=True),
        SimpleNamespace(uri=str(path), name="source"),
    )

    image.getChannels.assert_called_once_with(noRE=True)
    for obj, ex, em in zip(logical, [488, 561], [520, 600]):
        assert obj.excitationWave.getValue() == pytest.approx(ex)
        assert obj.emissionWave.getValue() == pytest.approx(em)
        assert obj.excitationWave.getUnit() == UnitsLength.NANOMETER
        assert obj.emissionWave.getUnit() == UnitsLength.NANOMETER
    conn.getUpdateService().saveAndReturnArray.assert_called_once_with(logical)
    assert {p.relative_to(path): p.read_bytes()
            for p in path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("entry", [
    {}, {"label": "Green"}, {"excitation_wavelength": None},
    {"excitation_wavelength": 0}, {"excitation_wavelength": -1},
    {"excitation_wavelength": True}, {"excitation_wavelength": "unknown"},
    {"excitation_wavelength": float("nan")},
    {"excitation_wavelength": float("inf")},
    {"excitation_wavelength": 488, "excitation_wavelength_unit": "pixels"},
    {"excitation_wavelength": 488, "excitation_wavelength_unit": "unknown"},
])
def test_absent_or_invalid_wavelength_does_not_fetch_or_update_channels(entry):
    conn, image, _, _, _ = channel_context(1)
    register.set_channel_wavelengths(conn, image, {"channels": [entry]})
    image.getChannels.assert_not_called()
    conn.getUpdateService().saveAndReturnArray.assert_not_called()


@pytest.mark.parametrize("unit", ["um", "\u00b5m", "\u03bcm", "MICROMETER"])
def test_unit_conversion_preserves_unspecified_channel_metadata(unit):
    conn, image, logical, _, _ = channel_context(2)
    logical[0].emissionWave = LengthI(650, UnitsLength.NANOMETER)
    metadata = {"channels": [
        {"excitation_wavelength": 0.488, "excitation_wavelength_unit": unit},
        {"label": "Unspecified"},
    ]}
    before = deepcopy(metadata)
    register.set_channel_wavelengths(conn, image, metadata)
    assert logical[0].excitationWave.getValue() == pytest.approx(488)
    assert logical[0].emissionWave.getValue() == 650
    assert logical[1].excitationWave is None
    assert logical[1].emissionWave is None
    saved = conn.getUpdateService().saveAndReturnArray
    saved.assert_called_once_with([logical[0]])
    assert metadata == before


def test_invalid_value_does_not_discard_other_valid_wavelength(caplog):
    conn, image, logical, _, _ = channel_context(1)
    register.set_channel_wavelengths(conn, image, {"channels": [{
        "excitation_wavelength": "not a number", "emission_wavelength": 520,
    }]})
    assert logical[0].excitationWave is None
    assert logical[0].emissionWave.getValue() == 520
    assert "wavelength" in caplog.text.lower()
    conn.getUpdateService().saveAndReturnArray.assert_called_once_with(logical)
