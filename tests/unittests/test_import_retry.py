"""
Tests for import retry logic and per-file log ID handling.

These tests verify:
- is_retryable_import_error() correctly detects transient errors in .errs files
- import_dataset() retries on retryable errors with proper backoff
- import_dataset() does NOT retry on non-retryable errors
- Per-file log IDs prevent log collisions across concurrent imports
- Retry attempts get unique log files preserving error evidence
"""
import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, call


# ---------------------------------------------------------------------------
# is_retryable_import_error tests
# ---------------------------------------------------------------------------

class TestIsRetryableImportError:
    """Tests for the is_retryable_import_error helper function."""

    def test_ice_object_not_exist_exception(self, tmp_path):
        """The primary race condition error should be retryable."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text(
            "WARN [ome.services.blitz.Entry] (0-omero-4064.Executor-2-thread-3) "
            "2024-06-15 10:32:45,123: Ice.ObjectNotExistException during "
            "IQueryEnumProvider.getEnumerations when trying to instantiate "
            "omero.model.Detector\n"
        )

        retryable, pattern = is_retryable_import_error(str(errs_file))
        assert retryable is True
        assert pattern == "Ice.ObjectNotExistException"

    def test_internal_exception(self, tmp_path):
        """INTERNAL_EXCEPTION should be retryable."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text(
            "ERROR: INTERNAL_EXCEPTION: some transient server problem\n"
        )

        retryable, pattern = is_retryable_import_error(str(errs_file))
        assert retryable is True
        assert pattern == "INTERNAL_EXCEPTION"

    def test_ice_connection_lost(self, tmp_path):
        """Ice.ConnectionLostException should be retryable."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text("Ice.ConnectionLostException: connection lost\n")

        retryable, pattern = is_retryable_import_error(str(errs_file))
        assert retryable is True
        assert pattern == "Ice.ConnectionLostException"

    def test_ice_connection_refused(self, tmp_path):
        """Ice.ConnectionRefusedException should be retryable."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text("Ice.ConnectionRefusedException\n")

        retryable, pattern = is_retryable_import_error(str(errs_file))
        assert retryable is True
        assert pattern == "Ice.ConnectionRefusedException"

    def test_ice_timeout(self, tmp_path):
        """Ice.TimeoutException should be retryable."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text("Ice.TimeoutException: request timed out\n")

        retryable, pattern = is_retryable_import_error(str(errs_file))
        assert retryable is True
        assert pattern == "Ice.TimeoutException"

    def test_non_retryable_error(self, tmp_path):
        """A generic error that doesn't match known patterns should NOT be retryable."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text(
            "ERROR: File format not recognized. "
            "Cannot determine reader for /data/broken.xyz\n"
        )

        retryable, pattern = is_retryable_import_error(str(errs_file))
        assert retryable is False
        assert pattern is None

    def test_empty_errs_file(self, tmp_path):
        """An empty .errs file should NOT be retryable."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text("")

        retryable, pattern = is_retryable_import_error(str(errs_file))
        assert retryable is False
        assert pattern is None

    def test_missing_errs_file(self, tmp_path):
        """A non-existent .errs file should be retryable (import crashed before writing output)."""
        from biomero_importer.utils.importer import is_retryable_import_error

        retryable, pattern = is_retryable_import_error(
            str(tmp_path / "nonexistent.errs")
        )
        assert retryable is True
        assert pattern == "no_errs_file"

    def test_missing_errs_file_logs_warning(self, tmp_path):
        """Logger should be warned when .errs file is missing."""
        from biomero_importer.utils.importer import is_retryable_import_error

        mock_logger = MagicMock()
        retryable, pattern = is_retryable_import_error(
            str(tmp_path / "nonexistent.errs"), mock_logger
        )
        assert retryable is True
        assert pattern == "no_errs_file"
        mock_logger.warning.assert_called_once()
        assert "No .errs file found" in mock_logger.warning.call_args[0][0]

    def test_errs_file_with_multiple_errors(self, tmp_path):
        """Should detect the first matching pattern in multi-error files."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text(
            "WARN: something happened\n"
            "ERROR: INTERNAL_EXCEPTION: blah\n"
            "ERROR: Ice.ObjectNotExistException: also this\n"
        )

        retryable, pattern = is_retryable_import_error(str(errs_file))
        assert retryable is True
        # Should find the first match in RETRYABLE_IMPORT_ERRORS order
        assert pattern in ("INTERNAL_EXCEPTION", "Ice.ObjectNotExistException")

    def test_logger_called_on_retryable(self, tmp_path):
        """Logger should be warned when a retryable error is detected."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text("Ice.ObjectNotExistException\n")

        mock_logger = MagicMock()
        retryable, _ = is_retryable_import_error(str(errs_file), mock_logger)
        assert retryable is True
        mock_logger.warning.assert_called_once()
        assert "Retryable error" in mock_logger.warning.call_args[0][0]

    def test_logger_not_called_on_non_retryable(self, tmp_path):
        """Logger should NOT be called for non-retryable errors."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text("some normal error\n")

        mock_logger = MagicMock()
        retryable, _ = is_retryable_import_error(str(errs_file), mock_logger)
        assert retryable is False
        mock_logger.warning.assert_not_called()

    def test_unreadable_file(self, tmp_path):
        """Should handle unreadable files gracefully."""
        from biomero_importer.utils.importer import is_retryable_import_error

        errs_file = tmp_path / "cli.test.errs"
        errs_file.write_text("Ice.ObjectNotExistException\n")

        mock_logger = MagicMock()
        with patch("builtins.open", side_effect=PermissionError("denied")):
            retryable, pattern = is_retryable_import_error(
                str(errs_file), mock_logger
            )
        assert retryable is False
        assert pattern is None
        mock_logger.warning.assert_called_once()
        assert "Could not read" in mock_logger.warning.call_args[0][0]


