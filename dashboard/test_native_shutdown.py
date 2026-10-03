"""Offline native RF-mode receipts, independent of capture success."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import threading

import server


def receipt(mode):
    return (f"Stop with Ctrl-C\nExiting...\n"
            f"hackrf_stop_{mode}() done\n"
            "hackrf_close() done\nhackrf_exit() done\nexit\n").encode()


RX_RECEIPT = receipt("rx")
TX_RECEIPT = receipt("tx")
RX_IDLE_RECEIPT = RX_RECEIPT.replace(
    b"Exiting...\n", b"Couldn't transfer any bytes for one second.\nExiting...\n"
)


class NativeShutdownReceiptTests(unittest.TestCase):
    def test_ordered_native_stop_close_exit_receipts_verify_rf_off(self):
        for mode, output in (("rx", RX_RECEIPT), ("tx", TX_RECEIPT)):
            with self.subTest(mode=mode):
                self.assertTrue(server.native_rf_off_receipt(0, output, mode))
                self.assertTrue(server.native_rf_off_receipt(0, output.decode(), mode))

    def test_each_exact_marker_and_final_exit_is_required(self):
        for marker in (b"hackrf_stop_tx() done\n", b"hackrf_close() done\n",
                       b"hackrf_exit() done\n", b"exit\n"):
            with self.subTest(missing=marker):
                self.assertFalse(server.native_rf_off_receipt(0, TX_RECEIPT.replace(marker, b""), "tx"))
            with self.subTest(inexact=marker):
                altered = TX_RECEIPT.replace(marker, marker.rstrip() + b" unexpected\n")
                self.assertFalse(server.native_rf_off_receipt(0, altered, "tx"))

    def test_cleanup_markers_must_be_in_native_control_order(self):
        for output in (
            b"hackrf_close() done\nhackrf_stop_tx() done\nhackrf_exit() done\nexit\n",
            b"hackrf_stop_tx() done\nhackrf_exit() done\nhackrf_close() done\nexit\n",
        ):
            with self.subTest(output=output):
                self.assertFalse(server.native_rf_off_receipt(0, output, "tx"))

    def test_post_exit_output_cannot_claim_clean_final_exit(self):
        self.assertFalse(server.native_rf_off_receipt(0, TX_RECEIPT + b"unexpected output\n", "tx"))

    def test_caught_signal_or_failed_diagnostic_invalidates_receipt(self):
        for diagnostic in (b"Caught signal 2\n", b"caught SIGNAL 15\n", b"USB operation FAILED\n"):
            with self.subTest(diagnostic=diagnostic):
                output = diagnostic + TX_RECEIPT
                self.assertFalse(server.native_rf_off_receipt(0, output, "tx"))

    def test_forced_interruption_is_rejected_even_with_success_exit_receipts(self):
        for mode, output in (("rx", RX_RECEIPT), ("tx", TX_RECEIPT)):
            with self.subTest(mode=mode):
                self.assertFalse(server.native_rf_off_receipt(0, output, mode, interrupted=True))

    def test_native_crashes_and_unknown_exit_codes_are_rejected(self):
        for returncode in (2, -1073740791, 3221226505):
            with self.subTest(returncode=returncode):
                self.assertFalse(server.native_rf_off_receipt(returncode, RX_IDLE_RECEIPT, "rx", allow_idle_warning=True))

    def test_known_rx_idle_exit_requires_explicit_permission_and_shutdown_receipts(self):
        self.assertFalse(server.native_rf_off_receipt(1, RX_IDLE_RECEIPT, "rx"))
        self.assertTrue(server.native_rf_off_receipt(1, RX_IDLE_RECEIPT, "rx", allow_idle_warning=True))
        self.assertFalse(server.native_rf_off_receipt(1, RX_RECEIPT, "rx", allow_idle_warning=True))
        for marker in (b"hackrf_stop_rx() done\n", b"hackrf_close() done\n", b"hackrf_exit() done\n"):
            with self.subTest(missing=marker):
                self.assertFalse(server.native_rf_off_receipt(
                    1, RX_IDLE_RECEIPT.replace(marker, b""), "rx", allow_idle_warning=True
                ))

    def test_tx_exit_one_is_never_accepted_as_rx_idle_warning(self):
        output = TX_RECEIPT.replace(
            b"Exiting...\n", b"Couldn't transfer any bytes for one second.\nExiting...\n"
        )
        self.assertFalse(server.native_rf_off_receipt(1, output, "tx", allow_idle_warning=True))

    def test_wrong_mode_receipt_and_unknown_mode_are_rejected(self):
        self.assertFalse(server.native_rf_off_receipt(0, RX_RECEIPT, "tx"))
        self.assertFalse(server.native_rf_off_receipt(0, TX_RECEIPT, "rx"))
        self.assertFalse(server.native_rf_off_receipt(0, TX_RECEIPT, "unknown"))


class NativeReceiveCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = TemporaryDirectory(prefix="nhi-native-cleanup-test-")
        self.addCleanup(temporary_directory.cleanup)
        self.runtime_root = Path(temporary_directory.name)
        root_patch = patch.object(server, "RUNTIME", self.runtime_root / "runtime")
        popen_patch = patch.object(
            server.subprocess, "Popen",
            side_effect=AssertionError("Unexpected native child in offline shutdown tests"),
        )
        run_patch = patch.object(
            server.subprocess, "run",
            side_effect=AssertionError("Unexpected control helper in offline shutdown tests"),
        )
        for patcher in (root_patch, popen_patch, run_patch):
            self.addCleanup(patcher.stop)
        root_patch.start()
        self.popen = popen_patch.start()
        self.run = run_patch.start()
        self.hardware = server.Hardware()

    def receive_child(self, output=RX_RECEIPT, returncode=0):
        child = Mock(spec=["stdout", "stderr", "poll", "wait", "terminate", "kill"])
        child.stdout = BytesIO(b"\x01\xfe" * 1024)
        child.stderr = BytesIO(output)
        child.poll.return_value = returncode
        child.wait.return_value = returncode
        self.popen.side_effect = None
        self.popen.return_value = child
        self.hardware.receive(1_600_000_000, lambda raw: 0, threading.Event(), duration=4)
        for reader in (self.hardware.reader, self.hardware.stderr_reader):
            reader.join(timeout=2)
            self.assertFalse(reader.is_alive())
        return child

    def test_partial_known_idle_capture_stays_failed_but_verified_off_skips_helper(self):
        child = self.receive_child(output=RX_IDLE_RECEIPT, returncode=1)
        self.assertEqual(self.hardware.bytes_received, 2048)
        self.assertEqual(self.hardware.expected_bytes, 64_000_000)
        with self.assertRaises(server.ReceiveStallError):
            self.hardware.poll()
        self.assertFalse(self.hardware.completion_warning)
        self.hardware.close()
        self.run.assert_not_called()
        child.wait.assert_called_once_with(timeout=3)
        self.assertTrue(child.stdout.closed)
        self.assertTrue(child.stderr.closed)
        self.assertIsNone(self.hardware.process)

    def test_clean_native_receive_shutdown_skips_control_helper(self):
        child = self.receive_child()
        self.hardware.close()
        child.terminate.assert_not_called()
        self.run.assert_not_called()
        self.assertIsNone(self.hardware.process)

    def test_incomplete_native_cleanup_requires_helper_after_child_reaped(self):
        child = self.receive_child(output=RX_RECEIPT.replace(b"hackrf_close() done\n", b""))
        events = []
        child.wait.side_effect = lambda **kwargs: events.append("reaped") or 0

        def verify_idle(idle=False):
            self.assertTrue(idle)
            self.assertTrue(child.stdout.closed)
            self.assertTrue(child.stderr.closed)
            events.append("mode_off")

        with patch.object(self.hardware, "_control", side_effect=verify_idle) as control:
            self.hardware.close()
        control.assert_called_once_with(idle=True)
        self.assertEqual(events, ["reaped", "mode_off"])
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
