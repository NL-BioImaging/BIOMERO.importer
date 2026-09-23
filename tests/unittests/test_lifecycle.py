import json
from pathlib import Path
from uuid import UUID

import pytest

from biomero_schema.imports import (
    ImportOptionsEnvelope,
    ShallowZarrImportOperation,
)
from biomero_schema.zarr import (
    TRANSFER_INPUT_MARKER,
    CanonicalInputManifest,
    CanonicalZarrSource,
    PixelIdentity,
    ShallowBindings,
    ShallowCollection,
    ShallowImageBinding,
    ShallowImageNode,
    ShallowLabelBinding,
    ShallowLabelNode,
    ShallowManifest,
    ZarrLabelComponent,
)

from biomero_importer.utils.lifecycle import ImportLifecycleEngine
from biomero_importer.utils.importer import DataPackageImporter
from biomero_importer.utils.result_zarr import (
    NormalizedShallowResult,
    ReturnedZarrDecision,
)


WORKFLOW_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _identity(node_path=".", role="image"):
    return PixelIdentity(
        nodePath=node_path,
        role=role,
        iscc="ISCC:KSUM",
        dataCode="ISCC:GDATA",
        instanceCode="ISCC:IINSTANCE",
        toolVersion="0.1.0",
        imagewalkRevision="iscc-bio/0.1.0@revision",
        shape=(1, 1, 8, 8),
        dtype="uint16",
        axes=("t", "c", "y", "x"),
    )


def _source():
    return CanonicalZarrSource(
        storageRoot="group-0-data",
        relativePath=".processed/source.ome.zarr",
        nodePath=".",
        sourceObjectType="Image",
        sourceObjectId=1,
        sourceGeneration=1,
        interchangeProfile="ngff-0.4-zarr-v2",
        pixelIdentity=_identity(),
        pixelIdentityOrigin="omero-pixels",
        canonicalPixelVerified=True,
    )


def _manifest():
    label = ZarrLabelComponent(
        logicalNodePath="labels/nuclei",
        pixelIdentity=_identity("labels/nuclei", "label"),
    )
    return ShallowManifest(
        workflowId=WORKFLOW_ID,
        transferArtifact="result.zarr",
        interchangeProfile="ngff-0.4-zarr-v2",
        collection=ShallowCollection(
            name="result.zarr",
            images=(ShallowImageNode(id="image-0", name=".", nodePath="."),),
            labels=(ShallowLabelNode(
                id="label-0",
                name="labels/nuclei",
                nodePath="labels/nuclei",
                sourceImageId="image-0",
            ),),
        ),
        bindings=ShallowBindings(
            images=(ShallowImageBinding(
                nodeId="image-0",
                source=_source(),
                returnedPixelIdentity=_identity(),
            ),),
            labels=(ShallowLabelBinding(
                nodeId="label-0",
                component=label,
            ),),
        ),
    )


def _options():
    operation = ShallowZarrImportOperation(
        canonicalInputs=CanonicalInputManifest(
            workflowId=WORKFLOW_ID,
            exportTaskId=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            inputs=(),
        )
    )
    return ImportOptionsEnvelope(operations=(operation,))


def _zarr(tmp_path):
    root = tmp_path / "result.zarr"
    root.mkdir()
    (root / ".zattrs").write_text("{}", encoding="utf-8")
    (root / "labels" / "nuclei").mkdir(parents=True)
    return root


def test_no_operations_preserve_paths_without_execution(tmp_path):
    path = tmp_path / "input.tif"

    plan = ImportLifecycleEngine().prepare([path], None)

    assert [item.path for item in plan.items] == [path]
    assert [item.role for item in plan.items] == ["input"]
    assert plan.changed is False


def test_shallow_operation_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("BIOMERO_SHALLOW_ZARR", raising=False)

    with pytest.raises(ValueError, match="disabled"):
        ImportLifecycleEngine().prepare([_zarr(tmp_path)], _options())


def test_legacy_passthrough_decision_does_not_suppress_registration(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: ReturnedZarrDecision(
            store_path=root, outcome="skip-passthrough",
            reason="input-image-unchanged-no-labels"),
    )
    plan = ImportLifecycleEngine().prepare([root], _options())
    assert root in [item.path for item in plan.items]
    assert root.exists()


