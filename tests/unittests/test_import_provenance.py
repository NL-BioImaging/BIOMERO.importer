"""Registration must not be repeated to recover mandatory provenance."""

from threading import Event
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from biomero_importer import main
from biomero_importer.utils import importer as module
from biomero_importer.utils import ingest_tracker
from biomero_importer.utils.ingest_tracker import STAGE_INGEST_FAILED


@pytest.fixture
def importer(monkeypatch):
    for name in ('HOST', 'PASSWORD', 'USER', 'PORT'):
        monkeypatch.setenv('OMERO_' + name, 'test')
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    return module.DataPackageImporter({}, {
        'UUID': 'original-order', 'Username': 'requester', 'Group': 'original',
        'DestinationID': 42, 'DestinationType': 'Screen',
        'Files': ['/data/result.zarr'],
    })


@pytest.fixture
def connections(monkeypatch):
    original = MagicMock()
    fresh = MagicMock()
    annotations = []
    for conn in (original, fresh):
        conn.getObject.return_value.listAnnotations.side_effect = (
            lambda **_: list(annotations)
        )
    root = MagicMock()
    root.__enter__.return_value = root
    root.suConn.return_value.__enter__.return_value = fresh
    monkeypatch.setattr(module, 'BlitzGateway', MagicMock(return_value=root))
    monkeypatch.setattr(module.ezomero, 'get_group_id', lambda *args: 17)
    return original, fresh, root, annotations


def upload(importer, conn):
    return importer.upload_files(conn, ['/data/result.zarr'], screen_id=42)


def annotation(values, annotation_id=80):
    result = MagicMock()
    result.getValue.return_value = list(values.items())
    result.getId.return_value = annotation_id
    return result


@pytest.mark.parametrize("alive", [True, False])
def test_registration_renews_expired_connection_before_start(
    importer, connections, monkeypatch, tmp_path, alive,
):
    from biomero_importer.utils import register

    original, fresh, root, _ = connections
    original.keepAlive.return_value = alive
    fresh.keepAlive.return_value = True
    registered = MagicMock()
    registered.getId.return_value.getValue.return_value = 991
    register_plate = MagicMock(return_value=registered)
    monkeypatch.setattr(module, "SHALLOW_ZARR_ENABLED", False)
    monkeypatch.setattr(register, "load_attrs", lambda store: {"plate": {}})
    monkeypatch.setattr(register, "register_plate", register_plate)
    monkeypatch.setattr(register, "link_to_target", MagicMock())
    monkeypatch.setattr(importer, "ensure_import_annotations", MagicMock())
    result_path = tmp_path / "result.ome.zarr"
    result_path.mkdir()

    successful, failed = importer.upload_files(
        original, [str(result_path)], screen_id=42,
    )

    assert not failed
    assert successful[0][3] == 991
    register_plate.assert_called_once()
    assert register_plate.call_args.args[0] is (original if alive else fresh)
    if alive:
        root.suConn.assert_not_called()
    else:
        root.suConn.assert_called_once_with("requester", ttl=6000000)
        fresh.setGroupForSession.assert_called_once_with(17)
        root.suConn.return_value.__exit__.assert_called_once()


def test_registration_disconnect_is_not_replayed(
    importer, connections, monkeypatch, tmp_path,
):
    from biomero_importer.utils import register

    original, _, root, _ = connections
    original.keepAlive.return_value = True
    register_plate = MagicMock(side_effect=ConnectionError("session lost"))
    monkeypatch.setattr(module, "SHALLOW_ZARR_ENABLED", False)
    monkeypatch.setattr(register, "load_attrs", lambda store: {"plate": {}})
    monkeypatch.setattr(register, "register_plate", register_plate)
    result_path = tmp_path / "result.ome.zarr"
    result_path.mkdir()

    successful, failed = importer.upload_files(
        original, [str(result_path)], screen_id=42,
    )

    assert not successful and len(failed) == 1
    register_plate.assert_called_once()
    root.suConn.assert_not_called()
    assert "Reconcile any registered objects before retrying" in (
        importer.data_package["Description"]
    )


