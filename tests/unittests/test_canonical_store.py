import errno
import json
import importlib.util
import logging
import os
from pathlib import Path
import subprocess
import sys

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
external_canonical_marker_path = canonical_store.external_canonical_marker_path
load_canonical_marker = canonical_store.load_canonical_marker
write_indexed_canonical_marker = canonical_store.write_indexed_canonical_marker


@pytest.fixture
def source_record():
    return CanonicalZarrSource.from_dict({
        "schema": 1,
        "storageRoot": "group-5-data",
        "relativePath": "project/.processed/Image-3207.ome.zarr",
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
    }).to_dict()


def make_zarr_v2(path: Path):
    path.mkdir(parents=True)
    (path / ".zgroup").write_text('{"zarr_format": 2}', encoding="utf-8")


def test_builds_deterministic_processed_path(tmp_path):
    store = CanonicalStore(tmp_path)

    relative = store.relative_path_for("project", "Image", 3207, 1)

    assert relative == Path("project/.processed/Image-3207.ome.zarr")
    assert store.resolve(relative) == (
        tmp_path / "project/.processed/Image-3207.ome.zarr"
    )


def test_source_generation_does_not_create_another_store(tmp_path):
    store = CanonicalStore(tmp_path)

    first = store.relative_path_for("project", "Plate", 9, 1)
    second = store.relative_path_for("project", "Plate", 9, 2)

    assert first == second == Path("project/.processed/Plate-9.ome.zarr")


def test_accepts_shared_pydantic_source_contract(tmp_path, source_record):
    source = CanonicalZarrSource.from_dict(source_record)

    destination = CanonicalStore(tmp_path).destination_for(source)

    assert destination == (
        tmp_path / "project/.processed/Image-3207.ome.zarr"
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
        relativePath="project/.processed/Plate-9.ome.zarr",
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
        relativePath="project/.processed/Plate-9.ome.zarr",
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
        tmp_path / "project/.processed/Plate-9.ome.zarr"
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


def sidecar_for(native):
    return native.parent / ".processed" / ".biomero" / f"{native.name}.canonical.json"


def test_indexes_native_zarr_with_sidecar_in_biomero_folder(
    tmp_path, source_record
):
    native = tmp_path / "project" / "incoming.ome.zarr"
    make_zarr_v2(native)

    marker_path = write_indexed_canonical_marker(native, source_record)

    assert marker_path == (
        tmp_path / "project" / ".processed" / ".biomero"
        / "incoming.ome.zarr.canonical.json"
    )
    assert marker_path == external_canonical_marker_path(native)
    # Nothing is written into the user's folder except the BIOMERO folder.
    assert not (native / CANONICAL_MARKER_NAME).exists()
    assert not (native.parent / ".biomero").exists()
    assert sorted(p.name for p in native.parent.iterdir()) == [
        ".processed", native.name]
    assert load_canonical_marker(native) == CanonicalZarrSource.from_dict(
        source_record
    )


def test_sidecar_location_honours_processed_folder_name(tmp_path):
    native = tmp_path / "x.ome.zarr"

    assert external_canonical_marker_path(native, ".custom") == (
        tmp_path / ".custom" / ".biomero" / "x.ome.zarr.canonical.json")


def test_sidecar_for_zarr_inside_biomero_folder_does_not_nest(tmp_path):
    native = tmp_path / ".processed" / "Image-1.ome.zarr"

    assert external_canonical_marker_path(native) == (
        tmp_path / ".processed" / ".biomero" / "Image-1.ome.zarr.canonical.json")


def test_sidecar_path_refuses_path_without_name():
    root = Path(Path.cwd().anchor)

    with pytest.raises(ValueError, match="no name"):
        external_canonical_marker_path(root)
    with pytest.raises(ValueError, match="no name"):
        canonical_store.legacy_canonical_marker_path(root)


def test_unwritable_biomero_folder_raises_oserror_and_creates_nothing_else(
    tmp_path, source_record
):
    native = tmp_path / "incoming.ome.zarr"
    make_zarr_v2(native)
    # A file where the BIOMERO folder should be: mkdir fails on every platform.
    (tmp_path / ".processed").write_text("not a folder")

    with pytest.raises(OSError):
        write_indexed_canonical_marker(native, source_record)

    assert (tmp_path / ".processed").read_text() == "not a folder"
    assert load_canonical_marker(native) is None


def write_legacy_sidecar(native, source_record):
    legacy = native.parent / ".biomero" / f"{native.name}.canonical.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({
        "markerSchema": 1, "state": "committed", "source": source_record,
    }), encoding="utf-8")
    return legacy


