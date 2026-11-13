"""Refresh identity metadata in place without creating another source/cache."""
from uuid import uuid4

import pytest
from biomero_schema.zarr import CanonicalInput, CanonicalInputManifest, PIXEL_COMPARISON_PROFILE
from test_canonical_promotion import canonical_store, identity


def test_refresh_replaces_existing_internal_marker_and_keeps_pixels(tmp_path):
    from biomero_schema.zarr import CanonicalZarrSource
    root = tmp_path / "source.zarr"
    root.mkdir()
    (root / ".zgroup").write_text('{"zarr_format": 2}')
    pixels = root / "pixels"
    pixels.write_bytes(b"unchanged")
    source = CanonicalZarrSource(storage_root="data", relative_path=root.name,
        node_path=".", source_object_type="Image", source_object_id=7,
        source_generation=2, interchange_profile="ngff-0.4-zarr-v2",
        pixel_identity=identity(), pixel_identity_origin="canonical-bootstrap",
        canonical_pixel_verified=True)
    import json
    (root / canonical_store.CANONICAL_MARKER_NAME).write_text(json.dumps({
        "markerSchema": 1, "state": "committed", "source": source.to_dict()}))
    current = identity().model_copy(update={"comparison_profile": PIXEL_COMPARISON_PROFILE,
        "dataset_path": "s1", "comparison_shape": (1, 1, 4, 4), "schema_version": 2})
    item = CanonicalInput(ordinal=0, selected_object_type="Image", selected_object_id=7,
                          transfer_artifact="input.zarr", source=source)
    expected = CanonicalInputManifest(workflow_id=uuid4(), export_task_id=uuid4(), inputs=(item,))
    refreshed = item.model_copy(update={"source": source.model_copy(update={"pixel_identity": current})})

    assert canonical_store.cache_refreshed_inputs(expected, (refreshed,), {"data": tmp_path}) == 1
    assert canonical_store.load_canonical_marker(root).pixel_identity == current
    assert pixels.read_bytes() == b"unchanged"
    assert not canonical_store.external_canonical_marker_path(root).exists()

    changed = refreshed.model_copy(update={"source": refreshed.source.model_copy(update={"source_generation": 3})})
    with pytest.raises(ValueError, match="source binding"):
        canonical_store.cache_refreshed_inputs(expected, (changed,), {"data": tmp_path})


def _refresh_inputs(tmp_path, make_store):
    """Build an expected/refreshed pair for a source Zarr in ``tmp_path``."""
    from biomero_schema.zarr import CanonicalZarrSource
    root = tmp_path / "source.zarr"
    make_store(root)
    source = CanonicalZarrSource(
        storage_root="data", relative_path=root.name, node_path=".",
        source_object_type="Image", source_object_id=7, source_generation=2,
        interchange_profile="ngff-0.4-zarr-v2", pixel_identity=None,
        pixel_identity_origin="canonical-bootstrap",
        canonical_pixel_verified=True)
    current = identity().model_copy(update={
        "comparison_profile": PIXEL_COMPARISON_PROFILE, "dataset_path": "s1",
        "comparison_shape": (1, 1, 4, 4), "schema_version": 2})
    item = CanonicalInput(
        ordinal=0, selected_object_type="Image", selected_object_id=7,
        transfer_artifact="input.zarr", source=source)
    expected = CanonicalInputManifest(
        workflow_id=uuid4(), export_task_id=uuid4(), inputs=(item,))
    refreshed = item.model_copy(update={
        "source": source.model_copy(update={"pixel_identity": current})})
    return root, expected, refreshed


def _make_valid_zarr(root):
    root.mkdir()
    (root / ".zgroup").write_text('{"zarr_format": 2}')


def _make_unrecognised_directory(root):
    root.mkdir()
    (root / "notes.txt").write_text("not a Zarr")


def test_refresh_with_blocked_biomero_folder_warns_and_continues(
        tmp_path, caplog):
    root, expected, refreshed = _refresh_inputs(tmp_path, _make_valid_zarr)
    # A file where the BIOMERO folder should be: it cannot be created.
    (tmp_path / ".processed").write_text("blocked")

    with caplog.at_level("WARNING"):
        written = canonical_store.cache_refreshed_inputs(
            expected, (refreshed,), {"data": tmp_path})

    assert written == 0
    assert "Could not persist optional comparison metadata" in caplog.text
    assert str(root) in caplog.text
    assert (tmp_path / ".processed").read_text() == "blocked"


def test_refresh_of_unrecognised_store_warns_and_continues(tmp_path, caplog):
    root, expected, refreshed = _refresh_inputs(
        tmp_path, _make_unrecognised_directory)

    with caplog.at_level("WARNING"):
        written = canonical_store.cache_refreshed_inputs(
            expected, (refreshed,), {"data": tmp_path})

    assert written == 0
    assert "Could not persist optional comparison metadata" in caplog.text
    assert not (tmp_path / ".processed").exists()