def test_session_expiring_during_preparation_is_replaced_for_registration(
    importer, connections, monkeypatch, tmp_path,
):
    from biomero_schema import imports
    from biomero_importer.utils import lifecycle, register

    _, original, _, _ = connections
    original.keepAlive.return_value = True
    replacement = MagicMock()
    replacement.keepAlive.return_value = True
    replacement_root = MagicMock()
    replacement_root.__enter__.return_value = replacement_root
    replacement_root.suConn.return_value.__enter__.return_value = replacement
    module.BlitzGateway.side_effect = [
        module.BlitzGateway.return_value, replacement_root,
    ]
    monkeypatch.setattr(module, "SHALLOW_ZARR_ENABLED", False)
    monkeypatch.setattr(module, "DataProcessor", MagicMock())
    module.DataProcessor.return_value.has_preprocessing.return_value = False
    monkeypatch.setattr(module, "get_tmp_output_path", lambda _: "/nonexistent")
    monkeypatch.setattr(imports, "parse_import_options", lambda _: SimpleNamespace(
        operations=[object()],
    ))
    result_path = tmp_path / "result.ome.zarr"
    result_path.mkdir()
    item = SimpleNamespace(
        role="result", path=result_path, registration=MagicMock(),
    )

    def prepare(*args):
        original.keepAlive.return_value = False
        return SimpleNamespace(items=[item])

    engine = MagicMock()
    engine.prepare.side_effect = prepare
    monkeypatch.setattr(lifecycle, "ImportLifecycleEngine", lambda *args: engine)
    registered = MagicMock()
    registered.getId.return_value.getValue.return_value = 991
    register_plate = MagicMock(return_value=registered)
    monkeypatch.setattr(register, "load_attrs", lambda store: {"plate": {}})
    monkeypatch.setattr(register, "register_plate", register_plate)
    monkeypatch.setattr(register, "link_to_target", MagicMock())
    monkeypatch.setattr(importer, "ensure_import_annotations", MagicMock())

    successful, failed, import_failed = importer.import_data_package()

    assert successful and not failed and not import_failed
    engine.prepare.assert_called_once()
    register_plate.assert_called_once()
    assert register_plate.call_args.args[0] is replacement
    replacement_root.suConn.assert_called_once_with("requester", ttl=6000000)
    replacement.setGroupForSession.assert_called_once_with(17)
    replacement_root.suConn.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize("failure", ["dead-session", "wrong-group"])
def test_replacement_connection_must_be_alive_and_in_requested_group(
    importer, connections, failure,
):
    original, fresh, root, _ = connections
    original.keepAlive.return_value = False
    fresh.keepAlive.return_value = failure != "dead-session"
    fresh.setGroupForSession.return_value = failure != "wrong-group"

    with pytest.raises(ConnectionError):
        importer.create_new_dataset(original)

    fresh.getUpdateService.assert_not_called()
    root.suConn.return_value.__exit__.assert_called_once()


def test_keepalive_logs_false_return():
    root = MagicMock()
    user = MagicMock()
    root.keepAlive.return_value = True
    called = Event()

    def failed_keepalive():
        called.set()
        return False

    user.keepAlive.side_effect = failed_keepalive
    logger = MagicMock()
    with module.omero_connection_keepalive(
        (("root", root), ("user", user)), logger, "test", interval=0.01,
    ):
        assert called.wait(1)
    logger.warning.assert_called()


def test_expired_connection_repairs_exact_registered_plate(
    importer, connections, monkeypatch,
):
    original, fresh, root, _ = connections
    original.getObject.side_effect = RuntimeError('Ice.ConnectionLostException')
    importer.import_zarr = MagicMock(return_value=([991], True))
    post = MagicMock(return_value=80)
    monkeypatch.setattr(module.ezomero, 'post_map_annotation', post)

    successful, failed = upload(importer, original)

    assert not failed
    assert successful[0][3] == 991
    importer.import_zarr.assert_called_once_with(
        original, uri='/data/result.zarr', target=42,
    )
    assert post.call_args.kwargs['conn'] is fresh
    assert post.call_args.kwargs['object_id'] == 991
    root.suConn.assert_called_once_with('requester', ttl=6000000)
    fresh.setGroupForSession.assert_called_once_with(17)


def test_expired_connection_renews_for_annotation_without_reimport(
    importer, connections, monkeypatch,
):
    original, fresh, root, _ = connections
    original.keepAlive.return_value = False
    importer.import_zarr = MagicMock(return_value=([991], True))
    post = MagicMock(return_value=80)
    monkeypatch.setattr(module.ezomero, "post_map_annotation", post)

    successful, failed = upload(importer, original)

    assert successful and not failed
    importer.import_zarr.assert_called_once()
    assert post.call_args.kwargs["conn"] is fresh
    assert post.call_args.kwargs["object_id"] == 991
    root.suConn.assert_called_once_with("requester", ttl=6000000)
    fresh.setGroupForSession.assert_called_once_with(17)