def test_reads_legacy_sidecar_next_to_input_without_touching_it(
    tmp_path, source_record
):
    native = tmp_path / "incoming.ome.zarr"
    make_zarr_v2(native)
    legacy = write_legacy_sidecar(native, source_record)

    assert load_canonical_marker(native) == CanonicalZarrSource.from_dict(
        source_record)
    assert legacy.is_file()
    assert not (tmp_path / ".processed").exists()


def test_new_sidecar_wins_over_legacy_when_both_exist(tmp_path, source_record):
    native = tmp_path / "incoming.ome.zarr"
    make_zarr_v2(native)
    write_legacy_sidecar(native, {**source_record, "sourceObjectId": 1})
    sidecar = sidecar_for(native)
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({
        "markerSchema": 1, "state": "committed",
        "source": {**source_record, "sourceObjectId": 2},
    }), encoding="utf-8")

    assert load_canonical_marker(native).source_object_id == 2


def test_write_moves_legacy_sidecar_and_removes_empty_biomero_folder(
    tmp_path, source_record
):
    native = tmp_path / "incoming.ome.zarr"
    make_zarr_v2(native)
    legacy = write_legacy_sidecar(native, source_record)

    marker_path = write_indexed_canonical_marker(native, source_record)

    assert marker_path == sidecar_for(native)
    assert marker_path.is_file()
    assert not legacy.exists()
    assert not legacy.parent.exists()
    assert load_canonical_marker(native) == CanonicalZarrSource.from_dict(
        source_record)


def test_write_keeps_foreign_files_in_legacy_biomero_folder(
    tmp_path, source_record
):
    native = tmp_path / "incoming.ome.zarr"
    make_zarr_v2(native)
    legacy = write_legacy_sidecar(native, source_record)
    foreign = legacy.parent / "notes.txt"
    foreign.write_text("user file")
    other = legacy.parent / "other.ome.zarr.canonical.json"
    other.write_text("{}")

    write_indexed_canonical_marker(native, source_record)

    assert not legacy.exists()
    assert foreign.read_text() == "user file"
    assert other.is_file()


def test_failed_write_leaves_legacy_sidecar_in_place_and_readable(
    tmp_path, source_record, monkeypatch
):
    native = tmp_path / "incoming.ome.zarr"
    make_zarr_v2(native)
    legacy = write_legacy_sidecar(native, source_record)

    def deny_replace(*_args):
        raise PermissionError(13, "read-only")

    monkeypatch.setattr(canonical_store.os, "replace", deny_replace)

    with pytest.raises(PermissionError):
        write_indexed_canonical_marker(native, source_record)

    assert legacy.is_file()
    assert not sidecar_for(native).exists()
    assert not list(sidecar_for(native).parent.glob("*.tmp"))
    assert load_canonical_marker(native) == CanonicalZarrSource.from_dict(
        source_record)


def test_legacy_removal_failure_is_a_warning_and_new_sidecar_stays(
    tmp_path, source_record, monkeypatch, caplog
):
    native = tmp_path / "incoming.ome.zarr"
    make_zarr_v2(native)
    legacy = write_legacy_sidecar(native, source_record)
    original_unlink = Path.unlink

    def deny_legacy(path, *args, **kwargs):
        if path == legacy:
            raise PermissionError(13, "Permission denied", str(path))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_legacy)

    with caplog.at_level("WARNING"):
        marker_path = write_indexed_canonical_marker(native, source_record)

    assert marker_path.is_file()
    assert legacy.is_file()
    assert "Could not remove canonical sidecar" in caplog.text
    assert load_canonical_marker(native) == CanonicalZarrSource.from_dict(
        source_record)


def test_zarr_inside_biomero_folder_keeps_its_sidecar_after_write(
    tmp_path, source_record
):
    # Legacy and current location coincide for a Zarr inside .processed.
    native = tmp_path / ".processed" / "Image-1.ome.zarr"
    make_zarr_v2(native)

    marker_path = write_indexed_canonical_marker(native, source_record)

    assert marker_path.is_file()
    assert load_canonical_marker(native) == CanonicalZarrSource.from_dict(
        source_record)


