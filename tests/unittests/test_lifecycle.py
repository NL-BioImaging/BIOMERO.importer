# -*- coding: utf-8 -*-
# Copyright (C) 2026 Torec Luik
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
import errno
import json
import os
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

from biomero_schema.imports import (
    IMPORT_STORAGE_FILENAME,
    ImporterFullImportStorageRecord,
    ImporterShallowImportStorageRecord,
    ImportOptionsEnvelope,
    RemoteImportStorageRecord,
    ShallowZarrImportOperation,
    parse_import_storage_record,
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

from biomero_importer.utils.lifecycle import (
    ImportLifecycleEngine,
    _prune_committed,
    write_import_storage_record,
)
from biomero_importer.utils.importer import DataPackageImporter
from biomero_importer.utils.pixel_identity import PixelIdentityError
from biomero_importer.utils.result_zarr import (
    NormalizedShallowResult,
    ReturnedZarrDecision,
)
from biomero_shallower import __version__ as shallower_version
from biomero_shallower.operations import locked
from biomero_shallower.transaction import begin, commit, rollback_path


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


@pytest.mark.parametrize("metadata_name", [".zattrs", "zarr.json"])
def test_interrupted_pruning_is_recovered_before_reading_store(
    tmp_path, monkeypatch, metadata_name,
):
    monkeypatch.delenv("BIOMERO_RESULT_RECOVERY", raising=False)
    root = _zarr(tmp_path)
    (root / ".zattrs").unlink()
    metadata = root / metadata_name
    original_metadata = b'{"original": true}'
    metadata.write_bytes(original_metadata)
    pixels = root / "0"
    pixels.mkdir()
    (pixels / "chunk").write_bytes(bytes(range(256)))
    rollback = root.parent / f".{root.name}.biomero-prune-crash"
    rollback.mkdir()
    begin(root, rollback, [pixels], {metadata: original_metadata})
    pixels.rename(rollback / "0")
    metadata.write_text('{"partial": true}', encoding="utf-8")
    (root / ".biomero-shallow.json").write_text("incomplete", encoding="utf-8")
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")

    def evaluate(*args, **kwargs):
        assert (pixels / "chunk").read_bytes() == bytes(range(256))
        assert metadata.read_bytes() == original_metadata
        assert not rollback.exists()
        assert not (root / ".biomero-shallow.json").exists()
        return ReturnedZarrDecision(
            store_path=root, outcome="keep-full", reason="different-pixels")

    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr", evaluate)
    plan = ImportLifecycleEngine().prepare([root], _options())
    assert plan.items[0].path == root


def test_conflicting_recovery_journal_blocks_registration(tmp_path, monkeypatch):
    monkeypatch.delenv("BIOMERO_RESULT_RECOVERY", raising=False)
    root = _zarr(tmp_path)
    pixels = root / "0"
    pixels.mkdir()
    rollback = root.parent / f".{root.name}.biomero-prune-crash"
    rollback.mkdir()
    begin(root, rollback, [pixels], {})
    (rollback / "0").mkdir()
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: pytest.fail("read an unrecovered store"))
    with pytest.raises(ValueError, match="Conflicting recovery"):
        ImportLifecycleEngine().prepare([root], _options())
    assert pixels.exists() and (rollback / "0").exists()


def test_busy_store_is_not_evaluated_or_modified(tmp_path, monkeypatch):
    monkeypatch.delenv("BIOMERO_RESULT_RECOVERY", raising=False)
    root = _zarr(tmp_path)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: pytest.fail("read a concurrently modified store"))
    with locked(root):
        with pytest.raises((ValueError, OSError)):
            ImportLifecycleEngine().prepare([root], _options())


