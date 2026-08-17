import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).parents[2]
    / "biomero_importer"
    / "utils"
    / "pixel_identity.py"
)
SPEC = importlib.util.spec_from_file_location("pixel_identity", MODULE_PATH)
pixel_identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pixel_identity)

IsccBioIdentityProvider = pixel_identity.IsccBioIdentityProvider
PixelIdentityError = pixel_identity.PixelIdentityError
read_zarr_v2_semantic_guard = pixel_identity.read_zarr_v2_semantic_guard
pixel_identities_match = pixel_identity.pixel_identities_match


def biocode_result(code="ISCC:KSUM", data="ISCC:GDATA", instance="ISCC:IINSTANCE"):
    return {"iscc_code": code, "units": [data, instance]}


def write_json(path, value):
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_ngff_v2_image(root, node_path="."):
    node = root if node_path == "." else root / node_path
    write_json(node / ".zgroup", {"zarr_format": 2})
    write_json(
        node / ".zattrs",
        {
            "multiscales": [
                {
                    "version": "0.4",
                    "axes": [
                        {"name": "t", "type": "time"},
                        {"name": "c", "type": "channel"},
                        {"name": "z", "type": "space"},
                        {"name": "y", "type": "space"},
                        {"name": "x", "type": "space"},
                    ],
                    "datasets": [
                        {
                            "path": "0",
                            "coordinateTransformations": [
                                {"type": "scale", "scale": [1, 1, 2, 0.5, 0.5]}
                            ],
                        },
                        {"path": "1"},
                    ],
                }
            ]
        },
    )
    write_json(
        node / "0" / ".zarray",
        {"zarr_format": 2, "shape": [1, 2, 3, 16, 32], "dtype": "<u2"},
    )
    return node


def test_reads_ngff_04_zarr_v2_semantic_guard(tmp_path):
    root = tmp_path / "input.ome.zarr"
    make_ngff_v2_image(root)

    guard = read_zarr_v2_semantic_guard(root, ".")

    assert guard.shape == (1, 2, 3, 16, 32)
    assert guard.dtype == "uint16"
    assert guard.axes == ("t", "c", "z", "y", "x")
    assert guard.coordinate_transformations == (
        {"type": "scale", "scale": [1, 1, 2, 0.5, 0.5]},
    )
    assert guard.ngff_version == "0.4"
    assert guard.zarr_format == 2


def test_reads_nested_plate_image_node(tmp_path):
    root = tmp_path / "plate.ome.zarr"
    make_ngff_v2_image(root, "A/1/0")

    guard = read_zarr_v2_semantic_guard(root, "A/1/0")

    assert guard.shape == (1, 2, 3, 16, 32)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda node: (node / ".zattrs").unlink(), "metadata"),
        (
            lambda node: write_json(
                node / ".zattrs", {"multiscales": [{"version": "0.5"}]}
            ),
            "NGFF 0.4",
        ),
        (
            lambda node: write_json(
                node / "0" / ".zarray",
                {"zarr_format": 3, "shape": [8, 8], "dtype": "uint8"},
            ),
            "Zarr v2",
        ),
    ],
)
def test_semantic_guard_fails_closed_for_unsupported_metadata(
    tmp_path, mutate, message
):
    root = tmp_path / "input.ome.zarr"
    node = make_ngff_v2_image(root)
    mutate(node)

    with pytest.raises(PixelIdentityError, match=message):
        read_zarr_v2_semantic_guard(root, ".")


def test_builds_identity_from_public_iscc_bio_api(tmp_path):
    zarr = tmp_path / "input.ome.zarr"
    zarr.mkdir()
    calls = []

    def generate(source, *, source_type):
        calls.append((source, source_type))
        return [biocode_result()]

    provider = IsccBioIdentityProvider(
        generate_biocode=generate,
        tool_version="0.1.0",
    )

    identity = provider.generate(
        zarr,
        node_path=".",
        role="image",
        shape=(1, 2, 3, 16, 32),
        dtype="uint16",
        axes=("t", "c", "z", "y", "x"),
        coordinate_transformations=({"type": "scale", "scale": [1, 1, 1, 2, 2]},),
    )

    assert calls == [(zarr, "zarr")]
    assert identity.iscc_code == "ISCC:KSUM"
    assert identity.data_code == "ISCC:GDATA"
    assert identity.instance_code == "ISCC:IINSTANCE"
    assert identity.tool_version == "0.1.0"
    assert identity.imagewalk_revision == (
        "iscc-bio/0.1.0@c536d7699b7d25592bfe5c91c947b749344b6914"
    )
    assert identity.node_path == "."
    assert identity.shape == (1, 2, 3, 16, 32)


