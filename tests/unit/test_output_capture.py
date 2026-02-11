import contextlib
import sys

import pytest

from stemtrace.library.output_capture import (
    CapturedOutput,
    OutputCapture,
    get_current_capture,
    start_capture,
    stop_capture,
)
from stemtrace.library.signals import (
    disconnect_signals,
)
from stemtrace.library.transports.memory import MemoryTransport


@pytest.fixture(autouse=True)
def clean_state() -> None:
    MemoryTransport.clear()
    disconnect_signals()
    if get_current_capture() is not None:
        stop_capture()


@contextlib.contextmanager
def capture_output(*args, **kwargs):
    try:
        capture = OutputCapture(*args, **kwargs)
        capture.start()
        yield capture
    finally:
        capture.stop()


class TestOutputCapture:
    def test_captures_stdout(self) -> None:
        with capture_output() as capture:
            print("Hello, stdout!")
            output = capture.get_output()

        assert "Hello, stdout!" in output.stdout
        assert output.stderr == ""

    def test_captures_stderr(self) -> None:
        with capture_output() as capture:
            print("Hello, stderr!", file=sys.stderr)
            output = capture.get_output()

        assert output.stdout == ""
        assert "Hello, stderr!" in output.stderr

    def test_captures_both_stdout_and_stderr(self) -> None:
        with capture_output() as capture:
            print("stdout message")
            print("stderr message", file=sys.stderr)
            output = capture.get_output()

        assert "stdout message" in output.stdout
        assert "stderr message" in output.stderr

    def test_restores_streams_after_exit(self) -> None:
        original_stdout = sys.stdout
        original_stderr = sys.stderr

        with capture_output():
            assert sys.stdout is not original_stdout
            assert sys.stderr is not original_stderr

        assert sys.stdout is original_stdout
        assert sys.stderr is original_stderr

    def test_tee_output_to_original_stream(self, capsys: pytest.CaptureFixture) -> None:
        """Output is still sent to the original stream (tee behavior)."""
        with capture_output() as capture:
            print("visible message")
            print("error message", file=sys.stderr)

        output = capture.get_output()
        assert "visible message" in output.stdout
        assert "error message" in output.stderr

        captured = capsys.readouterr()
        assert "visible message" in captured.out
        assert "error message" in captured.err

    def test_max_output_size_truncates_stdout(self) -> None:
        with capture_output(max_size=50) as capture:
            print("x" * 100)
            output = capture.get_output()

        assert len(output.stdout) < 110  # Original was 100+ chars
        assert "[truncated]" in output.stdout

    def test_max_output_size_truncates_stderr(self) -> None:
        with capture_output(max_size=50) as capture:
            print("y" * 100, file=sys.stderr)
            output = capture.get_output()

        assert len(output.stderr) < 110
        assert "[truncated]" in output.stderr

    def test_empty_capture_returns_empty_strings(self) -> None:
        with capture_output() as capture:
            output = capture.get_output()

        assert output.stdout == ""
        assert output.stderr == ""


class TestStartStopCapture:
    def test_start_capture_sets_active_capture(self) -> None:
        assert get_current_capture() is None

        capture = start_capture()
        assert get_current_capture() is capture

        stop_capture()
        assert get_current_capture() is None

    def test_stop_capture_returns_output(self) -> None:
        start_capture()
        print("captured text")

        output = stop_capture()
        assert isinstance(output, CapturedOutput)
        assert "captured text" in output.stdout

    def test_stop_capture_without_start_returns_empty(self) -> None:
        output = stop_capture()
        assert output.stdout == ""
        assert output.stderr == ""