@pytest.mark.parametrize("value", [None, "", "false", "TRUE"])
def test_shallow_recovery_is_automatic_without_a_feature_flag(
    tmp_path, monkeypatch, value,
):
    root = _zarr(tmp_path)
    if value is None:
        monkeypatch.delenv("BIOMERO_RESULT_RECOVERY", raising=False)
    else:
        monkeypatch.setenv("BIOMERO_RESULT_RECOVERY", value)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")

    recovered = Mock()
    monkeypatch.setattr("biomero_importer.utils.lifecycle.recover", recovered)

    def evaluate(*args, **kwargs):
        recovered.assert_called_once_with(root)
        return ReturnedZarrDecision(store_path=root, outcome="eligible", reason="matched")

    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        evaluate)
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.normalize_returned_zarr",
        lambda *a, **k: NormalizedShallowResult(
            store_path=root, manifest=_manifest(), bytes_before=None, bytes_after=None))
    plan = ImportLifecycleEngine().prepare([root], _options())
    assert plan.items[0].path == root
    assert len(plan.items) == 2


def test_unclassified_lock_error_does_not_fall_back(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    monkeypatch.delenv("BIOMERO_RESULT_RECOVERY", raising=False)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")

    def unsupported(*args, **kwargs):
        raise OSError("filesystem does not support locks")

    monkeypatch.setattr("biomero_importer.utils.lifecycle.locked", unsupported)
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *a, **k: pytest.fail("unsafe unlocked evaluation"))
    with pytest.raises(OSError, match="does not support locks"):
        ImportLifecycleEngine().prepare([root], _options())


@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EOPNOTSUPP])
def test_unsupported_lock_uses_legacy_normalization(tmp_path, monkeypatch, caplog, code):
    root = _zarr(tmp_path)
    monkeypatch.delenv("BIOMERO_RESULT_RECOVERY", raising=False)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")

    def unsupported(*args, **kwargs):
        raise OSError(code, "locking unsupported")

    monkeypatch.setattr("biomero_importer.utils.lifecycle.locked", unsupported)
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.recover",
        lambda *a: pytest.fail("recovery without a lock"))
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *a, **k: ReturnedZarrDecision(
            store_path=root, outcome="eligible", reason="matched"))
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.normalize_returned_zarr",
        lambda *a, **k: NormalizedShallowResult(
            store_path=root, manifest=_manifest(), bytes_before=None, bytes_after=None))
    plan = ImportLifecycleEngine().prepare([root], _options())
    assert plan.items[0].path == root
    assert len(plan.items) == 2
    assert "legacy" in caplog.text.lower()


def test_unsupported_lock_cannot_bypass_interrupted_prune(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    (root.parent / f".{root.name}.biomero-prune-crash").mkdir()
    monkeypatch.delenv("BIOMERO_RESULT_RECOVERY", raising=False)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "locking unsupported")

    monkeypatch.setattr("biomero_importer.utils.lifecycle.locked", unsupported)
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *a, **k: pytest.fail("read an interrupted prune without recovery"))
    with pytest.raises(ValueError, match="Interrupted shallow transaction"):
        ImportLifecycleEngine().prepare([root], _options())


def test_unsupported_error_during_recovery_is_not_a_lock_fallback(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    monkeypatch.delenv("BIOMERO_RESULT_RECOVERY", raising=False)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")

    def failed_recovery(*args):
        raise OSError(errno.EOPNOTSUPP, "recovery operation unsupported")

    monkeypatch.setattr("biomero_importer.utils.lifecycle.recover", failed_recovery)
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *a, **k: pytest.fail("ignored failed recovery"))
    with pytest.raises(OSError, match="recovery operation unsupported"):
        ImportLifecycleEngine().prepare([root], _options())


def test_incomplete_journal_stops_automatic_recovery(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    journal = root.parent / f".{root.name}.biomero-prune-crash"
    journal.mkdir()
    (journal / "uncommitted-chunk").write_bytes(b"retained")
    monkeypatch.delenv("BIOMERO_RESULT_RECOVERY", raising=False)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *a, **k: pytest.fail("read an unrecovered store"))
    with pytest.raises(ValueError, match="Incomplete recovery journal"):
        ImportLifecycleEngine().prepare([root], _options())


