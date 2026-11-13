"""Processed output paths follow the container environment at startup."""

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest


def load_module(name):
    spec = importlib.util.find_spec(f"biomero_importer.utils.{name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "configured, expected", [(None, ".processed"), ("processed", "processed")]
)
def test_processed_paths_follow_environment(
    monkeypatch, tmp_path, configured, expected
):
    if configured is None:
        monkeypatch.delenv("PROCESSED_DATA_FOLDER", raising=False)
    else:
        monkeypatch.setenv("PROCESSED_DATA_FOLDER", configured)

    # The importer takes the folder name from canonical_store, which reads the
    # environment once at import: load a fresh copy for this environment.
    utils_package = importlib.import_module("biomero_importer.utils")
    original = importlib.import_module("biomero_importer.utils.canonical_store")
    monkeypatch.delitem(sys.modules, "biomero_importer.utils.canonical_store")
    # Restore the package attribute too, so other tests keep the original.
    monkeypatch.setattr(utils_package, "canonical_store", original)
    importer = load_module("importer")
    canonical_store = load_module("canonical_store")
    processor = importer.DataProcessor({
        "UUID": "test-order",
        "preprocessing_container": "example/converter:latest",
        "preprocessing_inputfile": "{Files}",
        "preprocessing_outputfolder": "/data",
        "preprocessing_altoutputfolder": "/out",
    })

    _, args, mounts = processor.get_preprocessing_args(
        str(tmp_path / "image.tif")
    )

    assert (tmp_path / expected).is_dir()
    assert args[args.index("--outputfolder") + 1] == os.path.join(
        "/data", expected
    )
    assert (str(tmp_path), "/data") in mounts
    store = canonical_store.CanonicalStore(tmp_path)
    relative = store.relative_path_for("project", "Image", 3207, 1)
    assert relative == Path("project") / expected / "Image-3207.ome.zarr"
    assert store.resolve(relative) == tmp_path / relative
