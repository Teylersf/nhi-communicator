"""Offline RX child-lifecycle tests. Every subprocess entry point is mocked."""

from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

import server


class RepeatingIQPipe:
    """Supply a finite byte count using small in-memory I/Q chunks."""

    def __init__(self, byte_count):
        self.remaining = byte_count
        self.closed = False

    def read(self, size):
        if self.closed:
            raise ValueError("Mock I/Q pipe is closed")
        count = min(size, self.remaining)
        self.remaining -= count
        return (b"\x01\xfe" * ((count + 1) // 2))[:count]

    def close(self):
        self.closed = True


class DelayedReceiptPipe:
    """Hold the startup receipt until a test explicitly releases it."""

    def __init__(self, receipt):
        self.buffer = BytesIO(receipt)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = False
        self.first_read = True

    def readline(self):
        if self.first_read:
            self.first_read = False
            self.entered.set()
            if not self.release.wait(timeout=2):
                raise RuntimeError("Test did not release the mock startup receipt")
        return self.buffer.readline()

    def close(self):
        self.closed = True
        self.release.set()
        self.buffer.close()


IDLE_WARNING_RECEIPT = (
    b"Stop with Ctrl-C\n"
    b"Total time: 4.014 seconds.\n"
    b"Couldn't transfer any bytes for one second.\n"
    b"hackrf_stop_rx() done\n"
    b"hackrf_close() done\n"
    b"hackrf_exit() done\n"
)


class ReceiveProcessTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = TemporaryDirectory(prefix="nhi-receive-test-")
        self.addCleanup(temporary_directory.cleanup)
        self.runtime_root = Path(temporary_directory.name)
        root_patch = patch.object(server, "RUNTIME", self.runtime_root / "runtime")
        popen_patch = patch.object(
            server.subprocess,
            "Popen",
            side_effect=AssertionError("Unexpected process launch in offline RX tests"),
        )
        run_patch = patch.object(
            server.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, b'{"idle": true}', b""),
        )
        for patcher in (root_patch, popen_patch, run_patch):
            self.addCleanup(patcher.stop)
        root_patch.start()
        self.popen = popen_patch.start()
        self.run = run_patch.start()
        self.hardware = server.Hardware()

    def child(self, raw=b"", exit_code=0, receipt=b"Stop with Ctrl-C\nreceive complete\n"):
        process = Mock(spec=["stdout", "stderr", "poll", "terminate", "kill", "wait"])
        process.stdout = BytesIO(raw)
        process.stderr = BytesIO(receipt)
        process.poll.return_value = exit_code
        process.wait.return_value = exit_code
        self.popen.side_effect = None
        self.popen.return_value = process
        return process

    def finish_readers(self):
        for reader in (self.hardware.reader, self.hardware.stderr_reader):
            if reader:
                reader.join(timeout=2)
                self.assertFalse(reader.is_alive(), "Offline pipe reader did not finish")

    def stream_has_started(self):
        flag = self.hardware.stream_started
        return flag.is_set() if hasattr(flag, "is_set") else bool(flag)

    def finite_warning_child(self, byte_count=64_000_000, duration=4,
                             receipt=IDLE_WARNING_RECEIPT, exit_code=1):
        process = self.child(exit_code=exit_code, receipt=receipt)
        process.stdout = RepeatingIQPipe(byte_count)
        delivered = [0]

        def count_bytes(chunk):
            delivered[0] += len(chunk)
            return 0

        self.hardware.receive(1_600_000_000, count_bytes, threading.Event(), duration=duration)
        self.finish_readers()
        self.assertEqual(delivered[0], byte_count)
        self.assertEqual(self.hardware.bytes_received, byte_count)
        return process

    def test_finite_receive_command_has_only_receive_and_low_power_options(self):
        self.child()
        self.hardware.receive(1_600_000_000, lambda raw: 0, threading.Event(), duration=4)
        self.finish_readers()
        self.popen.assert_called_once()
        command = self.popen.call_args.args[0]
        expected_options = {
            "-d": server.SERIAL,
            "-r": "-",
            "-f": "1600000000",
            "-s": "8000000",
            "-a": "0",
            "-p": "0",
            "-l": "16",
            "-g": "16",
            "-n": "32000000",
        }
        self.assertEqual(command[0], str(server.BIN / "hackrf_transfer.exe"))
        self.assertEqual(len(command), 1 + 2 * len(expected_options))
        self.assertEqual(dict(zip(command[1::2], command[2::2])), expected_options)
        self.assertNotIn("-t", command)
        self.assertNotIn("-R", command)
        self.assertNotIn("-c", command)
        self.assertNotIn("shell", self.popen.call_args.kwargs)
        self.assertEqual(self.popen.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(self.popen.call_args.kwargs["stderr"], subprocess.PIPE)
        self.hardware.close()

    def test_already_stopped_does_not_launch_receive_child(self):
        stop_event = threading.Event()
        stop_event.set()
        callback = Mock(return_value=0)
        self.hardware.receive(1_600_000_000, callback, stop_event, duration=4)
        self.popen.assert_not_called()
        self.run.assert_not_called()
        callback.assert_not_called()
        self.assertIsNone(self.hardware.process)

    def test_binary_iq_is_preserved_across_chunks_and_counted_exactly(self):
        raw = bytes(range(256)) * 1024 + bytes(range(16))
        process = self.child(raw)
        chunks = []

        def collect(chunk):
            chunks.append(chunk)
            return 0

        self.hardware.receive(100_000_000, collect, threading.Event(), duration=2)
        self.finish_readers()
        self.assertEqual([len(chunk) for chunk in chunks], [262144, 16])
        self.assertEqual(b"".join(chunks), raw)
        self.assertEqual(self.hardware.bytes_received, len(raw))
        self.assertEqual(self.hardware.poll(), 0)
        self.hardware.close()
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_unexpected_nonzero_receive_exit_raises_with_receipt(self):
        process = self.child(exit_code=-1073740791, receipt=b"mock native failure\n")
        self.hardware.receive(100_000_000, lambda raw: 0, threading.Event(), duration=2)
        self.finish_readers()
        with self.assertRaisesRegex(RuntimeError, "Receive child exited -1073740791.*mock native failure"):
            self.hardware.poll()
        self.hardware.close()
        process.wait.assert_called_once_with(timeout=3)

    def test_receive_child_is_reaped_before_idle_helper(self):
        process = self.child()
        events = []
        process.wait.side_effect = lambda **kwargs: events.append("reaped") or 0

        def idle_helper(command, **kwargs):
            events.append("mode_off")
            self.assertIn("--idle", command)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            return subprocess.CompletedProcess(command, 0, b'{"idle": true}', b"")

        self.run.side_effect = idle_helper
        self.hardware.receive(100_000_000, lambda raw: 0, threading.Event(), duration=2)
        self.finish_readers()
        self.hardware.close()
        self.assertEqual(events, ["reaped", "mode_off"])
        self.assertIsNone(self.hardware.process)

    def test_idle_helper_native_failure_propagates_after_receive_child_reaped(self):
        process = self.child()
        self.run.return_value = subprocess.CompletedProcess(
            [], -1073740791, b"", b"mock helper native failure"
        )
        self.hardware.receive(100_000_000, lambda raw: 0, threading.Event(), duration=2)
        self.finish_readers()
        with self.assertRaisesRegex(RuntimeError, "Radio control child exited -1073740791.*mock helper native failure"):
            self.hardware.close()
        process.wait.assert_called_once_with(timeout=3)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_cancel_terminates_running_child_then_close_reaps_it(self):
        process = self.child(exit_code=None)
        process.terminate.side_effect = lambda: setattr(process.poll, "return_value", -15)
        process.wait.return_value = -15
        self.hardware.receive(100_000_000, lambda raw: 0, threading.Event(), duration=2)
        self.finish_readers()
        self.hardware.cancel()
        process.terminate.assert_called_once_with()
        self.run.assert_not_called()
        self.hardware.close()
        process.wait.assert_called_once_with(timeout=3)
        self.run.assert_called_once()

    def test_unreaped_child_blocks_idle_helper_and_retains_ownership(self):
        process = self.child(exit_code=None)
        process.wait.side_effect = subprocess.TimeoutExpired("mock-receive-child", 3)
        self.hardware.receive(100_000_000, lambda raw: 0, threading.Event(), duration=2)
        self.finish_readers()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.hardware.close()
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        self.run.assert_not_called()
        self.assertIs(self.hardware.process, process)
        # Reap only the mock after checking the ownership/idle safety invariant.
        process.poll.return_value = 0
        process.wait.side_effect = None
        process.wait.return_value = 0
        self.hardware.close()

    def test_exact_finite_capture_with_idle_warning_normalizes_exit_one_only(self):
        self.finite_warning_child()
        self.assertEqual(self.hardware.expected_bytes, 64_000_000)
        self.assertEqual(self.hardware.poll(), 0)
        self.assertTrue(self.hardware.completion_warning)
        self.assertEqual(self.hardware.poll(), 0)
        self.hardware.close()

    def test_idle_warning_normalization_waits_for_both_pipe_readers(self):
        self.finite_warning_child()
        for reader_name in ("reader", "stderr_reader"):
            with self.subTest(active_reader=reader_name):
                with patch.object(getattr(self.hardware, reader_name), "is_alive", return_value=True):
                    self.assertIsNone(self.hardware.poll())
                    self.assertFalse(self.hardware.completion_warning)
        self.assertEqual(self.hardware.poll(), 0)
        self.assertTrue(self.hardware.completion_warning)
        self.hardware.close()

    def test_idle_warning_with_partial_finite_capture_raises_typed_stall(self):
        for byte_count in (28_136_448, 63_999_998, 0):
            with self.subTest(byte_count=byte_count):
                self.finite_warning_child(byte_count=byte_count)
                self.assertEqual(self.hardware.expected_bytes, 64_000_000)
                with self.assertRaises(server.ReceiveStallError) as error:
                    self.hardware.poll()
                self.assertEqual(error.exception.received_bytes, byte_count)
                self.assertEqual(error.exception.expected_bytes, 64_000_000)
                self.assertEqual(error.exception.receipt, IDLE_WARNING_RECEIPT.decode().strip())
                self.assertIn("32,000,000 requested samples", str(error.exception))
                self.assertFalse(self.hardware.completion_warning)
                self.hardware.close()

    def test_idle_warning_with_overlong_finite_capture_keeps_general_error(self):
        self.finite_warning_child(byte_count=64_000_002)
        with self.assertRaisesRegex(RuntimeError, "Receive child exited 1") as error:
            self.hardware.poll()
        self.assertNotIsInstance(error.exception, server.ReceiveStallError)
        self.assertFalse(self.hardware.completion_warning)
        self.hardware.close()

    def test_partial_stall_classification_waits_for_both_pipe_readers(self):
        self.finite_warning_child(byte_count=28_136_448)
        for reader_name in ("reader", "stderr_reader"):
            with self.subTest(active_reader=reader_name):
                with patch.object(getattr(self.hardware, reader_name), "is_alive", return_value=True):
                    self.assertIsNone(self.hardware.poll())
        with self.assertRaises(server.ReceiveStallError):
            self.hardware.poll()
        self.hardware.close()

    def test_partial_stall_requires_exact_idle_diagnostic_and_shutdown_receipts(self):
        required_lines = (
            b"Couldn't transfer any bytes for one second.\n",
            b"hackrf_stop_rx() done\n",
            b"hackrf_close() done\n",
            b"hackrf_exit() done\n",
        )
        for missing in required_lines:
            with self.subTest(missing=missing.decode().strip()):
                self.finite_warning_child(
                    byte_count=28_136_448,
                    receipt=IDLE_WARNING_RECEIPT.replace(missing, b""),
                )
                with self.assertRaisesRegex(RuntimeError, "Receive child exited 1") as error:
                    self.hardware.poll()
                self.assertNotIsInstance(error.exception, server.ReceiveStallError)
                self.assertFalse(self.hardware.completion_warning)
                self.hardware.close()

    def test_partial_unknown_warning_and_failed_diagnostic_keep_general_error(self):
        receipts = (
            IDLE_WARNING_RECEIPT.replace(
                b"Couldn't transfer any bytes for one second.",
                b"Receive transfer timed out for an unknown reason.",
            ),
            IDLE_WARNING_RECEIPT + b"USB operation FAILED\n",
        )
        for receipt in receipts:
            with self.subTest(receipt=receipt):
                self.finite_warning_child(byte_count=28_136_448, receipt=receipt)
                with self.assertRaisesRegex(RuntimeError, "Receive child exited 1") as error:
                    self.hardware.poll()
                self.assertNotIsInstance(error.exception, server.ReceiveStallError)
                self.hardware.close()

    def test_partial_capture_with_other_or_native_exit_keeps_general_error(self):
        for exit_code in (2, -1073740791):
            with self.subTest(exit_code=exit_code):
                self.finite_warning_child(byte_count=28_136_448, exit_code=exit_code)
                with self.assertRaisesRegex(RuntimeError, f"Receive child exited {exit_code}") as error:
                    self.hardware.poll()
                self.assertNotIsInstance(error.exception, server.ReceiveStallError)
                self.hardware.close()

    def test_idle_warning_without_startup_receipt_keeps_general_error(self):
        self.child(
            raw=b"Usage:\n",
            exit_code=1,
            receipt=IDLE_WARNING_RECEIPT.replace(b"Stop with Ctrl-C\n", b""),
        )
        callback = Mock(return_value=0)
        self.hardware.receive(1_600_000_000, callback, threading.Event(), duration=4)
        self.finish_readers()
        callback.assert_not_called()
        self.assertEqual(self.hardware.bytes_received, 0)
        self.assertFalse(self.stream_has_started())
        with self.assertRaisesRegex(RuntimeError, "Receive child exited 1") as error:
            self.hardware.poll()
        self.assertNotIsInstance(error.exception, server.ReceiveStallError)
        self.hardware.close()

    def test_iq_reader_error_takes_priority_over_clean_idle_stall_receipt(self):
        self.child(raw=b"\x01", exit_code=1, receipt=IDLE_WARNING_RECEIPT)
        callback = Mock(return_value=0)
        self.hardware.receive(1_600_000_000, callback, threading.Event(), duration=4)
        self.finish_readers()
        callback.assert_not_called()
        self.assertEqual(self.hardware.bytes_received, 0)
        self.assertTrue(self.stream_has_started())
        with self.assertRaisesRegex(RuntimeError, "Receive processing:.*partial I/Q sample") as error:
            self.hardware.poll()
        self.assertNotIsInstance(error.exception, server.ReceiveStallError)
        self.hardware.close()

    def test_idle_warning_from_unlimited_receive_still_raises(self):
        self.finite_warning_child(duration=None)
        self.assertIsNone(self.hardware.expected_bytes)
        with self.assertRaisesRegex(RuntimeError, "Receive child exited 1") as error:
            self.hardware.poll()
        self.assertNotIsInstance(error.exception, server.ReceiveStallError)
        self.assertFalse(self.hardware.completion_warning)
        self.hardware.close()

    def test_idle_warning_requires_diagnostic_and_each_shutdown_receipt(self):
        required_lines = (
            b"Couldn't transfer any bytes for one second.\n",
            b"hackrf_stop_rx() done\n",
            b"hackrf_close() done\n",
            b"hackrf_exit() done\n",
        )
        for missing in required_lines:
            with self.subTest(missing=missing.decode().strip()):
                receipt = IDLE_WARNING_RECEIPT.replace(missing, b"")
                self.finite_warning_child(receipt=receipt)
                with self.assertRaisesRegex(RuntimeError, "Receive child exited 1"):
                    self.hardware.poll()
                self.assertFalse(self.hardware.completion_warning)
                self.hardware.close()

    def test_other_receive_failure_is_not_masked_by_complete_idle_receipt(self):
        self.finite_warning_child(receipt=IDLE_WARNING_RECEIPT + b"USB operation failed\n")
        with self.assertRaisesRegex(RuntimeError, "Receive child exited 1"):
            self.hardware.poll()
        self.assertFalse(self.hardware.completion_warning)
        self.hardware.close()

    def test_other_and_native_exit_codes_are_not_normalized_by_full_capture(self):
        for exit_code in (2, -1073740791):
            with self.subTest(exit_code=exit_code):
                self.finite_warning_child(exit_code=exit_code)
                with self.assertRaisesRegex(RuntimeError, f"Receive child exited {exit_code}"):
                    self.hardware.poll()
                self.assertFalse(self.hardware.completion_warning)
                self.hardware.close()

    def test_failed_open_help_stdout_never_reaches_iq_callback_or_counter(self):
        usage = b"Usage:\n" + b" " * 1683
        self.assertEqual(len(usage), 1690)
        self.child(
            raw=usage, exit_code=1,
            receipt=b"hackrf_open() failed: Access denied (insufficient permissions) (-1000)\n",
        )
        callback = Mock(return_value=0)
        self.hardware.receive(1_600_000_000, callback, threading.Event(), duration=4)
        self.finish_readers()
        callback.assert_not_called()
        self.assertEqual(self.hardware.bytes_received, 0)
        self.assertFalse(self.stream_has_started())
        self.assertTrue(self.hardware.startup_resolved.is_set())
        self.assertIsNone(self.hardware.reader_error)
        with self.assertRaisesRegex(server.ReceiveOpenError, "-1000"):
            self.hardware.poll()
        self.hardware.close()

    def test_delayed_startup_receipt_releases_unchanged_binary_iq(self):
        raw = b"\x00\xff\x80\x7f\x03\xfd\x40\xc0"
        process = self.child(raw=raw)
        receipt = DelayedReceiptPipe(b"Stop with Ctrl-C\nreceive complete\n")
        process.stderr = receipt
        callback = Mock(return_value=0)
        self.hardware.receive(100_000_000, callback, threading.Event(), duration=2)
        try:
            self.assertTrue(receipt.entered.wait(timeout=1))
            callback.assert_not_called()
            self.assertEqual(self.hardware.bytes_received, 0)
            self.assertFalse(self.stream_has_started())
        finally:
            receipt.release.set()
        self.finish_readers()
        callback.assert_called_once_with(raw)
        self.assertEqual(self.hardware.bytes_received, len(raw))
        self.assertTrue(self.stream_has_started())
        self.assertEqual(self.hardware.poll(), 0)
        self.hardware.close()

    def test_non_open_startup_failure_discards_stdout_and_keeps_general_error(self):
        self.child(
            raw=b"Usage:\ninvalid parameter\n", exit_code=1,
            receipt=b"hackrf_set_freq() failed: HACKRF_ERROR_INVALID_PARAM (-2)\n",
        )
        callback = Mock(return_value=0)
        self.hardware.receive(100_000_000, callback, threading.Event(), duration=2)
        self.finish_readers()
        callback.assert_not_called()
        self.assertEqual(self.hardware.bytes_received, 0)
        with self.assertRaises(RuntimeError) as error:
            self.hardware.poll()
        self.assertNotIsInstance(error.exception, server.ReceiveOpenError)
        self.assertIn("hackrf_set_freq() failed", str(error.exception))
        self.hardware.close()

    def test_native_startup_crash_is_not_classified_as_recoverable_open_error(self):
        self.child(
            raw=b"Usage:\n", exit_code=-1073740791,
            receipt=b"hackrf_open() failed: Access denied (insufficient permissions) (-1000)\n",
        )
        callback = Mock(return_value=0)
        self.hardware.receive(100_000_000, callback, threading.Event(), duration=2)
        self.finish_readers()
        callback.assert_not_called()
        self.assertEqual(self.hardware.bytes_received, 0)
        with self.assertRaises(RuntimeError) as error:
            self.hardware.poll()
        self.assertNotIsInstance(error.exception, server.ReceiveOpenError)
        self.assertIn("-1073740791", str(error.exception))
        self.hardware.close()

    def test_access_failure_after_stream_started_keeps_general_error(self):
        self.child(
            raw=b"\x01\xff", exit_code=1,
            receipt=b"Stop with Ctrl-C\nhackrf_open() failed: Access denied (insufficient permissions) (-1000)\n",
        )
        callback = Mock(return_value=0)
        self.hardware.receive(100_000_000, callback, threading.Event(), duration=2)
        self.finish_readers()
        callback.assert_called_once_with(b"\x01\xff")
        self.assertTrue(self.stream_has_started())
        with self.assertRaises(RuntimeError) as error:
            self.hardware.poll()
        self.assertNotIsInstance(error.exception, server.ReceiveOpenError)
        self.hardware.close()

    def test_startup_gate_resets_between_successful_and_failed_receive(self):
        self.child(raw=b"\x01\xff")
        self.hardware.receive(100_000_000, lambda raw: 0, threading.Event(), duration=2)
        self.finish_readers()
        self.assertTrue(self.stream_has_started())
        self.hardware.close()

        self.child(
            raw=b"Usage:\n" + b" " * 1683, exit_code=1,
            receipt=b"hackrf_open() failed: Access denied (insufficient permissions) (-1000)\n",
        )
        callback = Mock(return_value=0)
        self.hardware.receive(100_000_000, callback, threading.Event(), duration=2)
        self.finish_readers()
        callback.assert_not_called()
        self.assertEqual(self.hardware.bytes_received, 0)
        self.assertFalse(self.stream_has_started())
        with self.assertRaises(server.ReceiveOpenError):
            self.hardware.poll()
        self.hardware.close()

    def test_generic_libusb_startup_failure_is_not_recoverable_open_error(self):
        self.child(
            raw=b"Usage:\n", exit_code=1,
            receipt=b"hackrf_open() failed: HACKRF_ERROR_LIBUSB (-1000)\n",
        )
        callback = Mock(return_value=0)
        self.hardware.receive(100_000_000, callback, threading.Event(), duration=2)
        self.finish_readers()
        callback.assert_not_called()
        self.assertEqual(self.hardware.bytes_received, 0)
        with self.assertRaises(RuntimeError) as error:
            self.hardware.poll()
        self.assertNotIsInstance(error.exception, server.ReceiveOpenError)
        self.assertIn("HACKRF_ERROR_LIBUSB", str(error.exception))
        self.hardware.close()


if __name__ == "__main__":
    unittest.main()