@pytest.mark.parametrize("shallow_flag", [None, "false", "true"])
def test_no_operations_preserve_paths_without_execution(
    tmp_path, monkeypatch, shallow_flag,
):
    if shallow_flag is None:
        monkeypatch.delenv("BIOMERO_SHALLOW_ZARR", raising=False)
    else:
        monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", shallow_flag)
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle._recover_result_store",
        Mock(side_effect=AssertionError("ordinary import attempted shallow recovery")),
    )
    path = _zarr(tmp_path)

    plan = ImportLifecycleEngine().prepare([path], None)

    assert [item.path for item in plan.items] == [path]
    assert [item.role for item in plan.items] == ["input"]
    assert plan.changed is False


@pytest.mark.parametrize("shallow_flag", [None, "false", "FALSE", ""])
def test_disabled_shallow_operation_cannot_start_recovery(
    tmp_path, monkeypatch, shallow_flag,
):
    if shallow_flag is None:
        monkeypatch.delenv("BIOMERO_SHALLOW_ZARR", raising=False)
    else:
        monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", shallow_flag)
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle._recover_result_store",
        Mock(side_effect=AssertionError("disabled shallow recovery was entered")),
    )

    with pytest.raises(ValueError, match="disabled"):
        ImportLifecycleEngine().prepare([_zarr(tmp_path)], _options())


@pytest.mark.parametrize("reason, level", [
    ("pixels-differ", "INFO"),
    ("identity-unavailable: boom", "WARNING"),
])
def test_keep_full_decision_is_logged_at_matching_level(tmp_path, monkeypatch, caplog, reason, level):
    root = _zarr(tmp_path)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: ReturnedZarrDecision(
            store_path=root, outcome="keep-full", reason=reason),
    )
    with caplog.at_level("INFO", logger="biomero_importer.utils.lifecycle"):
        ImportLifecycleEngine().prepare([root], _options())
    records = [r for r in caplog.records
               if "Returned Zarr decision" in r.getMessage() and reason in r.getMessage()]
    assert [r.levelname for r in records] == [level]


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


def test_eligible_image_keeps_primary_and_adds_label_registration_view(tmp_path, monkeypatch):
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
        (root, "primary"),
        (root / "labels" / "nuclei", "image-label")
    ]
    assert plan.decisions == (decision,)
    assert not (root / TRANSFER_INPUT_MARKER).exists()
    provenance = json.loads((root / '.biomero-import-storage.json').read_text())
    assert provenance == {
        'schema': 1,
        'workflow_id': str(WORKFLOW_ID),
        'storage': 'shallow-zarr',
        'location': 'importer',
        'tool_version': shallower_version,
        'decision_basis': 'matched',
    }


@pytest.mark.parametrize("error", [PixelIdentityError("not matching"), OSError(errno.EIO, "io")])
def test_pruning_failure_keeps_and_imports_full_result(tmp_path, monkeypatch, caplog, error):
    root = _zarr(tmp_path)
    recovered = Mock()
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr("biomero_importer.utils.lifecycle.recover", recovered)
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: ReturnedZarrDecision(
            store_path=root, outcome="eligible", reason="matched"),
    )

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.normalize_returned_zarr", fail)

    plan = ImportLifecycleEngine().prepare([root], _options())

    assert [(item.path, item.role) for item in plan.items] == [(root, "primary")]
    provenance = json.loads((root / ".biomero-import-storage.json").read_text())
    assert provenance["reason"].startswith("normalization-failed")
    assert provenance == {
        "schema": 1,
        "workflow_id": str(WORKFLOW_ID),
        "storage": "full-zarr",
        "location": "importer",
        "tool_version": shallower_version,
        "reason": provenance["reason"],
    }
    assert "importing the full result" in caplog.text
    assert recovered.call_count == 2  # store recovery plus post-failure check