def test_untraversable_biomero_folder_is_treated_as_no_sidecar(
    tmp_path, source_record, monkeypatch, caplog
):
    native = tmp_path / "incoming.ome.zarr"
    make_zarr_v2(native)
    sidecar = write_indexed_canonical_marker(native, source_record)
    original_is_file = Path.is_file

    def deny_biomero_folder(path):
        # Windows has no chmod: simulate EACCES on the BIOMERO folder.
        if path == sidecar.resolve():
            raise PermissionError(13, "Permission denied", str(path))
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", deny_biomero_folder)

    with caplog.at_level("WARNING"):
        assert load_canonical_marker(native) is None

    assert "Cannot look for canonical marker" in caplog.text
    assert str(sidecar) in caplog.text


def test_untraversable_biomero_folder_still_reads_legacy_sidecar(
    tmp_path, source_record, monkeypatch
):
    native = tmp_path / "incoming.ome.zarr"
    make_zarr_v2(native)
    write_legacy_sidecar(native, source_record)
    original_is_file = Path.is_file

    def deny_biomero_folder(path):
        if ".processed" in path.parts:
            raise PermissionError(13, "Permission denied", str(path))
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", deny_biomero_folder)

    assert load_canonical_marker(native) == CanonicalZarrSource.from_dict(
        source_record)


def test_removal_race_between_exists_and_unlink_is_not_an_error(
    tmp_path, monkeypatch, caplog
):
    sidecar = tmp_path / ".biomero" / "x.ome.zarr.canonical.json"
    sidecar.parent.mkdir()
    # Another process removed the file after exists() reported it.
    monkeypatch.setattr(Path, "exists", lambda path: path == sidecar)

    with caplog.at_level("WARNING"):
        removed = canonical_store._remove_sidecar(sidecar)

    assert removed is True
    assert "Could not remove" not in caplog.text


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "a\\b", "../x"])
def test_invalid_processed_folder_falls_back_with_warning(value, caplog):
    with caplog.at_level("WARNING"):
        folder = canonical_store.read_processed_data_folder(
            {"PROCESSED_DATA_FOLDER": value})

    assert folder == ".processed"
    assert "Ignoring invalid PROCESSED_DATA_FOLDER" in caplog.text


def test_absolute_processed_folder_falls_back(tmp_path, caplog):
    folder = canonical_store.read_processed_data_folder(
        {"PROCESSED_DATA_FOLDER": str(tmp_path)})

    assert folder == ".processed"


@pytest.mark.parametrize("environment, expected", [
    ({}, ".processed"), ({"PROCESSED_DATA_FOLDER": "processed"}, "processed")])
def test_valid_processed_folder_is_used(environment, expected, caplog):
    with caplog.at_level("WARNING"):
        assert canonical_store.read_processed_data_folder(
            environment) == expected

    assert not caplog.text


def test_legacy_removal_tolerates_file_already_gone(tmp_path):
    native = tmp_path / "incoming.ome.zarr"
    make_zarr_v2(native)

    assert canonical_store.remove_legacy_canonical_marker(native) is False


def test_write_keeps_updating_internal_marker_in_place(tmp_path, source_record):
    store = CanonicalStore(tmp_path)
    staging = tmp_path / "staging.ome.zarr"
    make_zarr_v2(staging)
    committed = store.commit(staging, source_record)

    marker_path = write_indexed_canonical_marker(committed, source_record)

    assert marker_path == committed / CANONICAL_MARKER_NAME
    assert not (committed.parent / ".biomero").exists()


def test_move_canonical_marker_follows_renamed_zarr_from_both_locations(
    tmp_path, source_record
):
    moved_new = tmp_path / "new.ome.zarr"
    moved_legacy = tmp_path / "legacy.ome.zarr"
    for name in (moved_new, moved_legacy):
        make_zarr_v2(name)
    write_indexed_canonical_marker(moved_new, source_record)
    legacy = write_legacy_sidecar(moved_legacy, source_record)

    for old, new in ((moved_new, tmp_path / "sub" / "a.ome.zarr"),
                     (moved_legacy, tmp_path / "b.ome.zarr")):
        new.parent.mkdir(exist_ok=True)
        old.rename(new)
        target = canonical_store.move_canonical_marker(old, new)
        assert target == external_canonical_marker_path(new)
        assert target.is_file()
        assert not external_canonical_marker_path(old).exists()
    assert not legacy.parent.exists()


