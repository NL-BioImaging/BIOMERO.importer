"""Tests for narrowly retrying transient Podman bind-source failures."""

from io import BytesIO
from unittest.mock import MagicMock, call, patch

from biomero_importer.utils.importer import (
    DataProcessor,
    is_retryable_podman_run_error,
)


class FakeProcess:
    def __init__(self, return_code, lines):
        self.return_code = return_code
        self.stdout = BytesIO(
            b"".join(line.encode() + b"\n" for line in lines)
        )

    def wait(self):
        return self.return_code


def test_retryable_podman_error_matches_only_bind_source_ebusy():
    assert is_retryable_podman_run_error([
        "Error: statfs /data/cellular_imaging: device or resource busy"
    ])
    assert not is_retryable_podman_run_error([
        "Error: image not known"
    ])
    assert not is_retryable_podman_run_error([
        "Error: statfs /data/cellular_imaging: permission denied"
    ])


def test_podman_run_retries_busy_bind_then_returns_success(monkeypatch):
    monkeypatch.setenv("PODMAN_BIND_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("PODMAN_BIND_RETRY_DELAY_SECONDS", "2")
    processor = DataProcessor({}, logger=MagicMock())
    processes = [
        FakeProcess(125, [
            "Error: statfs /data/cellular_imaging: device or resource busy"
        ]),
        FakeProcess(0, ['[{"name": "converted.ome.tiff"}]']),
    ]

    with patch(
        "biomero_importer.utils.importer.Popen", side_effect=processes
    ) as popen, patch(
        "biomero_importer.utils.importer.time.sleep"
    ) as sleep:
        return_code, output = processor._run_podman_command(
            ["podman", "run", "--rm", "example/image:latest"]
        )

    assert return_code == 0
    assert output == ['[{"name": "converted.ome.tiff"}]']
    assert popen.call_count == 2
    sleep.assert_called_once_with(2)


def test_podman_run_does_not_retry_unrelated_failure(monkeypatch):
    monkeypatch.setenv("PODMAN_BIND_RETRY_ATTEMPTS", "3")
    processor = DataProcessor({}, logger=MagicMock())

    with patch(
        "biomero_importer.utils.importer.Popen",
        return_value=FakeProcess(125, ["Error: image not known"]),
    ) as popen, patch(
        "biomero_importer.utils.importer.time.sleep"
    ) as sleep:
        return_code, output = processor._run_podman_command(
            ["podman", "run", "--rm", "missing/image:latest"]
        )

    assert return_code == 125
    assert output == ["Error: image not known"]
    popen.assert_called_once()
    sleep.assert_not_called()


def test_podman_run_stops_after_bounded_exponential_backoff(monkeypatch):
    monkeypatch.setenv("PODMAN_BIND_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("PODMAN_BIND_RETRY_DELAY_SECONDS", "2")
    processor = DataProcessor({}, logger=MagicMock())
    busy = "Error: statfs /data/cellular_imaging: device or resource busy"

    with patch(
        "biomero_importer.utils.importer.Popen",
        side_effect=[FakeProcess(125, [busy]) for _ in range(3)],
    ) as popen, patch(
        "biomero_importer.utils.importer.time.sleep"
    ) as sleep:
        return_code, output = processor._run_podman_command(
            ["podman", "run", "--rm", "example/image:latest"]
        )

    assert return_code == 125
    assert output == [busy]
    assert popen.call_count == 3
    assert sleep.call_args_list == [call(2), call(4)]


def test_invalid_retry_settings_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("PODMAN_BIND_RETRY_ATTEMPTS", "zero")
    monkeypatch.setenv("PODMAN_BIND_RETRY_DELAY_SECONDS", "0")
    logger = MagicMock()
    processor = DataProcessor({}, logger=logger)

    with patch(
        "biomero_importer.utils.importer.Popen",
        return_value=FakeProcess(0, ["[]"]),
    ):
        return_code, output = processor._run_podman_command(
            ["podman", "run", "--rm", "example/image:latest"]
        )

    assert return_code == 0
    assert output == ["[]"]
    assert logger.warning.call_count == 2