def test_failure_after_commit_uses_committed_shallow_result(tmp_path, monkeypatch, caplog):
    root = _zarr(tmp_path)
    manifest = _manifest()
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: ReturnedZarrDecision(
            store_path=root, outcome="eligible", reason="matched"),
    )
    recovered = Mock()
    monkeypatch.setattr("biomero_importer.utils.lifecycle.recover", recovered)

    def commit_then_fail(*args, **kwargs):
        # Manifest committed, journal already gone.
        (root / ".biomero-shallow.json").write_text("{}", encoding="utf-8")
        raise OSError(errno.EIO, "cleanup failed")

    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.normalize_returned_zarr", commit_then_fail)
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle._load_shallow_manifest",
        lambda path: manifest if (path / ".biomero-shallow.json").exists() else None)

    plan = ImportLifecycleEngine().prepare([root], _options())

    assert (root, "primary") in [(item.path, item.role) for item in plan.items]
    provenance = json.loads((root / ".biomero-import-storage.json").read_text())
    assert provenance["storage"] == "shallow-zarr"
    assert "committed shallow result" in caplog.text
    assert recovered.call_count == 1  # only the store recovery before evaluation


def _real_pruning_setup(tmp_path, monkeypatch, replace):
    from functools import partial
    from test_result_zarr import (
        _make_image, _manifest as _result_manifest, _input,
        _identity as _result_identity, IdentityProvider)
    from biomero_importer.utils.result_zarr import (
        evaluate_returned_zarr, normalize_returned_zarr)
    root = tmp_path / "result.zarr"
    _make_image(root, labels=("cells",))
    manifest = _result_manifest(_input(0, "result.zarr"))
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        partial(evaluate_returned_zarr,
                identity_provider=IdentityProvider(_result_identity())))
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.normalize_returned_zarr",
        partial(normalize_returned_zarr, move=replace))
    options = ImportOptionsEnvelope(operations=(ShallowZarrImportOperation(
        canonicalInputs=manifest),))
    return root, options


def _failing_replace(source, target):
    raise OSError(errno.EIO, "move failed")


def test_real_pruning_failure_restores_arrays_and_imports_full(tmp_path, monkeypatch):
    root, options = _real_pruning_setup(tmp_path, monkeypatch, _failing_replace)

    plan = ImportLifecycleEngine().prepare([root], options)

    assert (root / "0" / "0.0.0.0").is_file()
    assert not (root / ".biomero-shallow.json").exists()
    assert not list(tmp_path.glob(".result.zarr.biomero-prune-*"))
    assert plan.items[0].path == root
    provenance = json.loads((root / ".biomero-import-storage.json").read_text())
    assert provenance["storage"] == "full-zarr"


def test_real_pruning_failure_with_failed_recovery_does_not_fall_back(tmp_path, monkeypatch):
    root, options = _real_pruning_setup(tmp_path, monkeypatch, _failing_replace)

    def broken(*args, **kwargs):
        raise ValueError("Conflicting recovery paths; refusing data loss")

    monkeypatch.setattr("biomero_shallower.transaction.recover", broken)
    # The store recovery before evaluation succeeds; later calls fail.
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.recover",
        Mock(side_effect=[None, ValueError("Conflicting recovery paths")]))

    with pytest.raises(ValueError, match="Conflicting recovery"):
        ImportLifecycleEngine().prepare([root], options)

    assert not (root / ".biomero-import-storage.json").exists()


def test_corrupt_metadata_during_pruning_keeps_full_result(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: ReturnedZarrDecision(
            store_path=root, outcome="eligible", reason="matched"),
    )
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.normalize_returned_zarr",
        Mock(side_effect=json.JSONDecodeError("bad", "{", 0)))
    plan = ImportLifecycleEngine().prepare([root], _options())
    assert [(item.path, item.role) for item in plan.items] == [(root, "primary")]