def test_move_canonical_marker_never_overwrites_destination_sidecar(
    tmp_path, source_record
):
    old = tmp_path / "old.ome.zarr"
    new = tmp_path / "new.ome.zarr"
    for zarr in (old, new):
        make_zarr_v2(zarr)
        write_indexed_canonical_marker(zarr, source_record)
    destination = external_canonical_marker_path(new)
    destination.write_text("destination wins")

    assert canonical_store.move_canonical_marker(old, new) is None

    assert destination.read_text() == "destination wins"
    assert external_canonical_marker_path(old).is_file()


def test_remove_external_markers_removes_both_and_empty_folders(
    tmp_path, source_record
):
    native = tmp_path / "gone.ome.zarr"
    make_zarr_v2(native)
    write_indexed_canonical_marker(native, source_record)
    legacy = write_legacy_sidecar(native, source_record)

    canonical_store.remove_external_canonical_markers(native)

    assert not legacy.parent.exists()
    assert not sidecar_for(native).parent.exists()
    assert (tmp_path / ".processed").is_dir()


def test_adopts_legacy_marker_without_new_optional_comparison_fields(tmp_path, source_record):
    store = CanonicalStore(tmp_path)
    staging = tmp_path / "staging.zarr"
    make_zarr_v2(staging)
    committed = store.commit(staging, source_record)
    marker_path = committed / CANONICAL_MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker["source"].pop("imageMetadata")
    for key in ("comparisonProfile", "datasetPath", "comparisonShape",
                "comparisonCoordinateTransformations", "codeBits"):
        marker["source"]["pixelIdentity"].pop(key, None)
    marker_path.write_text(json.dumps(marker))
    assert store.adopt(source_record) == committed


def test_external_marker_round_trips_plate_identity_inventory(
    tmp_path, source_record
):
    native = tmp_path / "plate.ome.zarr"
    make_zarr_v2(native)
    plate_image_source = CanonicalZarrSource.from_dict({
        **source_record,
        "relativePath": "plate.ome.zarr",
        "nodePath": "A/1/0",
        "sourceObjectType": "Plate",
        "sourceObjectId": 9,
        "canonicalPixelVerified": False,
    })
    plate = CanonicalPlateSource(
        storageRoot="group-5-data",
        relativePath="plate.ome.zarr",
        sourceObjectId=9,
        sourceGeneration=1,
        interchangeProfile="ngff-0.4-zarr-v2",
        images=(CanonicalPlateImage(
            imageNodePath="A/1/0",
            source=plate_image_source,
        ),),
    )

    write_indexed_canonical_marker(native, plate)

    assert load_canonical_marker(native) == plate


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


# A child process takes the creation lock, reports readiness on stdout and
# then waits for stdin to close, so tests control when it holds the lock.
LOCK_HOLDER_SCRIPT = """
import importlib.util, json, sys
from biomero_schema.zarr import CanonicalZarrSource
spec = importlib.util.spec_from_file_location("canonical_store", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
store = module.CanonicalStore(sys.argv[2])
with store.creation_lock(CanonicalZarrSource.from_dict(json.loads(sys.argv[3]))):
    print("locked", flush=True)
    sys.stdin.read()
"""


@pytest.fixture
def lock_holder(tmp_path, source_record):
    """Start a child process that holds the creation lock of source_record."""
    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER_SCRIPT, str(MODULE_PATH),
         str(tmp_path), json.dumps(source_record)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert holder.stdout.readline().strip() == "locked"
    yield holder
    holder.kill()
    holder.wait(timeout=30)
    holder.stdin.close()
    holder.stdout.close()


def test_creation_lock_is_refused_while_another_process_holds_it(
        tmp_path, source_record, lock_holder):
    store = CanonicalStore(tmp_path)

    with pytest.raises(CanonicalCreationLocked) as excinfo:
        with store.creation_lock(source_record):
            pytest.fail("lock held by another process was acquired")

    assert "not waiting" in str(excinfo.value)