# ---------------------------------------------------------------------------
# import_dataset retry tests
# ---------------------------------------------------------------------------

class TestImportDatasetRetry:
    """Tests for retry logic in DataPackageImporter.import_dataset()."""

    @pytest.fixture
    def importer(self, tmp_path):
        """Create a DataPackageImporter with mocked OMERO env vars."""
        with patch.dict(os.environ, {
            'OMERO_HOST': 'localhost',
            'OMERO_PASSWORD': 'test',
            'OMERO_USER': 'root',
            'OMERO_PORT': '4064',
        }):
            from biomero_importer.utils.importer import DataPackageImporter
            config = {}
            data_package = {
                'UUID': 'test-uuid-1234',
                'Username': 'testuser',
                'Group': 'testgroup',
            }
            imp = DataPackageImporter(config, data_package)
            # Override logs dir to tmp_path so errs files are created there
            return imp

    @patch('biomero_importer.utils.importer.time.sleep')
    @patch('biomero_importer.utils.importer.ezomero')
    def test_successful_import_no_retry(self, mock_ezomero, mock_sleep, importer):
        """Successful import on first try should not retry."""
        mock_conn = MagicMock()
        mock_ezomero.ezimport.return_value = [100, 101]

        result = importer.import_dataset(mock_conn, "/path/file.lof", 42)

        assert result == [100, 101]
        assert importer.imported is True
        mock_ezomero.ezimport.assert_called_once()
        mock_sleep.assert_not_called()

    @patch('biomero_importer.utils.importer.time.sleep')
    @patch('biomero_importer.utils.importer.is_retryable_import_error')
    @patch('biomero_importer.utils.importer.ezomero')
    def test_retry_on_retryable_error(
        self, mock_ezomero, mock_is_retryable, mock_sleep, importer
    ):
        """Should retry when ezimport fails and .errs has a retryable error."""
        # First call fails, second succeeds
        mock_ezomero.ezimport.side_effect = [None, [200]]
        mock_is_retryable.return_value = (True, "Ice.ObjectNotExistException")

        result = importer.import_dataset(MagicMock(), "/path/file.lof", 42)

        assert result == [200]
        assert importer.imported is True
        assert mock_ezomero.ezimport.call_count == 2
        # Should have slept with increasing backoff (IMPORT_RETRY_DELAY * attempt)
        mock_sleep.assert_called_once()

    @patch('biomero_importer.utils.importer.time.sleep')
    @patch('biomero_importer.utils.importer.is_retryable_import_error')
    @patch('biomero_importer.utils.importer.ezomero')
    def test_no_retry_on_non_retryable_error(
        self, mock_ezomero, mock_is_retryable, mock_sleep, importer
    ):
        """Should NOT retry when the error is non-retryable."""
        mock_ezomero.ezimport.return_value = None
        mock_is_retryable.return_value = (False, None)

        result = importer.import_dataset(MagicMock(), "/path/file.lof", 42)

        assert result is None
        assert importer.imported is False
        mock_ezomero.ezimport.assert_called_once()
        mock_sleep.assert_not_called()

    @patch('biomero_importer.utils.importer.time.sleep')
    @patch('biomero_importer.utils.importer.is_retryable_import_error')
    @patch('biomero_importer.utils.importer.ezomero')
    def test_max_retries_exhausted(
        self, mock_ezomero, mock_is_retryable, mock_sleep, importer
    ):
        """Should give up after IMPORT_MAX_RETRIES attempts."""
        from biomero_importer.utils.importer import IMPORT_MAX_RETRIES

        mock_ezomero.ezimport.return_value = None
        mock_is_retryable.return_value = (True, "Ice.ObjectNotExistException")

        result = importer.import_dataset(MagicMock(), "/path/file.lof", 42)

        assert result is None
        assert importer.imported is False
        assert mock_ezomero.ezimport.call_count == IMPORT_MAX_RETRIES

    @patch('biomero_importer.utils.importer.time.sleep')
    @patch('biomero_importer.utils.importer.is_retryable_import_error')
    @patch('biomero_importer.utils.importer.ezomero')
    def test_increasing_backoff(
        self, mock_ezomero, mock_is_retryable, mock_sleep, importer
    ):
        """Retry delays should increase with each attempt."""
        from biomero_importer.utils.importer import (
            IMPORT_MAX_RETRIES, IMPORT_RETRY_DELAY,
        )

        mock_ezomero.ezimport.return_value = None
        mock_is_retryable.return_value = (True, "INTERNAL_EXCEPTION")

        importer.import_dataset(MagicMock(), "/path/file.lof", 42)

        # Should sleep (MAX_RETRIES - 1) times (not on the last attempt)
        expected_sleeps = [
            call(IMPORT_RETRY_DELAY * attempt)
            for attempt in range(1, IMPORT_MAX_RETRIES)
        ]
        assert mock_sleep.call_args_list == expected_sleeps

    @patch('biomero_importer.utils.importer.time.sleep')
    @patch('biomero_importer.utils.importer.is_retryable_import_error')
    @patch('biomero_importer.utils.importer.ezomero')
    def test_retry_succeeds_on_third_attempt(
        self, mock_ezomero, mock_is_retryable, mock_sleep, importer
    ):
        """Should succeed when the third attempt works."""
        mock_ezomero.ezimport.side_effect = [None, None, [300, 301]]
        mock_is_retryable.return_value = (True, "Ice.ObjectNotExistException")

        result = importer.import_dataset(MagicMock(), "/path/file.lof", 42)

        assert result == [300, 301]
        assert importer.imported is True
        assert mock_ezomero.ezimport.call_count == 3