def _prune_state(tmp_path, journal):
    """A store with a shallow manifest and, optionally, a prune journal.

    ``journal`` is None (no journal), "committed", "uncommitted" or raw text
    written as an unreadable journal.
    """
    root = _zarr(tmp_path)
    (root / ".biomero-shallow.json").write_text("{}", encoding="utf-8")
    if journal is None:
        return root
    rollback = rollback_path(root, "abc")
    rollback.mkdir()
    if journal in ("committed", "uncommitted"):
        written = begin(root, rollback, [], {})
        if journal == "committed":
            commit(written)
    else:
        (rollback / "journal.json").write_text(journal, encoding="utf-8")
    return root


@pytest.mark.parametrize("journal, committed", [
    (None, True),
    ("committed", True),
    ("uncommitted", False),
    ("not json", False),
    ("[]", False),
    ("null", False),
])
def test_prune_committed_reads_the_journal(tmp_path, journal, committed):
    root = _prune_state(tmp_path, journal)
    assert _prune_committed(root, Mock()) is committed


def test_prune_without_manifest_is_not_committed(tmp_path):
    root = _zarr(tmp_path)
    assert _prune_committed(root, Mock()) is False


def test_unreadable_prune_journal_is_logged(tmp_path):
    root = _prune_state(tmp_path, "not json")
    logger = Mock()
    _prune_committed(root, logger)
    assert "treating the prune as uncommitted" in logger.warning.call_args.args[0]


def test_failing_before_commit_rolls_back_and_imports_full(tmp_path, monkeypatch):
    from functools import partial
    from biomero_importer.utils.result_zarr import normalize_returned_zarr

    def refuse_commit(collection):
        raise OSError(errno.EIO, "cannot commit")

    root, options = _real_pruning_setup(tmp_path, monkeypatch, os.replace)
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.normalize_returned_zarr",
        partial(normalize_returned_zarr, before_commit=refuse_commit))

    plan = ImportLifecycleEngine().prepare([root], options)

    assert (root / "0" / "0.0.0.0").is_file()
    assert not (root / ".biomero-shallow.json").exists()
    assert not list(tmp_path.glob(".result.zarr.biomero-prune-*"))
    assert plan.items[0].path == root
    provenance = json.loads((root / ".biomero-import-storage.json").read_text())
    assert provenance["storage"] == "full-zarr"


def test_programming_error_during_pruning_still_fails(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: ReturnedZarrDecision(
            store_path=root, outcome="eligible", reason="matched"),
    )
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.normalize_returned_zarr",
        Mock(side_effect=KeyError("bug")))
    with pytest.raises(KeyError):
        ImportLifecycleEngine().prepare([root], _options())


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


def test_disabling_label_views_still_registers_primary_image(tmp_path, monkeypatch):
    root = _zarr(tmp_path)
    operation = _options().operations[0].model_copy(
        update={"import_image_label_views": False}
    )
    options = ImportOptionsEnvelope(operations=(operation,))
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", "true")
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.evaluate_returned_zarr",
        lambda *args, **kwargs: ReturnedZarrDecision(
            store_path=root, outcome="eligible", reason="matched"
        ),
    )
    monkeypatch.setattr(
        "biomero_importer.utils.lifecycle.normalize_returned_zarr",
        lambda *args, **kwargs: NormalizedShallowResult(
            store_path=root,
            manifest=_manifest(),
            bytes_before=None,
            bytes_after=None,
        ),
    )

    plan = ImportLifecycleEngine().prepare([root], options)

    assert [(item.path, item.role) for item in plan.items] == [
        (root, "primary")
    ]


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
        root,
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


def test_committed_prune_without_manifest_raises(tmp_path):
    from biomero_importer.utils.lifecycle import _load_committed_manifest
    with pytest.raises(PixelIdentityError, match="has no manifest"):
        _load_committed_manifest(_zarr(tmp_path))