def test_creation_lock_is_free_after_holder_released_it(
        tmp_path, source_record, lock_holder):
    lock_holder.stdin.close()  # the child leaves its with-block and exits
    assert lock_holder.wait(timeout=30) == 0

    with CanonicalStore(tmp_path).creation_lock(source_record) as destination:
        assert destination.name == "Image-3207.ome.zarr"


def test_creation_lock_recovers_after_holder_was_killed(
        tmp_path, source_record, lock_holder, caplog):
    """A hard crash leaves the lock file behind but must not block creation."""
    lock_holder.kill()
    lock_holder.wait(timeout=30)
    lock_files = list((tmp_path / "project" / ".processed").glob("*lock"))
    assert lock_files, "the killed holder should leave its lock file behind"

    with caplog.at_level(logging.INFO, logger=canonical_store.logger.name):
        with CanonicalStore(tmp_path).creation_lock(source_record):
            pass

    assert "Acquired canonical creation lock" in caplog.text


def test_creation_lock_ignores_legacy_pid_lock_file(
        tmp_path, source_record, caplog):
    """The O_EXCL ``<name>.lock`` of older importers must not block."""
    processed = tmp_path / "project" / ".processed"
    processed.mkdir(parents=True)
    legacy_lock = processed / "Image-3207.ome.zarr.lock"
    legacy_lock.write_text(str(os.getpid()), encoding="ascii")

    with caplog.at_level(logging.INFO, logger=canonical_store.logger.name):
        with CanonicalStore(tmp_path).creation_lock(source_record):
            pass

    assert "Ignoring legacy canonical creation lock" in caplog.text
    assert legacy_lock.exists(), "the old file is left for a still-running old importer"


def test_commit_succeeds_with_legacy_lock_file(tmp_path, source_record):
    processed = tmp_path / "project" / ".processed"
    processed.mkdir(parents=True)
    (processed / "Image-3207.ome.zarr.lock").write_text("99999", encoding="ascii")
    staging = tmp_path / "staging.ome.zarr"
    staging.mkdir()
    (staging / "zarr.json").write_text("{}", encoding="utf-8")

    destination = CanonicalStore(tmp_path).commit(staging, source_record)

    assert (destination / CANONICAL_MARKER_NAME).is_file()


def test_creation_lock_file_name_is_pinned(tmp_path, source_record):
    with CanonicalStore(tmp_path).creation_lock(source_record):
        pass

    lock_file = (tmp_path / "project" / ".processed"
                 / ".Image-3207.ome.zarr.biomero-lock")
    assert lock_file.is_file()


def test_commit_is_refused_while_another_process_holds_the_lock(
        tmp_path, source_record, lock_holder):
    staging = tmp_path / "staging.ome.zarr"
    make_zarr_v2(staging)

    with pytest.raises(CanonicalCreationLocked):
        CanonicalStore(tmp_path).commit(staging, source_record)

    assert (staging / ".zgroup").is_file()
    assert not (staging / CANONICAL_MARKER_NAME).exists()
    assert not (tmp_path / source_record["relativePath"]).exists()


@pytest.mark.parametrize("error", [
    OSError(errno.ENOLCK, "no locks available"),
    OSError(errno.ENOSYS, "function not implemented"),
    OSError(errno.EROFS, "read-only file system"),
])
def test_creation_lock_failure_that_is_not_busy_is_not_reported_as_busy(
        tmp_path, source_record, monkeypatch, error):
    def fail(destination):
        raise error

    monkeypatch.setattr(canonical_store, "locked", fail)

    with pytest.raises(canonical_store.CanonicalCreationLockUnavailable,
                       match=f"errno {error.errno}") as excinfo:
        with CanonicalStore(tmp_path).creation_lock(source_record):
            pytest.fail("lock should not be acquired")

    assert not isinstance(excinfo.value, CanonicalCreationLocked)


def test_oserror_in_the_body_is_not_taken_for_a_lock_failure(
        tmp_path, source_record):
    with pytest.raises(OSError, match="body failed") as excinfo:
        with CanonicalStore(tmp_path).creation_lock(source_record):
            raise OSError(errno.EIO, "body failed")

    assert not isinstance(excinfo.value, RuntimeError)
