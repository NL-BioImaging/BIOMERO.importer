import json
from pathlib import Path
from uuid import UUID

from biomero_schema.zarr import (
    CanonicalInput,
    CanonicalInputManifest,
    CanonicalPlateImage,
    CanonicalPlateSource,
    CanonicalZarrSource,
    ManagedZarrNode,
    PixelIdentity,
    ZarrLabelComponent,
)

from biomero_importer.utils.result_zarr import (
    discover_ngff_nodes,
    evaluate_returned_zarr,
    find_returned_zarr_stores,
    materialize_shallow_zarr,
    normalize_returned_zarr,
    resolve_shallow_registration,
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


def _make_plate(root: Path, labels_by_image=None) -> None:
    labels_by_image = labels_by_image or {}
    _write_json(root / ".zgroup", {"zarr_format": 2})
    _write_json(root / ".zattrs", {
        "plate": {"wells": [{"path": "A/1"}, {"path": "B/1"}]},
    })
    for well in ("A/1", "B/1"):
        _write_json(
            root / well / ".zattrs",
            {"well": {"images": [{"path": "0"}]}},
        )
        image_path = f"{well}/0"
        _make_image(root, image_path, labels_by_image.get(image_path, ()))


def _identity(
    instance="ISCC:IINSTANCE",
    node_path=".",
    role="image",
) -> PixelIdentity:
    return PixelIdentity(
        node_path=node_path,
        role=role,
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


def _input(
    ordinal,
    artifact=None,
    instance="ISCC:IINSTANCE",
    labels=(),
) -> CanonicalInput:
    identity = _identity(instance)
    return CanonicalInput(
        ordinal=ordinal,
        selected_object_type="Image",
        selected_object_id=ordinal + 1,
        transfer_artifact=artifact,
        labels=labels,
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


def _plate_input(artifact="plate.zarr", labels_by_image=None) -> CanonicalInput:
    labels_by_image = labels_by_image or {}
    relative_path = ".processed/Plate-1.ome.zarr"
    images = []
    for image_path, instance in (
        ("A/1/0", "ISCC:IA"),
        ("B/1/0", "ISCC:IB"),
    ):
        source = CanonicalZarrSource(
            storage_root="group-0-data",
            relative_path=relative_path,
            node_path=image_path,
            source_object_type="Plate",
            source_object_id=1,
            source_generation=1,
            interchange_profile="ngff-0.4-zarr-v2",
            pixel_identity=_identity(instance, image_path),
            pixel_identity_origin="canonical-bootstrap",
            canonical_pixel_verified=True,
        )
        images.append(CanonicalPlateImage(
            image_node_path=image_path,
            source=source,
            labels=tuple(labels_by_image.get(image_path, ())),
        ))
    return CanonicalInput(
        ordinal=0,
        selected_object_type="Plate",
        selected_object_id=1,
        transfer_artifact=artifact,
        plate_source=CanonicalPlateSource(
            storage_root="group-0-data",
            relative_path=relative_path,
            source_object_id=1,
            source_generation=1,
            interchange_profile="ngff-0.4-zarr-v2",
            images=tuple(images),
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


class NodeIdentityProvider:
    def __init__(self, identities):
        self.identities = identities
        self.calls = []

    def generate(self, root, **kwargs):
        self.calls.append((Path(root), kwargs))
        return self.identities[kwargs["node_path"]]


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


def test_unchanged_plate_without_labels_is_a_passthrough(tmp_path):
    root = tmp_path / "plate.zarr"
    _make_plate(root)
    provider = NodeIdentityProvider({
        "A/1/0": _identity("ISCC:IA", "A/1/0"),
        "B/1/0": _identity("ISCC:IB", "B/1/0"),
    })

    decision = evaluate_returned_zarr(
        root,
        _manifest(_plate_input()),
        identity_provider=provider,
    )

    assert decision.unchanged_passthrough
    assert decision.reason == "input-plate-unchanged-no-labels"
    assert len(decision.image_identities) == 2


def test_changed_plate_image_keeps_full_result(tmp_path):
    root = tmp_path / "plate.zarr"
    _make_plate(root, {"A/1/0": ("cells",)})
    provider = NodeIdentityProvider({
        "A/1/0": _identity("ISCC:IA", "A/1/0"),
        "B/1/0": _identity("ISCC:ICHANGED", "B/1/0"),
    })

    decision = evaluate_returned_zarr(
        root,
        _manifest(_plate_input()),
        identity_provider=provider,
    )

    assert decision.outcome == "keep-full"
    assert decision.reason == "pixels-changed"


def test_normalizes_plate_images_and_retains_image_level_label(tmp_path):
    root = tmp_path / "plate.zarr"
    _make_plate(root, {"A/1/0": ("cells",)})
    label_path = "A/1/0/labels/cells"
    provider = NodeIdentityProvider({
        "A/1/0": _identity("ISCC:IA", "A/1/0"),
        "B/1/0": _identity("ISCC:IB", "B/1/0"),
        label_path: _identity("ISCC:ICELLS", label_path, "label"),
    })
    decision = evaluate_returned_zarr(
        root,
        _manifest(_plate_input()),
        identity_provider=provider,
    )

    normalized = normalize_returned_zarr(
        decision,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert decision.eligible
    assert decision.reason == "input-plate-unchanged"
    assert not (root / "A/1/0/0").exists()
    assert not (root / "B/1/0/0").exists()
    assert (root / label_path).is_dir()
    assert len(normalized.collection.images) == 2
    images = {
        image.image_node_path: image
        for image in normalized.collection.images
    }
    assert images["A/1/0"].label_node_paths == (label_path,)
    assert images["B/1/0"].label_node_paths == ()


def test_normalizes_renamed_plate_output_by_pixel_identity(tmp_path):
    root = tmp_path / "plate__segmentation.ome.zarr"
    _make_plate(root, {"A/1/0": ("cells",)})
    label_path = "A/1/0/labels/cells"
    provider = NodeIdentityProvider({
        "A/1/0": _identity("ISCC:IA", "A/1/0"),
        "B/1/0": _identity("ISCC:IB", "B/1/0"),
        label_path: _identity("ISCC:ICELLS", label_path, "label"),
    })
    decision = evaluate_returned_zarr(
        root,
        _manifest(_plate_input()),
        identity_provider=provider,
    )

    normalized = normalize_returned_zarr(
        decision,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert decision.eligible
    assert decision.matched_inputs[0].transfer_artifact == "plate.zarr"
    assert normalized.collection.transfer_artifact == root.name
    assert (root / ".biomero-shallow.json").is_file()
    assert not (root / "A/1/0/0").exists()
    assert not (root / "B/1/0/0").exists()
    assert (root / label_path).is_dir()


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


def test_classifies_inherited_new_and_changed_labels(tmp_path):
    root = tmp_path / "result.zarr"
    _make_image(root, labels=("nuclei", "cells", "foci"))
    managed_nuclei = ZarrLabelComponent(
        logical_node_path="labels/nuclei",
        pixel_identity=_identity(
            "ISCC:INUCLEI",
            "labels/nuclei",
            "label",
        ),
        source=ManagedZarrNode(
            storage_root="import-mount-data",
            relative_path="Project A/.analyzed/first/result.zarr",
            node_path="labels/nuclei",
        ),
    )
    managed_foci = ZarrLabelComponent(
        logical_node_path="labels/foci",
        pixel_identity=_identity("ISCC:IOLD", "labels/foci", "label"),
        source=ManagedZarrNode(
            storage_root="import-mount-data",
            relative_path="Project A/.analyzed/first/result.zarr",
            node_path="labels/foci",
        ),
    )
    provider = NodeIdentityProvider({
        ".": _identity(),
        "labels/nuclei": _identity(
            "ISCC:INUCLEI",
            "labels/nuclei",
            "label",
        ),
        "labels/cells": _identity(
            "ISCC:ICELLS",
            "labels/cells",
            "label",
        ),
        "labels/foci": _identity(
            "ISCC:ICHANGED",
            "labels/foci",
            "label",
        ),
    })

    decision = evaluate_returned_zarr(
        root,
        _manifest(_input(
            0,
            "result.zarr",
            labels=(managed_nuclei, managed_foci),
        )),
        identity_provider=provider,
    )

    assert decision.eligible
    assert len(decision.label_identities) == 3
    components = {
        item.logical_node_path: item for item in decision.label_components
    }
    assert components["labels/nuclei"].source == managed_nuclei.source
    assert components["labels/cells"].source is None
    assert components["labels/foci"].source is None


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


def test_unchanged_result_without_labels_is_a_passthrough(tmp_path):
    root = tmp_path / "result.zarr"
    _make_image(root)
    provider = IdentityProvider(_identity())

    decision = evaluate_returned_zarr(
        root,
        _manifest(_input(0, "result.zarr")),
        identity_provider=provider,
    )

    assert not decision.eligible
    assert decision.unchanged_passthrough
    assert decision.reason == "input-image-unchanged-no-labels"
    assert len(provider.calls) == 1


def test_changed_result_without_labels_is_kept_full(tmp_path):
    root = tmp_path / "result.zarr"
    _make_image(root)

    decision = evaluate_returned_zarr(
        root,
        _manifest(_input(0, "result.zarr")),
        identity_provider=IdentityProvider(_identity("ISCC:ICHANGED")),
    )

    assert not decision.eligible
    assert not decision.unchanged_passthrough
    assert decision.outcome == "keep-full"
    assert decision.reason == "pixels-changed"


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
    assert manifest["images"][0]["labelComponents"][0]["source"] is None
    assert manifest["images"][0]["labelComponents"][0][
        "pixelIdentity"
    ]["role"] == "label"
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


def test_resolves_primary_and_label_registration_views(tmp_path):
    import_root = tmp_path / "data"
    group_root = import_root / "Project A"
    returned = import_root / "results/result.zarr"
    canonical = group_root / ".processed/Image-1.ome.zarr"
    _make_image(returned, labels=("cells",))
    _make_image(canonical)
    decision = evaluate_returned_zarr(
        returned,
        _manifest(_input(0, "result.zarr")),
        identity_provider=IdentityProvider(_identity()),
    )
    normalize_returned_zarr(
        decision,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    roots = {
        "import-mount-data": import_root,
        "group-0-data": group_root,
    }

    primary = resolve_shallow_registration(
        returned,
        storage_roots=roots,
        import_mount_path=import_root,
    )
    label = resolve_shallow_registration(
        returned / "labels/cells",
        storage_roots=roots,
        import_mount_path=import_root,
    )

    assert primary.kind == "primary"
    assert primary.registration_path == canonical.resolve()
    assert primary.reference.relative_path == "results/result.zarr"
    assert primary.reference.label_node_paths == ("labels/cells",)
    assert label.kind == "label"
    assert label.registration_path == (returned / "labels/cells").resolve()
    assert label.reference == primary.reference


def test_resolves_source_and_label_backed_plate_registration(tmp_path):
    import_root = tmp_path / "data"
    group_root = import_root / "Project A"
    canonical = group_root / ".processed/Plate-1.ome.zarr"
    returned = import_root / "results/plate.zarr"
    labels_by_image = {
        "A/1/0": ("nuclei",),
        "B/1/0": ("nuclei",),
    }
    _make_plate(canonical)
    _make_plate(returned, labels_by_image)
    provider = NodeIdentityProvider({
        "A/1/0": _identity("ISCC:IA", "A/1/0"),
        "B/1/0": _identity("ISCC:IB", "B/1/0"),
        "A/1/0/labels/nuclei": _identity(
            "ISCC:ILABELA", "A/1/0/labels/nuclei", "label"
        ),
        "B/1/0/labels/nuclei": _identity(
            "ISCC:ILABELB", "B/1/0/labels/nuclei", "label"
        ),
    })
    decision = evaluate_returned_zarr(
        returned,
        _manifest(_plate_input()),
        identity_provider=provider,
    )
    normalize_returned_zarr(
        decision,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    roots = {
        "import-mount-data": import_root,
        "group-0-data": group_root,
    }

    source = resolve_shallow_registration(
        returned,
        storage_roots=roots,
        import_mount_path=import_root,
    )
    label = resolve_shallow_registration(
        returned,
        storage_roots=roots,
        import_mount_path=import_root,
        import_options={
            "schema": 1,
            "platePixelSource": "label",
            "plateLabelName": "nuclei",
        },
    )

    assert source.kind == "plate"
    assert source.registration_path == canonical.resolve()
    assert source.reference.source_object_id == 1
    assert source.reference.image_node_count == 2
    assert source.plate_label_paths == ()
    assert dict(label.plate_label_paths) == {
        "A/1/0": (returned / "A/1/0/labels/nuclei").resolve(),
        "B/1/0": (returned / "B/1/0/labels/nuclei").resolve(),
    }
    assert label.plate_label_name == "nuclei"


def test_shallow_registration_returns_none_outside_import_mount(tmp_path):
    assert resolve_shallow_registration(
        tmp_path / "outside.zarr",
        import_mount_path=tmp_path / "data",
    ) is None


def test_materializes_original_with_inherited_and_local_labels(tmp_path):
    import_root = tmp_path / "data"
    canonical = import_root / "Project A/.processed/Image-1.ome.zarr"
    prior = import_root / "Project A/.analyzed/first/result.zarr"
    returned = import_root / "results/result.zarr"
    _make_image(canonical)
    _make_image(prior, labels=("nuclei",))
    _make_image(returned, labels=("nuclei", "cells"))
    (prior / "labels/nuclei/0/0.0.0.0").write_bytes(b"prior-nuclei")
    (returned / "labels/nuclei/0/0.0.0.0").write_bytes(
        b"workflow-copy-of-nuclei"
    )
    (returned / "labels/cells/0/0.0.0.0").write_bytes(b"new-cells")
    nuclei_identity = _identity(
        "ISCC:INUCLEI", "labels/nuclei", "label"
    )
    input_nuclei = ZarrLabelComponent(
        logical_node_path="labels/nuclei",
        pixel_identity=nuclei_identity,
        source=ManagedZarrNode(
            storage_root="group-0-data",
            relative_path=".analyzed/first/result.zarr",
            node_path="labels/nuclei",
        ),
    )
    provider = NodeIdentityProvider({
        ".": _identity(),
        "labels/nuclei": nuclei_identity,
        "labels/cells": _identity(
            "ISCC:ICELLS", "labels/cells", "label"
        ),
    })
    decision = evaluate_returned_zarr(
        returned,
        _manifest(_input(
            0,
            "result.zarr",
            labels=(input_nuclei,),
        )),
        identity_provider=provider,
    )
    normalize_returned_zarr(
        decision,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    roots = {
        "import-mount-data": import_root,
        "group-0-data": import_root / "Project A",
    }
    registration = resolve_shallow_registration(
        returned / "labels/cells",
        storage_roots=roots,
        import_mount_path=import_root,
    )
    destination = tmp_path / "transfer/result.zarr"
    destination.parent.mkdir()

    result = materialize_shallow_zarr(
        registration.reference,
        destination,
        roots,
    )

    assert (destination / "0/0.0.0.0").read_bytes() == b"image-pixels" * 2048
    assert (
        destination / "labels/nuclei/0/0.0.0.0"
    ).read_bytes() == b"prior-nuclei"
    assert (
        destination / "labels/cells/0/0.0.0.0"
    ).read_bytes() == b"new-cells"
    assert json.loads(
        (destination / "labels/.zattrs").read_text(encoding="utf-8")
    ) == {"labels": ["nuclei", "cells"]}
    assert all(label.source is not None for label in result.labels)
    assert not (returned / "labels/nuclei").exists()
    assert (returned / "labels/cells/0/0.0.0.0").read_bytes() == b"new-cells"


def test_materializes_plate_label_projection_as_standalone_image(tmp_path):
    import_root = tmp_path / "data"
    group_root = import_root / "Project A"
    canonical = group_root / ".processed/Plate-1.ome.zarr"
    returned = import_root / "results/plate.zarr"
    _make_plate(canonical)
    _make_plate(returned, {"A/1/0": ("cells",)})
    label_path = "A/1/0/labels/cells"
    provider = NodeIdentityProvider({
        "A/1/0": _identity("ISCC:IA", "A/1/0"),
        "B/1/0": _identity("ISCC:IB", "B/1/0"),
        label_path: _identity("ISCC:ICELLS", label_path, "label"),
    })
    decision = evaluate_returned_zarr(
        returned,
        _manifest(_plate_input()),
        identity_provider=provider,
    )
    normalize_returned_zarr(
        decision,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    roots = {
        "import-mount-data": import_root,
        "group-0-data": group_root,
    }
    registration = resolve_shallow_registration(
        returned / label_path,
        storage_roots=roots,
        import_mount_path=import_root,
    )
    destination = tmp_path / "transfer/image.zarr"
    destination.parent.mkdir()

    result = materialize_shallow_zarr(
        registration.reference,
        destination,
        roots,
    )

    assert (destination / "0/0.0.0.0").read_bytes() == b"image-pixels" * 2048
    assert (
        destination / "labels/cells/0/0.0.0.0"
    ).read_bytes() == b"image-pixels" * 2048
    assert discover_ngff_nodes(destination)[0].node_path == "."
    assert result.labels[0].logical_node_path == "labels/cells"
    assert result.labels[0].source.node_path == label_path


def test_materializes_whole_shallow_plate_with_all_image_labels(tmp_path):
    import_root = tmp_path / "data"
    group_root = import_root / "Project A"
    canonical = group_root / ".processed/Plate-1.ome.zarr"
    returned = import_root / "results/plate.zarr"
    destination = tmp_path / "transfer/plate.zarr"
    destination.parent.mkdir()
    _make_plate(canonical)
    _make_plate(returned, {
        "A/1/0": ("cells",),
        "B/1/0": ("cells",),
    })
    identities = {
        "A/1/0": _identity("ISCC:IA", "A/1/0"),
        "B/1/0": _identity("ISCC:IB", "B/1/0"),
        "A/1/0/labels/cells": _identity(
            "ISCC:IACELLS", "A/1/0/labels/cells", "label"
        ),
        "B/1/0/labels/cells": _identity(
            "ISCC:IBCELLS", "B/1/0/labels/cells", "label"
        ),
    }
    decision = evaluate_returned_zarr(
        returned,
        _manifest(_plate_input()),
        identity_provider=NodeIdentityProvider(identities),
    )
    normalize_returned_zarr(
        decision,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    roots = {
        "import-mount-data": import_root,
        "group-0-data": group_root,
    }
    registration = resolve_shallow_registration(
        returned,
        storage_roots=roots,
        import_mount_path=import_root,
    )

    result = materialize_shallow_zarr(
        registration.reference,
        destination,
        roots,
    )

    assert json.loads((destination / ".zattrs").read_text(encoding="utf-8")) == {
        "plate": {"wells": [{"path": "A/1"}, {"path": "B/1"}]},
    }
    for image_path in ("A/1/0", "B/1/0"):
        assert (
            destination / image_path / "0/0.0.0.0"
        ).read_bytes() == b"image-pixels" * 2048
        assert (
            destination / image_path / "labels/cells/0/0.0.0.0"
        ).read_bytes() == b"image-pixels" * 2048
        assert json.loads((
            destination / image_path / "labels/.zattrs"
        ).read_text(encoding="utf-8")) == {"labels": ["cells"]}
    assert {label.logical_node_path for label in result.labels} == {
        "A/1/0/labels/cells",
        "B/1/0/labels/cells",
    }
    assert all(label.source is not None for label in result.labels)


def test_materializes_legacy_shallow_manifest_without_component_records(
    tmp_path,
):
    import_root = tmp_path / "data"
    group_root = import_root / "Project A"
    canonical = group_root / ".processed/Image-1.ome.zarr"
    returned = import_root / "results/result.zarr"
    _make_image(canonical)
    _make_image(returned, labels=("cells",))
    label_chunk = returned / "labels/cells/0/0.0.0.0"
    label_chunk.write_bytes(b"legacy-cells")
    decision = evaluate_returned_zarr(
        returned,
        _manifest(_input(0, "result.zarr")),
        identity_provider=IdentityProvider(_identity()),
    )
    normalize_returned_zarr(
        decision,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    manifest_path = returned / ".biomero-shallow.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["images"][0].pop("labelComponents")
    _write_json(manifest_path, manifest)
    roots = {
        "import-mount-data": import_root,
        "group-0-data": group_root,
    }
    registration = resolve_shallow_registration(
        returned / "labels/cells",
        storage_roots=roots,
        import_mount_path=import_root,
    )
    destination = tmp_path / "full.zarr"

    result = materialize_shallow_zarr(
        registration.reference,
        destination,
        roots,
        identity_provider=IdentityProvider(_identity()),
    )

    assert (
        destination / "labels/cells/0/0.0.0.0"
    ).read_bytes() == b"legacy-cells"
    assert result.labels[0].source == ManagedZarrNode(
        storage_root="import-mount-data",
        relative_path="results/result.zarr",
        node_path="labels/cells",
    )
