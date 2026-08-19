import errno
import json
import importlib.util
from pathlib import Path

import pytest
from biomero_schema.zarr import (
    CanonicalPlateImage,
    CanonicalPlateSource,
    CanonicalZarrSource,
    PixelIdentity,
)

MODULE_PATH = (
    Path(__file__).parents[2]
    / "biomero_importer"
    / "utils"
    / "canonical_store.py"
)
SPEC = importlib.util.spec_from_file_location("canonical_store", MODULE_PATH)
canonical_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canonical_store)

CANONICAL_MARKER_NAME = canonical_store.CANONICAL_MARKER_NAME
CanonicalCreationLocked = canonical_store.CanonicalCreationLocked
CanonicalStore = canonical_store.CanonicalStore
InvalidCanonicalStore = canonical_store.InvalidCanonicalStore


@pytest.fixture
def source_record():
    return {
        "schema": 1,
        "storageRoot": "group-5-data",
        "relativePath": "project/.processed/Image-3207.g1.ome.zarr",
        "nodePath": ".",
        "sourceObjectType": "Image",
        "sourceObjectId": 3207,
        "sourceGeneration": 1,
        "interchangeProfile": "ngff-0.4-zarr-v2",
        "pixelIdentity": {
            "schema": 1,
            "method": "iscc-bio/imagewalk",
            "nodePath": ".",
            "role": "image",
            "iscc": "ISCC:KPIXEL",
            "dataCode": "ISCC:GDATA",
            "instanceCode": "ISCC:IINSTANCE",
            "toolVersion": "0.1.0",
            "imagewalkRevision": "draft-2026-06",
            "shape": [1, 1, 1, 16, 16],
            "dtype": "uint16",
            "axes": ["t", "c", "z", "y", "x"],
            "coordinateTransformations": [],
        },
        "pixelIdentityOrigin": "raw",
        "canonicalPixelVerified": True,
        "storeIdentity": None,
    }


def make_zarr_v2(path: Path):
    path.mkdir(parents=True)
    (path / ".zgroup").write_text('{"zarr_format": 2}', encoding="utf-8")


def test_builds_deterministic_processed_path(tmp_path):
    store = CanonicalStore(tmp_path)

    relative = store.relative_path_for("project", "Image", 3207, 1)

    assert relative == Path("project/.processed/Image-3207.g1.ome.zarr")
    assert store.resolve(relative) == (
        tmp_path / "project/.processed/Image-3207.g1.ome.zarr"
    )


def test_accepts_shared_pydantic_source_contract(tmp_path, source_record):
    source = CanonicalZarrSource.from_dict(source_record)

    destination = CanonicalStore(tmp_path).destination_for(source)

    assert destination == (
        tmp_path / "project/.processed/Image-3207.g1.ome.zarr"
    )


def test_accepts_shared_plate_source_contract(tmp_path):
    identity = PixelIdentity(
        node_path="A/1/0",
        role="image",
        iscc="ISCC:KPIXEL",
        dataCode="ISCC:GDATA",
        instanceCode="ISCC:IINSTANCE",
        toolVersion="0.1.0",
        imagewalkRevision="draft-2026-06",
        shape=(1, 1, 16, 16),
        dtype="uint16",
        axes=("t", "c", "y", "x"),
    )
    image_source = CanonicalZarrSource(
        storageRoot="group-5-data",
        relativePath="project/.processed/Plate-9.g1.ome.zarr",
        nodePath="A/1/0",
        sourceObjectType="Plate",
        sourceObjectId=9,
        sourceGeneration=1,
        interchangeProfile="ngff-0.4-zarr-v2",
        pixelIdentity=identity,
        pixelIdentityOrigin="canonical-bootstrap",
        canonicalPixelVerified=False,
    )
    plate = CanonicalPlateSource(
        storageRoot="group-5-data",
        relativePath="project/.processed/Plate-9.g1.ome.zarr",
        sourceObjectId=9,
        sourceGeneration=1,
        interchangeProfile="ngff-0.4-zarr-v2",
        images=(CanonicalPlateImage(
            imageNodePath="A/1/0",
            source=image_source,
        ),),
    )

    destination = CanonicalStore(tmp_path).destination_for(plate)

    assert destination == (
        tmp_path / "project/.processed/Plate-9.g1.ome.zarr"
    )


