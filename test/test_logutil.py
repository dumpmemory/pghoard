import logging
from unittest.mock import Mock, patch

from pghoard import logutil


class TestLogUtil:
    def test_configure_logging_systemd_journal(self):
        """Test logging configuration when running under systemd with journal available."""
        with (
            patch("pghoard.logutil.journal") as mock_journal,
            patch("logging.getLogger") as mock_get_logger,
            patch("logging.basicConfig") as mock_basic_config,
            patch.dict("os.environ", {"NOTIFY_SOCKET": "/run/systemd/notify"}),
        ):
            # Setup mocks
            mock_handler = Mock()
            mock_journal.JournalHandler.return_value = mock_handler
            mock_root_logger = Mock()
            mock_get_logger.return_value = mock_root_logger

            # Run
            logutil.configure_logging(level=logging.INFO)

            # Verify
            mock_journal.JournalHandler.assert_called_once()
            mock_handler.setFormatter.assert_called_once()
            mock_get_logger.assert_called()
            mock_root_logger.addHandler.assert_called_once_with(mock_handler)
            mock_root_logger.setLevel.assert_called_once_with(logging.INFO)
            mock_basic_config.assert_not_called()

            # Verify formatter usage
            args, _ = mock_handler.setFormatter.call_args
            assert isinstance(args[0], logging.Formatter)

    def test_configure_logging_systemd_no_journal_module(self):
        """Test logging configuration under systemd but python-systemd (journal) not installed."""
        with (
            patch("pghoard.logutil.journal", None),
            # We need to mock daemon as well to be None, otherwise the print won't happen
            patch("pghoard.logutil.daemon", None),
            patch("logging.getLogger"),
            patch("logging.basicConfig") as mock_basic_config,
            patch("sys.stdout") as mock_stdout,
            patch.dict("os.environ", {"NOTIFY_SOCKET": "/run/systemd/notify"}),
        ):  # print is used for warning

            # Run
            logutil.configure_logging(level=logging.WARNING)

            # Verify
            mock_basic_config.assert_called_once()
            _, kwargs = mock_basic_config.call_args
            assert kwargs["level"] == logging.WARNING
            assert kwargs["format"] == logutil.LOG_FORMAT_SYSLOG

            # Verify the warning was printed
            # print calls write() on stdout. We check if any write call contained the warning message.
            # print() might call write multiple times (e.g. for the message and then for newline)
            assert mock_stdout.write.called
            # Check if any call args tuple contains the warning string
            assert any("WARNING:" in args[0] for args, _ in mock_stdout.write.call_args_list)

    def test_configure_logging_regular_run(self):
        """Test logging configuration when NOT running under systemd."""
        # Ensure NOTIFY_SOCKET is not present
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("logging.getLogger"),
            patch("logging.basicConfig") as mock_basic_config,
        ):
            # Run
            logutil.configure_logging(level=logging.DEBUG)

            # Verify
            mock_basic_config.assert_called_once()
            _, kwargs = mock_basic_config.call_args
            assert kwargs["level"] == logging.DEBUG
            assert kwargs["format"] == logutil.LOG_FORMAT

    def test_configure_logging_short_log(self):
        """Test short log format option."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("logging.basicConfig") as mock_basic_config,
        ):
            # Run
            logutil.configure_logging(level=logging.DEBUG, short_log=True)

            # Verify
            mock_basic_config.assert_called_once()
            _, kwargs = mock_basic_config.call_args
            assert kwargs["format"] == logutil.LOG_FORMAT_SHORT