def test_targets_an_explicit_nested_zarr_node(tmp_path):
    root = tmp_path / "plate.ome.zarr"
    node = root / "A" / "1" / "0"
    node.mkdir(parents=True)
    calls = []
    provider = IsccBioIdentityProvider(
        generate_biocode=lambda source, *, source_type: (
            calls.append((source, source_type)) or [biocode_result()]
        ),
        tool_version="0.1.0",
    )

    identity = provider.generate(
        root,
        node_path="A/1/0",
        role="image",
        shape=(1, 1, 1, 8, 8),
        dtype="uint8",
        axes=("t", "c", "z", "y", "x"),
    )

    assert calls == [(node, "zarr")]
    assert identity.node_path == "A/1/0"


@pytest.mark.parametrize("node_path", ["../escape", "/absolute", r"A\1\0"])
def test_rejects_unsafe_node_path(tmp_path, node_path):
    provider = IsccBioIdentityProvider(
        generate_biocode=lambda *args, **kwargs: [biocode_result()],
        tool_version="0.1.0",
    )

    with pytest.raises(PixelIdentityError, match="node path"):
        provider.generate(
            tmp_path,
            node_path=node_path,
            role="image",
            shape=(8, 8),
            dtype="uint8",
            axes=("y", "x"),
        )


def test_rejects_missing_or_ambiguous_scene(tmp_path):
    zarr = tmp_path / "input.ome.zarr"
    zarr.mkdir()

    for results in ([], [biocode_result(), biocode_result(code="ISCC:KOTHER")]):
        provider = IsccBioIdentityProvider(
            generate_biocode=lambda *args, _results=results, **kwargs: _results,
            tool_version="0.1.0",
        )
        with pytest.raises(PixelIdentityError, match="exactly one scene"):
            provider.generate(
                zarr,
                node_path=".",
                role="image",
                shape=(8, 8),
                dtype="uint8",
                axes=("y", "x"),
            )


def test_rejects_unexpected_upstream_result(tmp_path):
    zarr = tmp_path / "input.ome.zarr"
    zarr.mkdir()
    provider = IsccBioIdentityProvider(
        generate_biocode=lambda *args, **kwargs: [
            {"iscc_code": "ISCC:KSUM", "units": ["ISCC:GDATA"]}
        ],
        tool_version="0.1.0",
    )

    with pytest.raises(PixelIdentityError, match="Data and Instance"):
        provider.generate(
            zarr,
            node_path=".",
            role="image",
            shape=(8, 8),
            dtype="uint8",
            axes=("y", "x"),
        )


def test_exact_match_requires_instance_code_and_semantic_guard(tmp_path):
    zarr = tmp_path / "input.ome.zarr"
    zarr.mkdir()
    provider = IsccBioIdentityProvider(
        generate_biocode=lambda *args, **kwargs: [biocode_result()],
        tool_version="0.1.0",
    )
    first = provider.generate(
        zarr,
        node_path=".",
        role="image",
        shape=(8, 8),
        dtype="uint8",
        axes=("y", "x"),
    )
    same = first.model_copy(update={"iscc_code": "ISCC:KDIFFERENT"})
    changed_instance = first.model_copy(update={"instance_code": "ISCC:IDIFFERENT"})
    changed_shape = first.model_copy(update={"shape": (4, 16)})

    assert pixel_identities_match(first, same)
    assert not pixel_identities_match(first, changed_instance)
    assert not pixel_identities_match(first, changed_shape)


def test_default_provider_loads_upstream_lazily(monkeypatch):
    imported = []

    def fail_import(name):
        imported.append(name)
        raise ImportError("not installed")

    monkeypatch.setattr(pixel_identity, "import_module", fail_import)
    provider = IsccBioIdentityProvider()

    with pytest.raises(PixelIdentityError, match="identity extra"):
        provider.generate(
            Path("missing.zarr"),
            node_path=".",
            role="image",
            shape=(8, 8),
            dtype="uint8",
            axes=("y", "x"),
        )

    assert imported == ["iscc_bio.api"]
