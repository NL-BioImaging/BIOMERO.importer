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
import hashlib
import json
from unittest.mock import patch

import pytest

from test_result_zarr import _make_image, _manifest, _input, _identity, IdentityProvider
from biomero_schema.imports import ImportOptionsEnvelope, ShallowZarrImportOperation
from biomero_schema.shallower import RemoteShallowReceipt, SHALLOW_OPERATION_REPORT
from biomero_shallower.operations import normalize
from biomero_shallower import __version__ as shallower_version
from biomero_importer.utils.lifecycle import ImportLifecycleEngine
from biomero_importer.utils.result_zarr import ReturnedZarrDecision


def fixture(tmp_path, monkeypatch, helper_version=shallower_version, provider=None):
    root = tmp_path / 'result.zarr'
    _make_image(root, labels=('cells',))
    manifest = _manifest(_input(0))
    monkeypatch.setenv('BIOMERO_SHALLOW_ZARR', 'true')
    monkeypatch.setenv('BIOMERO_REMOTE_SHALLOW_ZARR', 'true')
    monkeypatch.setenv('SLURM_JOB_ID', '123')
    task_id = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
    with patch('biomero_shallower.operations.__version__', helper_version):
        normalize(root, manifest, identity_provider=provider or IdentityProvider(_identity()),
                  image='helper:0.1.0', task_id=task_id)
    receipt = RemoteShallowReceipt(schema=1, image='helper:0.1.0', toolVersion=helper_version,
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


def test_remote_kept_full_is_logged(tmp_path, monkeypatch, caplog):
    root, options = fixture(tmp_path, monkeypatch)
    manifest = options.operations[0].canonical_inputs
    # A full result reported by the helper: the returned pixels differ.
    import shutil
    shutil.rmtree(root)
    _make_image(root, labels=('cells',))
    report = normalize(root, manifest, identity_provider=IdentityProvider(_identity('ISCC:ICHANGED')),
                       image='helper:0.1.0', task_id='cccccccc-cccc-cccc-cccc-cccccccccccc')
    assert report.result == 'kept-full'
    receipt = options.operations[0].remote_receipts[0].model_copy(update={
        'report_sha256': hashlib.sha256((root / SHALLOW_OPERATION_REPORT).read_bytes()).hexdigest()})
    options = ImportOptionsEnvelope(operations=(ShallowZarrImportOperation(
        canonicalInputs=manifest, remoteReceipts=(receipt,)),))
    with caplog.at_level('INFO', logger='biomero_importer.utils.lifecycle'):
        ImportLifecycleEngine().prepare([root], options)
    assert 'Remote shallowing kept the full result' in caplog.text


def test_remote_result_without_receipt_is_evaluated_locally_with_warning(tmp_path, monkeypatch, caplog):
    root, options = fixture(tmp_path, monkeypatch)
    manifest = options.operations[0].canonical_inputs
    import shutil
    shutil.rmtree(root)
    _make_image(root, labels=('cells',))
    normalize(root, manifest, identity_provider=IdentityProvider(_identity('ISCC:ICHANGED')),
              image='helper:0.1.0')
    options = ImportOptionsEnvelope(operations=(ShallowZarrImportOperation(
        canonicalInputs=manifest),))
    keep_full = ReturnedZarrDecision(
        store_path=root, outcome='keep-full', reason='pixels-differ')
    with patch('biomero_importer.utils.lifecycle.evaluate_returned_zarr',
               return_value=keep_full) as evaluate:
        ImportLifecycleEngine().prepare([root], options)
    assert evaluate.call_count == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
    assert len([m for m in warnings if 'no receipt; evaluating locally' in m]) == 1
    assert (root / '.biomero-remote-attempt.json').is_file()
    assert not (root / SHALLOW_OPERATION_REPORT).exists()


def test_remote_tamper_is_rejected(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch)
    report = root / SHALLOW_OPERATION_REPORT
    report.write_text(report.read_text() + ' ')
    with pytest.raises(ValueError, match='checksum'):
        ImportLifecycleEngine().prepare([root], options)


def _assert_single_version_warning(caplog):
    version_warnings = [
        record for record in caplog.records
        if record.levelname == 'WARNING' and 'differs from installed' in record.getMessage()]
    assert len(version_warnings) == 1
    assert '0.0.1' in version_warnings[0].getMessage()
    assert shallower_version in version_warnings[0].getMessage()


def test_remote_version_mismatch_with_compatible_report_is_accepted(tmp_path, monkeypatch, caplog):
    root, options = fixture(tmp_path, monkeypatch, helper_version='0.0.1')
    plan = ImportLifecycleEngine().prepare([root], options)
    assert [(item.path, item.role) for item in plan.items] == [
        (root, 'primary'), (root / 'labels/cells', 'image-label')]
    provenance = json.loads((root / '.biomero-import-storage.json').read_text())
    assert provenance['storage'] == 'shallow-zarr'
    assert provenance['tool_version'] == '0.0.1'
    _assert_single_version_warning(caplog)


def test_remote_version_mismatch_keeps_full_result(tmp_path, monkeypatch, caplog):
    root, options = fixture(tmp_path, monkeypatch, helper_version='0.0.1',
                            provider=IdentityProvider(_identity('ISCC:ICHANGED')))
    plan = ImportLifecycleEngine().prepare([root], options)
    assert [(item.path, item.role) for item in plan.items][0] == (root, 'primary')
    provenance = json.loads((root / '.biomero-import-storage.json').read_text())
    assert provenance['storage'] == 'full-zarr'
    _assert_single_version_warning(caplog)


def _receipt_for(root, options, helper_version):
    operation = options.operations[0]
    receipt = operation.remote_receipts[0].model_copy(update={
        'tool_version': helper_version,
        'report_sha256': hashlib.sha256((root / SHALLOW_OPERATION_REPORT).read_bytes()).hexdigest()})
    return ImportOptionsEnvelope(operations=(
        operation.model_copy(update={'remote_receipts': (receipt,)}),))


def _prepare_with_unreadable_report(tmp_path, monkeypatch, helper_version):
    root, options = fixture(tmp_path, monkeypatch, helper_version=helper_version)
    report = root / SHALLOW_OPERATION_REPORT
    report.write_text(report.read_text().replace('"schema": 1', '"schema": 2', 1))
    options = _receipt_for(root, options, helper_version)
    return ImportLifecycleEngine().prepare([root], options)


def test_remote_result_from_newer_shallower_fails_naming_both_versions(tmp_path, monkeypatch, caplog):
    newer_version = '99.0.0'
    with pytest.raises(ValueError) as raised:
        _prepare_with_unreadable_report(tmp_path, monkeypatch, newer_version)
    message = str(raised.value)
    assert 'written by a newer biomero-shallower' in message
    assert newer_version in message
    assert f'than the one installed ({shallower_version})' in message
    assert 'Install the matching biomero-shallower' in message
    assert 'validated with the installed package' not in caplog.text


def test_remote_result_from_older_shallower_with_parse_failure_is_neutral(tmp_path, monkeypatch):
    older_version = '0.0.1'
    with pytest.raises(ValueError) as raised:
        _prepare_with_unreadable_report(tmp_path, monkeypatch, older_version)
    message = str(raised.value)
    assert 'newer' not in message
    assert older_version in message
    assert f'cannot be read by installed {shallower_version}' in message


def test_remote_version_mismatch_integrity_failure_keeps_original_message(tmp_path, monkeypatch):
    root, options = fixture(tmp_path, monkeypatch, helper_version='0.0.1')
    # Report claims another version than the receipt: an integrity failure.
    options = _receipt_for(root, options, '0.0.2')
    with pytest.raises(ValueError, match='Unexpected helper version') as raised:
        ImportLifecycleEngine().prepare([root], options)
    assert 'Install the matching' not in str(raised.value)


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
    with patch('biomero_shallower.evaluation._identity_for_node',
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
        assert (original.parent / '.biomero' / 'Image-1.ome.zarr.canonical.json').is_file()
    else:
        assert load_canonical_marker(original) is None
        assert 'continuing with the validated result' in caplog.text
    assert (root / '0/0.0.0.0').exists()