@pytest.mark.parametrize("value, default, expected", [
    (None, False, False), (None, True, True), ("TRUE", False, True),
    (" true ", False, True), ("false", True, False), ("yes", True, False),
])
def test_env_flag_is_parsed_in_one_place(monkeypatch, value, default, expected):
    from biomero_importer.utils.shallow_settings import env_flag_enabled
    if value is None:
        monkeypatch.delenv("FLAG_UNDER_TEST", raising=False)
    else:
        monkeypatch.setenv("FLAG_UNDER_TEST", value)
    assert env_flag_enabled("FLAG_UNDER_TEST", default) is expected


def test_shared_flag_parser_is_used_by_importer_module(monkeypatch):
    import importlib
    import biomero_importer.utils.importer as importer_module
    monkeypatch.setenv("BIOMERO_SHALLOW_ZARR", " true ")
    try:
        assert importlib.reload(importer_module).SHALLOW_ZARR_ENABLED is True
    finally:
        monkeypatch.undo()
        importlib.reload(importer_module)


IMPORT_STORAGE_WORKFLOW = "12345678-1234-5678-1234-567812345678"


@pytest.mark.parametrize("record, expected", [
    (RemoteImportStorageRecord(
        schema=1, workflow_id=IMPORT_STORAGE_WORKFLOW, storage="shallow-zarr",
        decision_basis=None, container="helper:1", tool_version="1.0",
        task_id="t", job_id=None, report_sha256="abc"),
     '{\n  "container": "helper:1",\n  "decision_basis": null,\n'
     '  "job_id": null,\n  "location": "remote",\n'
     '  "report_sha256": "abc",\n  "schema": 1,\n'
     '  "storage": "shallow-zarr",\n  "task_id": "t",\n'
     '  "tool_version": "1.0",\n'
     '  "workflow_id": "12345678-1234-5678-1234-567812345678"\n}'),
    (ImporterShallowImportStorageRecord(
        schema=1, workflow_id=IMPORT_STORAGE_WORKFLOW, tool_version="1.0",
        decision_basis="matched"),
     '{\n  "decision_basis": "matched",\n  "location": "importer",\n'
     '  "schema": 1,\n  "storage": "shallow-zarr",\n'
     '  "tool_version": "1.0",\n'
     '  "workflow_id": "12345678-1234-5678-1234-567812345678"\n}'),
    (ImporterFullImportStorageRecord(
        schema=1, workflow_id=IMPORT_STORAGE_WORKFLOW, tool_version="1.0",
        reason="pixels-differ"),
     '{\n  "location": "importer",\n  "reason": "pixels-differ",\n'
     '  "schema": 1,\n  "storage": "full-zarr",\n'
     '  "tool_version": "1.0",\n'
     '  "workflow_id": "12345678-1234-5678-1234-567812345678"\n}'),
], ids=["remote", "importer-shallow", "importer-full"])
def test_import_storage_record_file_keeps_its_persisted_bytes(
        tmp_path, record, expected):
    # Other BIOMERO components read this file: pin field set, null and bytes
    # as written by importer betas 6 to 13.
    write_import_storage_record(tmp_path, record)

    written = (tmp_path / IMPORT_STORAGE_FILENAME).read_text(encoding="utf-8")
    assert written == expected
    assert parse_import_storage_record(json.loads(written)) == record


def test_writing_an_import_storage_record_is_logged(tmp_path, caplog):
    record = ImporterFullImportStorageRecord(
        schema=1, workflow_id=IMPORT_STORAGE_WORKFLOW, tool_version="1.0",
        reason="pixels-differ")

    with caplog.at_level("INFO", logger="biomero_importer.utils.lifecycle"):
        write_import_storage_record(tmp_path, record)

    info = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(info) == 1
    assert "Recording full-zarr on importer" in info[0].getMessage()
    assert IMPORT_STORAGE_FILENAME in info[0].getMessage()
