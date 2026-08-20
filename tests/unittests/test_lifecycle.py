import json
from pathlib import Path
from uuid import UUID

import pytest

from biomero_schema.imports import (
    ImportOptionsEnvelope,
    ShallowZarrImportOperation,
)
from biomero_schema.zarr import (
    CanonicalInputManifest,
    CanonicalZarrSource,
    PixelIdentity,
    ShallowCollection,
    ShallowImageReference,
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


def _collection():
    label = ZarrLabelComponent(
        logicalNodePath="labels/nuclei",
        pixelIdentity=_identity("labels/nuclei", "label"),
    )
    return ShallowCollection(
        workflowId=WORKFLOW_ID,
        transferArtifact="result.zarr",
        interchangeProfile="ngff-0.4-zarr-v2",
        images=(ShallowImageReference(
            imageNodePath=".",
            source=_source(),
            returnedPixelIdentity=_identity(),
            labelNodePaths=("labels/nuclei",),
            labelComponents=(label,),
        ),),
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


def test_eligible_image_becomes_label_registration_view(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    collection = _collection()
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
            collection=collection,
            bytes_before=None,
            bytes_after=None,
        ),
    )

    plan = ImportLifecycleEngine().prepare([root], _options())

    assert [(item.path, item.role) for item in plan.items] == [
        (root / "labels" / "nuclei", "image-label")
    ]
    assert plan.decisions == (decision,)


def test_existing_manifest_is_idempotently_reused(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    (root / ".biomero-shallow.json").write_text(
        json.dumps(_collection().to_dict()),
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