def test_eligible_image_becomes_label_registration_view(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    (root / TRANSFER_INPUT_MARKER).write_text("{}", encoding="utf-8")
    manifest = _manifest()
    decision = ReturnedZarrDecision(
        store_path=root,
        outcome="eligible",
        reason="matched",
        matched_inputs=(),
    )
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: decision,
    )
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.normalize_returned_zarr",
        lambda *args, **kwargs: NormalizedShallowResult(
            store_path=root,
            manifest=manifest,
            bytes_before=None,
            bytes_after=None,
        ),
    )

    plan = ImportLifecycleEngine().prepare([root], _options())

    assert [(item.path, item.role) for item in plan.items] == [
        (root / "labels" / "nuclei", "image-label")
    ]
    assert plan.decisions == (decision,)
    assert not (root / TRANSFER_INPUT_MARKER).exists()
    provenance = json.loads((root / '.biomero-import-storage.json').read_text())
    assert provenance['storage'] == 'shallow-zarr'
    assert provenance['location'] == 'importer'
    assert provenance['tool_version']
    assert provenance['workflow_id'] == str(WORKFLOW_ID)


def test_label_free_shallow_image_keeps_primary_registration(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    manifest = _manifest()
    manifest = manifest.model_copy(update={
        'collection': manifest.collection.model_copy(update={'labels': ()}),
        'bindings': manifest.bindings.model_copy(update={'labels': ()}),
    })
    monkeypatch.setenv('BIOMERO_SHALLOW_ZARR', 'true')
    monkeypatch.setattr('biomero_importer.utils.lifecycle.evaluate_returned_zarr',
                        lambda *args, **kwargs: ReturnedZarrDecision(store_path=root, outcome='eligible', reason='matched'))
    monkeypatch.setattr('biomero_importer.utils.lifecycle.normalize_returned_zarr',
                        lambda *args, **kwargs: NormalizedShallowResult(
                            store_path=root, manifest=manifest,
                            bytes_before=None, bytes_after=None,
                        ))
    plan = ImportLifecycleEngine().prepare([root], _options())
    assert [(item.path, item.role) for item in plan.items] == [(root, 'primary')]


def test_plate_manifest_adds_requested_label_preview(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    manifest = _manifest()
    plate_source = _source().model_copy(update={
        'source_object_type': 'Plate',
        'source_object_id': 7,
    })
    image_binding = manifest.bindings.images[0].model_copy(update={
        'source': plate_source,
    })
    manifest = manifest.model_copy(update={
        'bindings': manifest.bindings.model_copy(update={
            'images': (image_binding,),
        }),
    })
    operation = ShallowZarrImportOperation(
        canonicalInputs=_options().operations[0].canonical_inputs,
        importPlateLabelPreview=True,
        plateLabelName='nuclei',
    )
    monkeypatch.setenv('BIOMERO_SHALLOW_ZARR', 'true')
    monkeypatch.setattr(
        'biomero_importer.utils.lifecycle.evaluate_returned_zarr',
        lambda *args, **kwargs: ReturnedZarrDecision(
            store_path=root, outcome='eligible', reason='matched',
        ),
    )
    monkeypatch.setattr(
        'biomero_importer.utils.lifecycle.normalize_returned_zarr',
        lambda *args, **kwargs: NormalizedShallowResult(
            store_path=root, manifest=manifest,
            bytes_before=None, bytes_after=None,
        ),
    )

    plan = ImportLifecycleEngine().prepare(
        [root], ImportOptionsEnvelope(operations=(operation,)),
    )

    assert [item.role for item in plan.items] == [
        'primary', 'plate-label-preview',
    ]
    assert plan.items[1].registration.plate_pixel_source == 'label'
    assert plan.items[1].registration.plate_label_name == 'nuclei'


def test_existing_manifest_is_idempotently_reused(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    (root / ".biomero-shallow.json").write_text(
        json.dumps(_manifest().to_dict()),
        encoding="utf-8",
    )
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: pytest.fail("must not re-evaluate"),
    )

    plan = ImportLifecycleEngine().prepare([root], _options())

    assert [item.path for item in plan.items] == [
        root / "labels" / "nuclei"
    ]
    assert plan.decisions == ()


def test_prepared_views_use_existing_upload_path_and_restore_envelope(tmp_path):
    root = _zarr(tmp_path)
    plan = ImportLifecycleEngine().prepare([root], None)
    importer = DataPackageImporter.__new__(DataPackageImporter)
    importer.data_package = {"ImportOptions": {"schema": 2, "operations": []}}
    importer.logger = __import__("logging").getLogger(__name__)
    calls = []

    def upload_files(conn, files, **targets):
        calls.append((
            files,
            targets,
            importer.data_package["ImportOptions"],
        ))
        return [(files[0], 1, Path(files[0]).name, 1)], []

    importer.upload_files = upload_files

    successful, failed = importer.upload_prepared_plan(
        object(),
        plan,
        dataset_id=1,
    )

    assert len(successful) == 1
    assert failed == []
    assert calls[0][0] == [str(root)]
    assert calls[0][2] == {
        "schema": 1,
        "platePixelSource": "source",
        "plateLabelName": None,
    }
    assert importer.data_package["ImportOptions"] == {
        "schema": 2,
        "operations": [],
    }