@pytest.mark.parametrize('lost_response', ['exception', 'no_id'])
def test_uncertain_annotation_write_is_reconciled_without_duplicates(
    importer, connections, monkeypatch, lost_response,
):
    original, _, _, annotations = connections
    importer.import_zarr = MagicMock(return_value=([991], True))

    def commit_then_lose_response(**kwargs):
        annotations.append(annotation(kwargs['kv_dict']))
        if lost_response == 'exception':
            raise RuntimeError('Ice.ConnectionLostException')
        return None

    post = MagicMock(side_effect=commit_then_lose_response)
    monkeypatch.setattr(module.ezomero, 'post_map_annotation', post)

    successful, failed = upload(importer, original)

    assert successful and not failed
    assert len(annotations) == 1
    post.assert_called_once()
    importer.import_zarr.assert_called_once()


@pytest.mark.parametrize('failure', ['exception', 'no_id', 'incomplete'])
def test_exhausted_repair_fails_and_preserves_registered_ids(
    importer, connections, monkeypatch, failure,
):
    original, _, _, annotations = connections
    importer.import_zarr = MagicMock(return_value=([991], True))
    post = MagicMock(return_value=None)
    if failure == 'exception':
        post.side_effect = RuntimeError('Ice.ConnectionLostException')
    elif failure == 'incomplete':
        annotations.append(annotation({
            'UUID': 'original-order', 'Filepath': '/data/result.zarr',
        }))
    monkeypatch.setattr(module.ezomero, 'post_map_annotation', post)

    successful, failed = upload(importer, original)

    assert not successful
    assert failed[0][3] == 991
    assert 'registered Plate IDs: [991]' in importer.data_package['Description']
    assert 'do not reimport' in importer.data_package['Description']
    importer.import_zarr.assert_called_once()
    if failure == 'incomplete':
        post.assert_not_called()
    else:
        assert post.call_count == module.ANNOTATION_MAX_ATTEMPTS


def test_every_returned_image_requires_provenance(
    importer, connections, monkeypatch,
):
    original, _, _, _ = connections
    importer.import_zarr = MagicMock(return_value=([31, 32], False))
    post = MagicMock(return_value=80)
    monkeypatch.setattr(module.ezomero, 'post_map_annotation', post)
    successful, failed = upload(importer, original)
    assert successful and not failed
    assert [call.kwargs['object_id'] for call in post.call_args_list] == [31, 32]
    assert all(call.kwargs['object_type'] == 'Image'
               for call in post.call_args_list)


def test_ingestion_never_reports_completed_after_provenance_failure(
    importer, connections, monkeypatch,
):
    original, _, _, _ = connections
    importer.import_zarr = MagicMock(return_value=([991], True))
    monkeypatch.setattr(module.ezomero, 'post_map_annotation', lambda **_: None)
    importer.import_data_package = lambda: (*upload(importer, original), False)
    monkeypatch.setattr(main, 'DataPackageImporter', lambda *args: importer)
    log = MagicMock()
    monkeypatch.setattr(main, 'log_ingestion_step', log)

    process = main.IngestionProcess(importer.data_package, {}, None)
    process.import_data_package()

    log.assert_called_once()
    package, stage = log.call_args.args
    assert stage == STAGE_INGEST_FAILED
    assert 'registered Plate IDs: [991]' in package['Description']


def test_registration_and_postprocessing_keep_both_sessions_alive(
    importer, connections, monkeypatch,
):
    _, fresh, root, _ = connections
    monkeypatch.setattr(module, 'OMERO_KEEPALIVE_SECONDS', 0.01)
    monkeypatch.setattr(module, 'DataProcessor', MagicMock())
    module.DataProcessor.return_value.has_preprocessing.return_value = False
    monkeypatch.setattr(module, 'get_tmp_output_path', lambda _: '/nonexistent')
    heartbeat = Event()
    def keep_alive():
        heartbeat.set()
        return True

    fresh.keepAlive.side_effect = keep_alive

    def long_registration(*args, **kwargs):
        heartbeat.clear()
        assert heartbeat.wait(2)
        return [991], True

    def long_annotation(**kwargs):
        heartbeat.clear()
        assert heartbeat.wait(2)
        return 80

    importer.import_zarr = MagicMock(side_effect=long_registration)
    monkeypatch.setattr(module.ezomero, 'post_map_annotation', long_annotation)
    successful, failed, import_failed = importer.import_data_package()

    assert successful and not failed and not import_failed
    assert root.keepAlive.call_count >= 3
    assert fresh.keepAlive.call_count >= 3
    importer.import_zarr.assert_called_once()


