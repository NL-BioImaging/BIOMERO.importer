import pytest

from biomero_importer.utils.importer import _zarr_import_title


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("/data/example.ome.zarr", "example"),
        (
            "/data/result.ome.zarr/labels/labels_cytoplasm",
            "labels_cytoplasm",
        ),
    ],
)
def test_zarr_import_title_removes_only_exact_ome_suffix(uri, expected):
    assert _zarr_import_title(uri) == expected