# ---------------------------------------------------------------------------
# Per-file log ID tests
# ---------------------------------------------------------------------------

class TestPerFileLogIds:
    """Tests for per-file log ID generation to avoid log collisions."""

    @pytest.fixture
    def importer(self):
        """Create a DataPackageImporter with mocked OMERO env vars."""
        with patch.dict(os.environ, {
            'OMERO_HOST': 'localhost',
            'OMERO_PASSWORD': 'test',
            'OMERO_USER': 'root',
            'OMERO_PORT': '4064',
        }):
            from biomero_importer.utils.importer import DataPackageImporter
            config = {}
            data_package = {
                'UUID': 'abc-123',
                'Username': 'testuser',
                'Group': 'testgroup',
            }
            return DataPackageImporter(config, data_package)

    @patch('biomero_importer.utils.importer.ezomero')
    def test_file_index_in_log_path(self, mock_ezomero, importer):
        """Log files should include file_index to avoid collisions."""
        mock_conn = MagicMock()
        mock_ezomero.ezimport.return_value = [1]

        importer.import_dataset(mock_conn, "/path/file.lof", 42, file_index=3)

        call_kwargs = mock_ezomero.ezimport.call_args[1]
        assert call_kwargs['file'] == "logs/cli.abc-123_3.logs"
        assert call_kwargs['errs'] == "logs/cli.abc-123_3.errs"

    @patch('biomero_importer.utils.importer.ezomero')
    def test_no_file_index_uses_uuid_only(self, mock_ezomero, importer):
        """Without file_index, log files should use UUID only (backward compat)."""
        mock_conn = MagicMock()
        mock_ezomero.ezimport.return_value = [1]

        importer.import_dataset(mock_conn, "/path/file.lof", 42)

        call_kwargs = mock_ezomero.ezimport.call_args[1]
        assert call_kwargs['file'] == "logs/cli.abc-123.logs"
        assert call_kwargs['errs'] == "logs/cli.abc-123.errs"

    @patch('biomero_importer.utils.importer.is_retryable_import_error')
    @patch('biomero_importer.utils.importer.time.sleep')
    @patch('biomero_importer.utils.importer.ezomero')
    def test_retry_attempt_gets_unique_log_file(
        self, mock_ezomero, mock_sleep, mock_is_retryable, importer
    ):
        """Each retry attempt should get a unique log file name."""
        mock_ezomero.ezimport.side_effect = [None, [1]]
        mock_is_retryable.return_value = (True, "INTERNAL_EXCEPTION")

        importer.import_dataset(
            MagicMock(), "/path/file.lof", 42, file_index=0
        )

        calls = mock_ezomero.ezimport.call_args_list
        assert len(calls) == 2

        # First attempt: normal log ID
        first_kwargs = calls[0][1]
        assert first_kwargs['errs'] == "logs/cli.abc-123_0.errs"
        assert first_kwargs['file'] == "logs/cli.abc-123_0.logs"

        # Second attempt: includes _attempt2
        second_kwargs = calls[1][1]
        assert second_kwargs['errs'] == "logs/cli.abc-123_0_attempt2.errs"
        assert second_kwargs['file'] == "logs/cli.abc-123_0_attempt2.logs"

    @patch('biomero_importer.utils.importer.is_retryable_import_error')
    @patch('biomero_importer.utils.importer.time.sleep')
    @patch('biomero_importer.utils.importer.ezomero')
    def test_all_retries_get_distinct_log_files(
        self, mock_ezomero, mock_sleep, mock_is_retryable, importer
    ):
        """Every retry should produce a distinct log filename."""
        from biomero_importer.utils.importer import IMPORT_MAX_RETRIES

        mock_ezomero.ezimport.return_value = None
        mock_is_retryable.return_value = (True, "INTERNAL_EXCEPTION")

        importer.import_dataset(
            MagicMock(), "/path/file.lof", 42, file_index=5
        )

        calls = mock_ezomero.ezimport.call_args_list
        assert len(calls) == IMPORT_MAX_RETRIES

        errs_files = [c[1]['errs'] for c in calls]
        # All log file names should be unique
        assert len(set(errs_files)) == IMPORT_MAX_RETRIES
        # First attempt has no _attempt suffix
        assert errs_files[0] == "logs/cli.abc-123_5.errs"
        # Subsequent attempts have _attempt{N}
        for i in range(1, IMPORT_MAX_RETRIES):
            assert f"_attempt{i + 1}" in errs_files[i]


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------

