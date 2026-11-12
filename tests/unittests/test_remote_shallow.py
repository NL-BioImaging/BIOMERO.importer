import hashlib
from unittest.mock import patch

import pytest

from test_result_zarr import _make_image, _manifest, _input, _identity, IdentityProvider
from biomero_schema.imports import ImportOptionsEnvelope, ShallowZarrImportOperation
from biomero_schema.shallower import RemoteShallowReceipt, SHALLOW_OPERATION_REPORT
from biomero_shallower.operations import normalize
from biomero_shallower import __version__ as shallower_version
from biomero_importer.utils.lifecycle import ImportLifecycleEngine


def fixture(tmp_path, monkeypatch):
    root = tmp_path / 'result.zarr'
    _make_image(root, labels=('cells',))
    manifest = _manifest(_input(0))
    monkeypatch.setenv('BIOMERO_SHALLOW_ZARR', 'true')
    monkeypatch.setenv('BIOMERO_REMOTE_SHALLOW_ZARR', 'true')
    monkeypatch.setenv('BIOMERO_REMOTE_SHALLOWER_IMAGE', 'helper:0.1.0')
    monkeypatch.setenv('BIOMERO_REMOTE_SHALLOWER_VERSION', shallower_version)
    monkeypatch.setenv('SLURM_JOB_ID', '123')
    task_id = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
    normalize(root, manifest, identity_provider=IdentityProvider(_identity()),
              image='helper:0.1.0', task_id=task_id)
    receipt = RemoteShallowReceipt(schema=1, image='helper:0.1.0', toolVersion=shallower_version,
                                   artifactPath='result.zarr', slurmJobId='123', taskId=task_id,
                                   reportSha256=hashlib.sha256((root / SHALLOW_OPERATION_REPORT).read_bytes()).hexdigest())
    options = ImportOptionsEnvelope(operations=(ShallowZarrImportOperation(
        canonicalInputs=manifest, remoteReceipts=(receipt,)),))
    return root, options


def test_remote_result_bypasses_identity_and_normalization(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch)
    monkeypatch.delenv('BIOMERO_REMOTE_SHALLOW_ZARR')
    with patch('biomero_importer.utils.lifecycle.evaluate_returned_zarr', side_effect=AssertionError('rehash')), \
         patch('biomero_importer.utils.lifecycle.normalize_returned_zarr', side_effect=AssertionError('renormalize')):
        plan = ImportLifecycleEngine().prepare([root], options)
    assert [(item.path, item.role) for item in plan.items] == [
        (root, 'primary'),
        (root / 'labels/cells', 'image-label'),
    ]


def test_remote_receipt_decision_is_logged(tmp_path, monkeypatch, caplog):
    root, options = fixture(tmp_path, monkeypatch)
    with caplog.at_level('DEBUG', logger='biomero_importer.utils.lifecycle'):
        ImportLifecycleEngine().prepare([root], options)
    assert 'Validating remote shallow receipt' in caplog.text
    assert 'Accepted remote shallow result' in caplog.text


def test_remote_tamper_is_rejected(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch)
    report = root / SHALLOW_OPERATION_REPORT
    report.write_text(report.read_text() + ' ')
    with pytest.raises(ValueError, match='checksum'):
        ImportLifecycleEngine().prepare([root], options)


def test_remote_version_mismatch_is_rejected(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('BIOMERO_REMOTE_SHALLOWER_VERSION', 'unexpected-version')
    with pytest.raises(ValueError, match='administrator configuration'):
        ImportLifecycleEngine().prepare([root], options)


def test_remote_receipt_respects_admin_opt_out(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('BIOMERO_REMOTE_SHALLOW_ZARR', 'false')
    with patch('biomero_importer.utils.lifecycle._recover_result_store',
               side_effect=AssertionError('disabled receipt started recovery')), \
         pytest.raises(ValueError, match='disabled'):
        ImportLifecycleEngine().prepare([root], options)


def test_user_renaming_keeps_original_verified_receipt(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch)
    renamed = root.with_name('renamed.zarr')
    root.rename(renamed)
    plan = ImportLifecycleEngine().prepare([renamed], options)
    assert [(item.path, item.role) for item in plan.items] == [
        (renamed, 'primary'),
        (renamed / 'labels/cells', 'image-label'),
    ]


def test_full_fallback_can_normalize_locally_and_retry(tmp_path, monkeypatch):
    from biomero_shallower.pixel_identity import PixelIdentityError
    from biomero_shallower.result_zarr import evaluate_returned_zarr
    root = tmp_path / 'result.zarr'
    _make_image(root, labels=('cells',))
    manifest = _manifest(_input(0))
    monkeypatch.setenv('BIOMERO_SHALLOW_ZARR', 'true')
    with patch('biomero_shallower.result_zarr._identity_for_node',
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


@pytest.mark.parametrize('cache_writable', [True, False])
def test_kept_full_receipt_refreshes_source_cache_without_repeating_comparison(
        tmp_path, monkeypatch, caplog, cache_writable):
    from contextlib import nullcontext
    from biomero_importer.utils.canonical_store import load_canonical_marker
    from pathlib import Path
    root = tmp_path / 'result.zarr'
    _make_image(root, labels=('cells',))
    item = _input(0, artifact='input.zarr')
    source = item.source.model_copy(update={'pixel_identity': None})
    item = item.model_copy(update={'source': source})
    original = tmp_path / source.relative_path
    _make_image(original)
    manifest = _manifest(item)

    class DifferentResult:
        def generate(self, path, **kwargs):
            return _identity('ISCC:ISOURCE' if Path(path) == original else 'ISCC:ICHANGED')

    monkeypatch.setenv('BIOMERO_SHALLOW_ZARR', 'true')
    monkeypatch.setenv('BIOMERO_REMOTE_SHALLOWER_IMAGE', 'helper:0.1.0')
    monkeypatch.setenv('BIOMERO_REMOTE_SHALLOWER_VERSION', shallower_version)
    monkeypatch.setenv('SLURM_JOB_ID', '123')
    task_id = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
    report = normalize(root, manifest, identity_provider=DifferentResult(),
                       storage_roots={'group-0-data': tmp_path},
                       image='helper:0.1.0', task_id=task_id)
    assert report.result == 'kept-full'
    assert report.refreshed_inputs is not None
    receipt = RemoteShallowReceipt(schema=1, image='helper:0.1.0', toolVersion=shallower_version,
        artifactPath='result.zarr', slurmJobId='123', taskId=task_id,
        reportSha256=hashlib.sha256((root / SHALLOW_OPERATION_REPORT).read_bytes()).hexdigest())
    options = ImportOptionsEnvelope(operations=(ShallowZarrImportOperation(
        canonicalInputs=manifest, remoteReceipts=(receipt,)),))
    cache_write = (nullcontext() if cache_writable else patch(
        'biomero_importer.utils.canonical_store.write_indexed_canonical_marker',
        side_effect=PermissionError('read-only metadata directory')))
    with cache_write, \
         patch('biomero_importer.utils.lifecycle.evaluate_returned_zarr', side_effect=AssertionError('rehash')), \
         patch('biomero_importer.utils.lifecycle.load_managed_storage_roots', return_value={'group-0-data': tmp_path}):
        prepared = ImportLifecycleEngine().prepare([root], options)
    assert [item.role for item in prepared.items] == ['primary', 'image-label']
    if cache_writable:
        assert load_canonical_marker(original).pixel_identity.instance_code == 'ISCC:ISOURCE'
    else:
        assert load_canonical_marker(original) is None
        assert 'continuing with the validated result' in caplog.text
    assert (root / '0/0.0.0.0').exists()
