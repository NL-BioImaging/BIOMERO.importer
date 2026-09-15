import hashlib
from unittest.mock import patch

import pytest

from test_result_zarr import _make_image, _manifest, _input, _identity, IdentityProvider
from biomero_schema.imports import ImportOptionsEnvelope, ShallowZarrImportOperation
from biomero_schema.shallower import RemoteShallowReceipt, SHALLOW_OPERATION_REPORT
from biomero_shallower.operations import normalize
from biomero_importer.utils.lifecycle import ImportLifecycleEngine


def fixture(tmp_path, monkeypatch):
    root = tmp_path / 'result.zarr'
    _make_image(root, labels=('cells',))
    manifest = _manifest(_input(0))
    monkeypatch.setenv('BIOMERO_SHALLOW_ZARR', 'true')
    monkeypatch.setenv('BIOMERO_REMOTE_SHALLOW_ZARR', 'true')
    monkeypatch.setenv('BIOMERO_RESULT_NORMALIZER_IMAGE', 'helper:0.1.0')
    monkeypatch.setenv('SLURM_JOB_ID', '123')
    task_id = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
    normalize(root, manifest, identity_provider=IdentityProvider(_identity()),
              image='helper:0.1.0', task_id=task_id)
    receipt = RemoteShallowReceipt(schema=1, image='helper:0.1.0', toolVersion='0.1.0',
                                   artifactPath='result.zarr', slurmJobId='123', taskId=task_id,
                                   reportSha256=hashlib.sha256((root / SHALLOW_OPERATION_REPORT).read_bytes()).hexdigest())
    options = ImportOptionsEnvelope(operations=(ShallowZarrImportOperation(
        canonicalInputs=manifest, remoteReceipts=(receipt,)),))
    return root, options


def test_remote_result_bypasses_identity_and_normalization(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch)
    with patch('biomero_importer.utils.lifecycle.evaluate_returned_zarr', side_effect=AssertionError('rehash')), \
         patch('biomero_importer.utils.lifecycle.normalize_returned_zarr', side_effect=AssertionError('renormalize')):
        plan = ImportLifecycleEngine().prepare([root], options)
    assert plan.items[0].path == root / 'labels/cells'


def test_remote_tamper_is_rejected(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch)
    report = root / SHALLOW_OPERATION_REPORT
    report.write_text(report.read_text() + ' ')
    with pytest.raises(ValueError, match='checksum'):
        ImportLifecycleEngine().prepare([root], options)


def test_remote_receipt_requires_admin_enablement(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch)
    monkeypatch.delenv('BIOMERO_REMOTE_SHALLOW_ZARR')
    with pytest.raises(ValueError, match='disabled'):
        ImportLifecycleEngine().prepare([root], options)


def test_user_renaming_keeps_original_verified_receipt(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch)
    renamed = root.with_name('renamed.zarr')
    root.rename(renamed)
    plan = ImportLifecycleEngine().prepare([renamed], options)
    assert plan.items[0].path == renamed / 'labels/cells'


def test_full_fallback_can_normalize_locally_and_retry(tmp_path, monkeypatch):
    from biomero_shallower.pixel_identity import PixelIdentityError
    from biomero_shallower.result_zarr import evaluate_returned_zarr
    root = tmp_path / 'result.zarr'
    _make_image(root, labels=('cells',))
    manifest = _manifest(_input(0))
    monkeypatch.setenv('BIOMERO_SHALLOW_ZARR', 'true')
    with patch('biomero_shallower.result_zarr._identities_for_nodes',
               side_effect=PixelIdentityError('unavailable')):
        report = normalize(root, manifest)
    assert report.result == 'kept-full'
    decision = evaluate_returned_zarr(root, manifest, identity_provider=IdentityProvider(_identity()))
    options = ImportOptionsEnvelope(operations=(ShallowZarrImportOperation(canonicalInputs=manifest),))
    with patch('biomero_importer.utils.lifecycle.evaluate_returned_zarr', return_value=decision):
        first = ImportLifecycleEngine().prepare([root], options)
    assert (root / '.biomero-remote-attempt.json').exists()
    with patch('biomero_importer.utils.lifecycle.evaluate_returned_zarr', side_effect=AssertionError('rehash')):
        retry = ImportLifecycleEngine().prepare([root], options)
    assert first.items == retry.items