def test_failure_event_persists_all_registered_ids(
    importer, connections, monkeypatch, tmp_path,
):
    original, _, _, _ = connections
    importer.import_zarr = MagicMock(return_value=([31, 32], False))
    monkeypatch.setattr(module.ezomero, 'post_map_annotation', lambda **_: None)
    importer.import_data_package = lambda: (*upload(importer, original), False)
    monkeypatch.setattr(main, 'DataPackageImporter', lambda *args: importer)
    monkeypatch.delenv('INGEST_TRACKING_DB_URL', raising=False)
    tracker = ingest_tracker.IngestTracker({
        'ingest_tracking_db': f"sqlite:///{tmp_path / 'events.db'}",
    })
    monkeypatch.setattr(ingest_tracker, '_ingest_tracker', tracker)
    try:
        main.IngestionProcess(importer.data_package, {}, None).import_data_package()
        with tracker.Session() as session:
            event = session.query(ingest_tracker.IngestionTracking).one()
            assert event.stage == STAGE_INGEST_FAILED
            assert 'registered Image IDs: [31, 32]' in event.description
            assert event.uuid == 'original-order'
            assert event.user_name == 'requester'
            assert event.group_name == 'original'
    finally:
        tracker.dispose()


def test_session_close_failure_does_not_restart_registration(
    importer, connections, monkeypatch,
):
    _, _, root, _ = connections
    monkeypatch.setattr(module, 'DataProcessor', MagicMock())
    module.DataProcessor.return_value.has_preprocessing.return_value = False
    monkeypatch.setattr(module, 'get_tmp_output_path', lambda _: '/nonexistent')
    root.suConn.return_value.__exit__.side_effect = ConnectionError('connection lost')
    importer.import_zarr = MagicMock(return_value=([991], True))
    monkeypatch.setattr(module.ezomero, 'post_map_annotation', lambda **_: 80)

    _, _, import_failed = importer.import_data_package()

    assert import_failed
    importer.import_zarr.assert_called_once()
    assert 'Plate IDs: [991]' in importer.data_package['Description']
    assert 'do not reimport' in importer.data_package['Description']


@pytest.mark.parametrize('output', [
    '- Image: [31, 32]\n- Image: [33]\n', '', '- Image: [invalid]\n',
])
def test_legacy_folder_uses_cli_ids_never_dataset_id(
    importer, connections, monkeypatch, tmp_path, output,
):
    original, _, _, _ = connections
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'logs').mkdir()
    folder = tmp_path / 'input'
    folder.mkdir()
    cli_output = tmp_path / 'logs' / 'cli.original-order_0.logs'
    cli_output.write_text('- Image: [999]\n')
    cli = MagicMock()
    cli.rv = 0

    def invoke(arguments):
        assert cli_output.read_text() == '- Image: [999]\n'
        assert arguments[arguments.index('--output') + 1] == 'yaml'
        exact_output = Path(arguments[arguments.index('--file') + 1])
        assert exact_output.read_text() == ''
        assert exact_output.name.startswith('cli.original-order_0.')
        assert exact_output.name.endswith('.ids.yaml')
        exact_output.write_text(output)

    cli.invoke.side_effect = invoke
    monkeypatch.setattr(module, 'CLI', lambda: cli)
    post = MagicMock(return_value=80)
    monkeypatch.setattr(module.ezomero, 'post_map_annotation', post)
    successful, failed = importer.upload_files(
        original, [str(folder)], dataset_id=42,
    )
    cli.invoke.assert_called_once()
    if 'invalid' in output or not output:
        assert not successful and failed
        post.assert_not_called()
        assert 'Dataset 42' in importer.data_package['Description']
    else:
        assert successful and not failed
        assert [call.kwargs['object_id'] for call in post.call_args_list] == [31, 32, 33]


@pytest.mark.parametrize('returned_ids', [[31, 32], True])
def test_post_registration_path_failure_preserves_exact_ids_not_booleans(
    importer, connections, tmp_path, returned_ids,
):
    original, _, _, _ = connections
    local_file = tmp_path / 'converted.tif'
    local_file.touch()
    importer.import_dataset = MagicMock(return_value=returned_ids)
    importer.get_image_paths = MagicMock(side_effect=ConnectionError('lost'))
    successful, failed = importer.upload_files(
        original, ['/data/original.tif'], dataset_id=42,
        local_paths=[str(local_file)],
    )
    assert not successful and failed
    if returned_ids is True:
        assert failed[0][3] is None
        assert 'registered Image IDs' not in importer.data_package['Description']
    else:
        assert failed[0][3] == 32
        assert 'registered Image IDs: [31, 32]' in importer.data_package['Description']
        assert importer._registered_objects