def test_rejects_relative_path_escape(tmp_path):
    store = CanonicalStore(tmp_path)

    with pytest.raises(ValueError, match="managed storage root"):
        store.resolve("../outside.ome.zarr")


def test_atomically_promotes_zarr_with_committed_marker(
    tmp_path, source_record
):
    store = CanonicalStore(tmp_path)
    staging = tmp_path / "staging.ome.zarr"
    make_zarr_v2(staging)

    committed = store.commit(staging, source_record)

    assert committed == tmp_path / source_record["relativePath"]
    assert committed.is_dir()
    assert not staging.exists()
    marker = json.loads(
        (committed / CANONICAL_MARKER_NAME).read_text(encoding="utf-8")
    )
    assert marker["state"] == "committed"
    assert marker["source"] == source_record


def test_promotes_across_filesystems_via_destination_staging(
    tmp_path, source_record, monkeypatch
):
    store = CanonicalStore(tmp_path / "managed")
    staging = tmp_path / "task" / "staging.ome.zarr"
    make_zarr_v2(staging)
    destination = store.destination_for(source_record)
    real_replace = canonical_store.os.replace

    def replace_with_cross_device_boundary(source, target):
        if Path(source) == staging and Path(target) == destination:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(source, target)

    monkeypatch.setattr(canonical_store.os, "replace", replace_with_cross_device_boundary)

    committed = store.commit(staging, source_record)

    assert committed == destination
    assert committed.is_dir()
    assert not staging.exists()
    assert not list(destination.parent.glob(f".{destination.name}.staging-*"))
    marker = json.loads(
        (committed / CANONICAL_MARKER_NAME).read_text(encoding="utf-8")
    )
    assert marker["state"] == "committed"
    assert marker["source"] == source_record


def test_adopts_existing_commit_after_annotation_gap(tmp_path, source_record):
    destination = tmp_path / source_record["relativePath"]
    make_zarr_v2(destination)
    marker = {
        "markerSchema": 1,
        "state": "committed",
        "source": source_record,
    }
    (destination / CANONICAL_MARKER_NAME).write_text(
        json.dumps(marker), encoding="utf-8"
    )
    store = CanonicalStore(tmp_path)

    adopted = store.adopt(source_record)

    assert adopted == destination


def test_rejects_marker_for_different_omero_object(tmp_path, source_record):
    destination = tmp_path / source_record["relativePath"]
    make_zarr_v2(destination)
    wrong = dict(source_record, sourceObjectId=999)
    marker = {"markerSchema": 1, "state": "committed", "source": wrong}
    (destination / CANONICAL_MARKER_NAME).write_text(
        json.dumps(marker), encoding="utf-8"
    )
    store = CanonicalStore(tmp_path)

    with pytest.raises(InvalidCanonicalStore, match="does not match"):
        store.adopt(source_record)


def test_accepts_bioformats2raw_layout_three_root(tmp_path, source_record):
    destination = tmp_path / source_record["relativePath"]
    (destination / "OME").mkdir(parents=True)
    (destination / "OME/METADATA.ome.xml").write_text(
        "<OME/>", encoding="utf-8"
    )
    (destination / "0").mkdir()
    (destination / "0/.zgroup").write_text(
        '{"zarr_format": 2}', encoding="utf-8"
    )
    marker = {
        "markerSchema": 1,
        "state": "committed",
        "source": source_record,
    }
    (destination / CANONICAL_MARKER_NAME).write_text(
        json.dumps(marker), encoding="utf-8"
    )

    assert CanonicalStore(tmp_path).adopt(source_record) == destination


def test_creation_lock_is_exclusive(tmp_path, source_record):
    store = CanonicalStore(tmp_path)

    with store.creation_lock(source_record):
        with pytest.raises(CanonicalCreationLocked):
            with store.creation_lock(source_record):
                pass
