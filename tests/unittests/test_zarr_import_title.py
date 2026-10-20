import pytest

from biomero_importer.utils.importer import _zarr_import_title


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("/data/example.ome.zarr", "example"),
        (
            "/data/result.ome.zarr/labels/labels_cytoplasm",
            "result_labels_cytoplasm",
        ),
        ("/data/other.zarr/labels/cells", "other_cells"),
        ("/data/plate.zarr/A/1/0/labels/cells", "plate_A_1_0_cells"),
        ("/data/plate.zarr/A/1/1/labels/cells", "plate_A_1_1_cells"),
        ("/data/result.ome.zarr/labels/result_cells", "result_cells"),
        ("/data/result.ome.zarr/labels/cells.v2/", "result_cells.v2"),
        ("/data/series.zarr/0/labels/cells", "series_0_cells"),
        ("/data/series.zarr/1/labels/cells", "series_1_cells"),
        ("/data/result.ome.zarr/labels/custom/cells", "result_custom_cells"),
    ],
)
def test_zarr_import_title_preserves_source_and_label_context(uri, expected):
    assert _zarr_import_title(uri) == expected