class TestRetryConstants:
    """Tests for retry-related constants."""

    def test_retryable_errors_list_not_empty(self):
        """The retryable errors list should contain known patterns."""
        from biomero_importer.utils.importer import RETRYABLE_IMPORT_ERRORS

        assert len(RETRYABLE_IMPORT_ERRORS) > 0
        assert "Ice.ObjectNotExistException" in RETRYABLE_IMPORT_ERRORS
        assert "INTERNAL_EXCEPTION" in RETRYABLE_IMPORT_ERRORS

    def test_import_retry_constants_sensible(self):
        """Retry constants should have sensible values."""
        from biomero_importer.utils.importer import (
            IMPORT_MAX_RETRIES, IMPORT_RETRY_DELAY,
        )

        assert IMPORT_MAX_RETRIES >= 1
        assert IMPORT_MAX_RETRIES <= 10  # sanity upper bound
        assert IMPORT_RETRY_DELAY >= 1
        assert IMPORT_RETRY_DELAY <= 60  # sanity upper bound


# ---------------------------------------------------------------------------
# import_to_omero log_id tests
# ---------------------------------------------------------------------------

class TestImportToOmeroLogId:
    """Tests for import_to_omero log_id parameter."""

    @pytest.fixture
    def importer(self):
        with patch.dict(os.environ, {
            'OMERO_HOST': 'localhost',
            'OMERO_PASSWORD': 'test',
            'OMERO_USER': 'root',
            'OMERO_PORT': '4064',
        }):
            from biomero_importer.utils.importer import DataPackageImporter
            config = {'skip_all': True}
            data_package = {
                'UUID': 'xyz-789',
                'Username': 'testuser',
                'Group': 'testgroup',
            }
            return DataPackageImporter(config, data_package)

    @patch('biomero_importer.utils.importer.CLI')
    def test_log_id_overrides_uuid_in_cli_args(self, mock_cli_cls, importer):
        """When log_id is provided, it should be used instead of uuid."""
        mock_cli = MagicMock()
        mock_cli.rv = 0
        mock_cli_cls.return_value = mock_cli

        mock_conn = MagicMock()
        mock_conn.getSession.return_value.getUuid.return_value.val = "session-key"
        mock_conn.host = "localhost"
        mock_conn.port = 4064

        importer.import_to_omero(
            mock_conn,
            file_path="/path/file.lof",
            target_id=42,
            target_type="Dataset",
            uuid="xyz-789",
            log_id="xyz-789_2"
        )

        # Extract the arguments passed to cli.invoke
        invoke_args = mock_cli.invoke.call_args[0][0]
        # Find --file and --errs values
        file_idx = invoke_args.index('--file')
        errs_idx = invoke_args.index('--errs')
        assert invoke_args[file_idx + 1] == "logs/cli.xyz-789_2.logs"
        assert invoke_args[errs_idx + 1] == "logs/cli.xyz-789_2.errs"

    @patch('biomero_importer.utils.importer.CLI')
    def test_no_log_id_falls_back_to_uuid(self, mock_cli_cls, importer):
        """Without log_id, should fall back to the uuid parameter."""
        mock_cli = MagicMock()
        mock_cli.rv = 0
        mock_cli_cls.return_value = mock_cli

        mock_conn = MagicMock()
        mock_conn.getSession.return_value.getUuid.return_value.val = "session-key"
        mock_conn.host = "localhost"
        mock_conn.port = 4064

        importer.import_to_omero(
            mock_conn,
            file_path="/path/file.lof",
            target_id=42,
            target_type="Dataset",
            uuid="xyz-789"
        )

        invoke_args = mock_cli.invoke.call_args[0][0]
        file_idx = invoke_args.index('--file')
        errs_idx = invoke_args.index('--errs')
        assert invoke_args[file_idx + 1] == "logs/cli.xyz-789.logs"
        assert invoke_args[errs_idx + 1] == "logs/cli.xyz-789.errs"
