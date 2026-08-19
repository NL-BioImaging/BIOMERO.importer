import json
from pathlib import Path
from uuid import UUID

from biomero_schema.zarr import (
    CanonicalInput,
    CanonicalInputManifest,
    CanonicalZarrSource,
    PixelIdentity,
)

from biomero_importer.utils.result_zarr import (
    discover_ngff_nodes,
    evaluate_returned_zarr,
    find_returned_zarr_stores,
    normalize_returned_zarr,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _make_image(root: Path, node_path=".", labels=()) -> None:
    node = root if node_path == "." else root / node_path
    _write_json(node / ".zgroup", {"zarr_format": 2})
    _write_json(node / ".zattrs", {
        "multiscales": [{
            "version": "0.4",
            "axes": [
                {"name": "t"},
                {"name": "c"},
                {"name": "y"},
                {"name": "x"},
            ],
            "datasets": [{"path": "0"}],
        }],
    })
    _write_json(node / "0" / ".zarray", {
        "zarr_format": 2,
        "shape": [1, 1, 8, 8],
        "chunks": [1, 1, 8, 8],
        "dtype": "<u2",
    })
    (node / "0" / "0.0.0.0").write_bytes(b"image-pixels" * 2048)
    if labels:
        _write_json(node / "labels" / ".zgroup", {"zarr_format": 2})
        _write_json(node / "labels" / ".zattrs", {"labels": list(labels)})
        for label in labels:
            _make_image(root, f"{node_path}/labels/{label}".replace("./", ""))


def _identity(instance="ISCC:IINSTANCE", node_path=".") -> PixelIdentity:
    return PixelIdentity(
        node_path=node_path,
        role="image",
        iscc_code="ISCC:KSUM",
        data_code="ISCC:GDATA",
        instance_code=instance,
        tool_version="0.1.0",
        imagewalk_revision="iscc-bio/0.1.0@revision",
        shape=(1, 1, 8, 8),
        dtype="uint16",
        axes=("t", "c", "y", "x"),
    )


def _manifest(*items) -> CanonicalInputManifest:
    return CanonicalInputManifest(
        workflow_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        export_task_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        inputs=tuple(items),
    )


def _input(ordinal, artifact=None, instance="ISCC:IINSTANCE") -> CanonicalInput:
    identity = _identity(instance)
    return CanonicalInput(
        ordinal=ordinal,
        selected_object_type="Image",
        selected_object_id=ordinal + 1,
        transfer_artifact=artifact,
        source=CanonicalZarrSource(
            storage_root="group-0-data",
            relative_path=f".processed/Image-{ordinal + 1}.ome.zarr",
            node_path=".",
            source_object_type="Image",
            source_object_id=ordinal + 1,
            source_generation=1,
            interchange_profile="ngff-0.4-zarr-v2",
            pixel_identity=identity,
            pixel_identity_origin="omero-pixels",
            canonical_pixel_verified=True,
        ),
    )


class IdentityProvider:
    def __init__(self, identity):
        self.identity = identity
        self.calls = []

    def generate(self, root, **kwargs):
        self.calls.append((Path(root), kwargs))
        return self.identity.model_copy(update={
            "node_path": kwargs["node_path"],
            "role": kwargs["role"],
        })


def test_discovers_image_and_declared_labels(tmp_path):
    root = tmp_path / "result.zarr"
    _make_image(root, labels=("cells", "nuclei"))

    nodes = discover_ngff_nodes(root)

    assert [(node.node_path, node.role) for node in nodes] == [
        (".", "image"),
        ("labels/cells", "label"),
        ("labels/nuclei", "label"),
    ]


def test_discovers_plate_image_level_labels(tmp_path):
    root = tmp_path / "plate.zarr"
    _write_json(root / ".zattrs", {"plate": {"wells": [{"path": "A/1"}]}})
    _write_json(root / "A/1/.zattrs", {"well": {"images": [{"path": "0"}]}})
    _make_image(root, "A/1/0", labels=("cells",))

    nodes = discover_ngff_nodes(root)

    assert [(node.node_path, node.role) for node in nodes] == [
        ("A/1/0", "image"),
        ("A/1/0/labels/cells", "label"),
    ]


def test_transfer_artifact_disambiguates_duplicate_input_identities(tmp_path):
    root = tmp_path / "second.zarr"
    _make_image(root, labels=("cells",))
    provider = IdentityProvider(_identity())
    manifest = _manifest(
        _input(0, "first.zarr"),
        _input(1, "second.zarr"),
    )

    decision = evaluate_returned_zarr(
        root,
        manifest,
        identity_provider=provider,
    )

    assert decision.eligible
    assert decision.reason == "input-image-unchanged"
    assert decision.matched_inputs[0].ordinal == 1


def test_legacy_duplicate_identities_are_ambiguous(tmp_path):
    root = tmp_path / "result.zarr"
    _make_image(root, labels=("cells",))
    manifest = _manifest(_input(0), _input(1))

    decision = evaluate_returned_zarr(
        root,
        manifest,
        identity_provider=IdentityProvider(_identity()),
    )

    assert not decision.eligible
    assert decision.reason == "ambiguous-input-identity"


def test_changed_pixels_keep_full_even_when_artifact_matches(tmp_path):
    root = tmp_path / "result.zarr"
    _make_image(root, labels=("cells",))
    manifest = _manifest(_input(0, "result.zarr"))

    decision = evaluate_returned_zarr(
        root,
        manifest,
        identity_provider=IdentityProvider(_identity("ISCC:ICHANGED")),
    )

    assert not decision.eligible
    assert decision.reason == "pixels-changed"


def test_result_without_labels_is_not_eligible(tmp_path):
    root = tmp_path / "result.zarr"
    _make_image(root)
    provider = IdentityProvider(_identity())

    decision = evaluate_returned_zarr(
        root,
        _manifest(_input(0, "result.zarr")),
        identity_provider=provider,
    )

    assert not decision.eligible
    assert decision.reason == "no-label-nodes"
    assert provider.calls == []


def test_store_finder_prunes_nested_label_zarrs(tmp_path):
    outer = tmp_path / "nested" / "result.ome.zarr"
    nested = outer / "labels" / "cells.zarr"
    nested.mkdir(parents=True)
    (tmp_path / "second.zarr").mkdir()

    assert find_returned_zarr_stores(tmp_path) == (
        tmp_path / "second.zarr",
        outer,
    )


def test_normalization_transaction_keeps_labels_and_omits_image_chunks(
    tmp_path,
):
    root = tmp_path / "result.zarr"
    _make_image(root, labels=("cells",))
    label_chunk = root / "labels/cells/0/0.0.0.0"
    label_chunk.write_bytes(b"label-pixels")
    decision = evaluate_returned_zarr(
        root,
        _manifest(_input(0, "result.zarr")),
        identity_provider=IdentityProvider(_identity()),
    )

    normalized = normalize_returned_zarr(
        decision,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert not (root / "0").exists()
    assert label_chunk.read_bytes() == b"label-pixels"
    manifest = json.loads(
        (root / ".biomero-shallow.json").read_text(encoding="utf-8")
    )
    assert manifest["model"] == "rfc8-shallow-copy"
    assert manifest["images"][0]["source"]["sourceObjectId"] == 1
    assert "multiscales" not in json.loads(
        (root / ".zattrs").read_text(encoding="utf-8")
    )
    assert normalized.bytes_after < normalized.bytes_before
    assert not list(tmp_path.glob(".result.zarr.biomero-*"))


def test_normalization_restores_full_store_when_commit_rename_fails(tmp_path):
    root = tmp_path / "result.zarr"
    _make_image(root, labels=("cells",))
    original_chunk = root / "0/0.0.0.0"
    decision = evaluate_returned_zarr(
        root,
        _manifest(_input(0, "result.zarr")),
        identity_provider=IdentityProvider(_identity()),
    )
    calls = 0

    def fail_second_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated commit failure")
        source.replace(target)

    try:
        normalize_returned_zarr(
            decision,
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            replace=fail_second_replace,
        )
    except OSError as exc:
        assert "simulated commit failure" in str(exc)
    else:
        raise AssertionError("normalization unexpectedly succeeded")

    assert original_chunk.read_bytes() == b"image-pixels" * 2048
    assert not (root / ".biomero-shallow.json").exists()
