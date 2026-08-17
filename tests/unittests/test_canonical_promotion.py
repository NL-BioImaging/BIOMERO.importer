import importlib.util
import json
from pathlib import Path

import pytest
from biomero_schema.zarr import PixelIdentity


def load_module(name, relative_path):
    path = Path(__file__).parents[2] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


canonical_store = load_module(
    "canonical_store", "biomero_importer/utils/canonical_store.py"
)
pixel_identity = load_module(
    "pixel_identity", "biomero_importer/utils/pixel_identity.py"
)
canonical_promotion = load_module(
    "canonical_promotion", "biomero_importer/utils/canonical_promotion.py"
)

CanonicalPromotionService = canonical_promotion.CanonicalPromotionService
CanonicalPixelMismatch = canonical_promotion.CanonicalPixelMismatch


def identity(instance="ISCC:IINSTANCE", shape=(1, 1, 8, 8), node_path="."):
    return PixelIdentity(
        node_path=node_path,
        role="image",
        iscc_code="ISCC:KSUM",
        data_code="ISCC:GDATA",
        instance_code=instance,
        tool_version="0.1.0",
        imagewalk_revision="iscc-bio/0.1.0@revision",
        shape=shape,
        dtype="uint16",
        axes=("t", "c", "y", "x"),
        coordinate_transformations=(
            {"type": "scale", "scale": [1, 1, 0.5, 0.5]},
        ),
    )


def make_zarr(path):
    path.mkdir(parents=True)
    (path / ".zgroup").write_text('{"zarr_format": 2}', encoding="utf-8")


def service(tmp_path):
    return CanonicalPromotionService(
        storage_root_id="group-3-data",
        storage_root=tmp_path / "managed",
        canonical_store_factory=canonical_store.CanonicalStore,
        identities_match=pixel_identity.pixel_identities_match,
    )


def test_verifies_and_atomically_promotes_first_export(tmp_path):
    staging = tmp_path / "workflow" / "result.zarr"
    make_zarr(staging)
    promotion = service(tmp_path)

    result = promotion.promote(
        staging,
        source_directory="project/dataset",
        source_object_type="Image",
        source_object_id=3207,
        source_generation=1,
        node_path=".",
        original_identity=identity(),
        exported_identity=identity(),
        pixel_identity_origin="omero-pixels",
    )

    expected = (
        tmp_path
        / "managed/project/dataset/.processed/Image-3207.g1.ome.zarr"
    )
    assert result.path == expected
    assert not staging.exists()
    assert result.source.storage_root == "group-3-data"
    assert result.source.relative_path == (
        "project/dataset/.processed/Image-3207.g1.ome.zarr"
    )
    assert result.source.pixel_identity == identity()
    assert result.source.canonical_pixel_verified is True
    marker = json.loads(
        (expected / canonical_store.CANONICAL_MARKER_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert marker["source"] == result.source.to_dict()


def test_mismatch_never_promotes_or_removes_staging(tmp_path):
    staging = tmp_path / "workflow" / "result.zarr"
    make_zarr(staging)

    with pytest.raises(CanonicalPixelMismatch, match="Image 3207"):
        service(tmp_path).promote(
            staging,
            source_directory="project",
            source_object_type="Image",
            source_object_id=3207,
            source_generation=1,
            node_path=".",
            original_identity=identity(),
            exported_identity=identity(instance="ISCC:IDIFFERENT"),
            pixel_identity_origin="omero-pixels",
        )

    assert staging.is_dir()
    assert not (tmp_path / "managed/project/.processed").exists()


def test_rejects_identity_for_a_different_node(tmp_path):
    staging = tmp_path / "workflow" / "result.zarr"
    make_zarr(staging)

    with pytest.raises(CanonicalPixelMismatch, match="node path"):
        service(tmp_path).promote(
            staging,
            source_directory="project",
            source_object_type="Image",
            source_object_id=3207,
            source_generation=1,
            node_path="0",
            original_identity=identity(node_path="."),
            exported_identity=identity(node_path="."),
            pixel_identity_origin="omero-pixels",
        )


def test_adopts_matching_commit_after_annotation_gap(tmp_path):
    first_staging = tmp_path / "workflow-1" / "result.zarr"
    make_zarr(first_staging)
    promotion = service(tmp_path)
    first = promotion.promote(
        first_staging,
        source_directory="project",
        source_object_type="Image",
        source_object_id=3207,
        source_generation=1,
        node_path=".",
        original_identity=identity(),
        exported_identity=identity(),
        pixel_identity_origin="omero-pixels",
    )
    retry_staging = tmp_path / "workflow-2" / "result.zarr"
    make_zarr(retry_staging)

    adopted = promotion.promote(
        retry_staging,
        source_directory="project",
        source_object_type="Image",
        source_object_id=3207,
        source_generation=1,
        node_path=".",
        original_identity=identity(),
        exported_identity=identity(),
        pixel_identity_origin="omero-pixels",
    )

    assert adopted.path == first.path
    assert adopted.source == first.source
    assert retry_staging.is_dir()
