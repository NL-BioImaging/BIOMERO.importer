from uuid import uuid4

import pytest

from biomero_schema.imports import (
    ImportOptionsEnvelope,
    ShallowZarrImportOperation,
)
from biomero_schema.zarr import CanonicalInputManifest

from biomero_importer.api import (
    UnsupportedImportOperation,
    get_importer_capabilities,
    submit_import_order,
)


def _order(options=None):
    order = {
        "Group": "test",
        "Username": "user",
        "UUID": str(uuid4()),
        "DestinationID": 1,
        "DestinationType": "Dataset",
        "Files": ["/data/result.zarr"],
    }
    if options is not None:
        order["ImportOptions"] = options
    return order


def _operation():
    return ShallowZarrImportOperation(
        canonicalInputs=CanonicalInputManifest(
            workflowId=uuid4(),
            exportTaskId=uuid4(),
            inputs=(),
        )
    )


def test_capability_is_opt_in(monkeypatch):
    monkeypatch.delenv("BIOMERO_SHALLOW_ZARR", raising=False)
    assert get_importer_capabilities()["lifecycleOperations"] == []

    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.api._identity_dependency_available", lambda: True
    )
    assert get_importer_capabilities()["lifecycleOperations"] == [
        "biomero.shallow-zarr"
    ]


def test_capability_reports_missing_identity_extra(monkeypatch):
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.api._identity_dependency_available", lambda: False
    )

    capabilities = get_importer_capabilities()

    assert capabilities["lifecycleOperations"] == []
    assert capabilities["isccBioIdentity"] is False
    assert capabilities["configurationErrors"] == [
        "BIOMERO_SHALLOW_ZARR requires the biomero-importer identity extra"
    ]


def test_legacy_order_is_submitted_unchanged(monkeypatch):
    monkeypatch.delenv("BIOMERO_SHALLOW_ZARR", raising=False)
    events = []
    order = _order({"schema": 1})

    returned = submit_import_order(
        order,
        log_order=lambda value, stage: events.append((value, stage)),
    )

    assert returned == order["UUID"]
    assert events[0][0]["ImportOptions"] == {"schema": 1}
    assert events[0][1] == "Import Pending"


def test_operation_order_requires_capability(monkeypatch):
    monkeypatch.delenv("BIOMERO_SHALLOW_ZARR", raising=False)
    order = _order(ImportOptionsEnvelope(
        operations=(_operation(),)
    ).to_dict())

    with pytest.raises(UnsupportedImportOperation):
        submit_import_order(order, log_order=lambda *_: None)


def test_operation_order_is_normalized_and_submitted(monkeypatch):
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.api._identity_dependency_available", lambda: True
    )
    events = []
    order = _order(ImportOptionsEnvelope(
        operations=(_operation(),)
    ).to_dict())

    submit_import_order(
        order,
        log_order=lambda value, stage: events.append((value, stage)),
    )

    assert events[0][0]["ImportOptions"]["schema"] == 2
    assert events[0][0]["ImportOptions"]["operations"][0]["kind"] == (
        "biomero.shallow-zarr"
    )


def test_operation_order_reports_missing_identity_extra(monkeypatch):
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.api._identity_dependency_available", lambda: False
    )
    order = _order(ImportOptionsEnvelope(
        operations=(_operation(),)
    ).to_dict())

    with pytest.raises(
        UnsupportedImportOperation,
        match=r"install biomero-importer\[identity\]",
    ):
        submit_import_order(order, log_order=lambda *_: None)
