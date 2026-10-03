"""Offline control-safety tests; USB hardware and RF processes are always mocked."""

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import server


TX_RECEIPT = (
    b"Stop with Ctrl-C\n"
    b"Exiting...\nTotal time: 0.20000 s\n"
    b"hackrf_stop_tx() done\n"
    b"hackrf_close() done\n"
    b"hackrf_exit() done\n"
    b"exit\n"
)


class ExperimentControlSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = TemporaryDirectory(prefix="nhi-server-test-")
        self.addCleanup(temporary_directory.cleanup)
        self.runtime_root = Path(temporary_directory.name)
        self.addCleanup(patch.stopall)
        patch.object(server, "RUNTIME", self.runtime_root / "runtime").start()
        self.hardware = Mock(spec=server.Hardware)
        self.hardware.info.return_value = {
            "firmware": "mock-offline",
            "serial": "0" * 32,
            "name": "Offline hardware stub",
        }
        self.hardware_constructor = patch.object(
            server, "Hardware", return_value=self.hardware
        ).start()
        patch.object(server.Experiment, "log").start()
        # Any unexpected child-process invocation is blocked, not just observed.
        self.popen = patch.object(
            server.subprocess,
            "Popen",
            side_effect=AssertionError("An unmocked RF process must never launch in these tests."),
        ).start()

    def make_experiment(self, shielded_test=True):
        experiment = server.Experiment(shielded_test=shielded_test)
        self.hardware_constructor.assert_called_once_with()
        self.hardware.info.assert_called_once_with()
        return experiment

    def completed_child(self, receipt=TX_RECEIPT):
        child = Mock(spec=["returncode", "wait", "poll", "terminate", "kill"])
        child.returncode = 0
        child.wait.return_value = 0
        child.poll.return_value = 0
        self.popen.side_effect = None
        self.popen.return_value = child
        def start_child(*args, **kwargs):
            kwargs['stdout'].write(receipt)
            kwargs['stdout'].flush()
            return child
        self.popen.side_effect = start_child
        return child

    def test_missing_shielded_test_flag_rejects_transmit_loop(self):
        experiment = server.Experiment()
        with patch.object(server.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "declared shielded conducted setup"):
                experiment.start(loop=True)
            thread.assert_not_called()
        self.assertFalse(experiment.data["shielded_test_authorized"])
        self.assertFalse(experiment.data["rf_tx_enabled"])
        self.assertEqual(experiment.data["state"], "stopped")
        self.popen.assert_not_called()
        self.hardware.receive.assert_not_called()

    def test_wrong_transmit_frequency_rejects_loop(self):
        experiment = self.make_experiment()
        with patch.object(server.threading, "Thread") as thread:
            for frequency in (1_200_000_000, 1_575_420_000, 1_600_000_001):
                with self.subTest(frequency=frequency):
                    with self.assertRaisesRegex(ValueError, "1.600 GHz"):
                        experiment.start(frequency=frequency, loop=True)
            thread.assert_not_called()
        self.assertFalse(experiment.data["rf_tx_enabled"])
        self.popen.assert_not_called()
        self.hardware.receive.assert_not_called()

    def test_stop_during_encoding_prevents_process_launch(self):
        experiment = self.make_experiment()

        def stop_during_encoding(*args, **kwargs):
            experiment.stop_event.set()
            return bytes(range(16))

        with patch.object(server, "encode_iq", side_effect=stop_during_encoding):
            experiment._transmit_packet(16)
        self.assertTrue(experiment.stop_event.is_set())
        self.popen.assert_not_called()
        self.hardware.force_idle.assert_not_called()
        self.assertFalse(experiment.data["rf_tx_enabled"])
        self.assertEqual(experiment.data["tx"]["packets_sent"], 0)

    def test_transmit_command_enforces_frequency_power_and_finite_samples(self):
        experiment = self.make_experiment()
        waveform = bytes(range(32))
        child = self.completed_child()
        with patch.object(server, "encode_iq", return_value=waveform):
            experiment._transmit_packet(16)
        self.popen.assert_called_once()
        command = self.popen.call_args.args[0]
        expected_options = {
            "-d": server.SERIAL,
            "-t": str(self.runtime_root / "runtime" / "current-packet.iq"),
            "-f": "1600000000",
            "-s": "8000000",
            "-n": "16",
            "-a": "0",
            "-p": "0",
            "-x": "0",
        }
        self.assertEqual(command[0], str(server.BIN / "hackrf_transfer.exe"))
        self.assertEqual(len(command), 1 + 2 * len(expected_options))
        self.assertEqual(dict(zip(command[1::2], command[2::2])), expected_options)
        self.assertNotIn("-R", command)
        self.assertNotIn("-c", command)
        self.assertNotIn("shell", self.popen.call_args.kwargs)
        self.assertEqual((self.runtime_root / "runtime" / "current-packet.iq").read_bytes(), waveform)
        child.wait.assert_called_once_with(timeout=8)
        child.terminate.assert_not_called()
        self.assertEqual(experiment.data["tx"]["packets_sent"], 1)
        self.assertFalse(experiment.data["rf_tx_enabled"])

    def test_clean_native_shutdown_skips_redundant_idle_helper(self):
        experiment = self.make_experiment()
        child = self.completed_child()
        with patch.object(server, "encode_iq", return_value=bytes(range(16))):
            experiment._transmit_packet(16)
        child.wait.assert_called_once_with(timeout=8)
        self.hardware.force_idle.assert_not_called()
        self.assertIsNone(experiment.tx_process)
        self.assertFalse(experiment.data["rf_tx_enabled"])

    def test_zero_exit_without_checked_cleanup_calls_helper_and_keeps_tx_failure(self):
        experiment = self.make_experiment()
        bad_receipts = (
            TX_RECEIPT.replace(b"hackrf_stop_tx() done\n", b""),
            TX_RECEIPT.replace(b"hackrf_close() done\n", b""),
            TX_RECEIPT.replace(b"hackrf_exit() done\n", b""),
            TX_RECEIPT.replace(b"exit\n", b""),
            TX_RECEIPT.replace(b"hackrf_close() done", b"hackrf_close() FAILED: USB error"),
        )
        for receipt in bad_receipts:
            with self.subTest(receipt=receipt):
                self.completed_child(receipt=receipt)
                self.hardware.force_idle.reset_mock()
                with patch.object(server, "encode_iq", return_value=bytes(range(16))):
                    with self.assertRaisesRegex(RuntimeError, "checked native shutdown"):
                        experiment._transmit_packet(16)
                self.hardware.force_idle.assert_called_once_with()
                self.assertIsNone(experiment.tx_process)
                self.assertFalse(experiment.data["rf_tx_enabled"])
                self.assertEqual(experiment.data["tx"]["packets_sent"], 0)

    def test_stop_after_clean_child_completion_still_skips_idle_helper(self):
        experiment = self.make_experiment()
        child = self.completed_child()

        def completed_as_stop_arrives(**kwargs):
            experiment.stop_event.set()
            return 0

        child.wait.side_effect = completed_as_stop_arrives
        with patch.object(server, "encode_iq", return_value=bytes(range(16))):
            experiment._transmit_packet(16)
        child.terminate.assert_not_called()
        self.hardware.force_idle.assert_not_called()
        self.assertIsNone(experiment.tx_process)
        self.assertFalse(experiment.data["rf_tx_enabled"])

    def test_forced_termination_requires_helper_even_with_zero_exit_and_clean_receipt(self):
        experiment = self.make_experiment()
        child = self.completed_child()
        child.poll.return_value = None
        child.wait.side_effect = [
            subprocess.TimeoutExpired("offline-tx", 8), 0,
        ]
        child.terminate.side_effect = lambda: setattr(child.poll, "return_value", 0)
        with patch.object(server, "encode_iq", return_value=bytes(range(16))):
            with self.assertRaisesRegex(RuntimeError, "8-second deadline"):
                experiment._transmit_packet(16)
        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()
        self.hardware.force_idle.assert_called_once_with()
        self.assertTrue(experiment.tx_interrupted)
        self.assertIsNone(experiment.tx_process)
        self.assertFalse(experiment.data["rf_tx_enabled"])

    def test_transmit_idle_failure_preserves_enabled_flag_and_raises(self):
        experiment = self.make_experiment()
        self.completed_child(receipt=b"unconfirmed cleanup\n")
        self.hardware.force_idle.side_effect = RuntimeError("Cannot verify RF shutdown")
        with patch.object(server, "encode_iq", return_value=bytes(range(16))):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify RF shutdown"):
                experiment._transmit_packet(16)
        self.hardware.force_idle.assert_called_once_with()
        self.assertTrue(experiment.data["rf_tx_enabled"])
        self.assertIsNone(experiment.tx_process)

    def test_failed_fallback_blocks_start_of_another_radio_operation(self):
        experiment = self.make_experiment()
        self.completed_child(receipt=b"unconfirmed cleanup\n")
        self.hardware.force_idle.side_effect = RuntimeError("Cannot verify RF shutdown")
        with patch.object(server, "encode_iq", return_value=bytes(range(16))):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify RF shutdown"):
                experiment._transmit_packet(16)
        self.popen.reset_mock()
        self.hardware.receive.reset_mock()
        with patch.object(server.threading, "Thread") as thread:
            with self.assertRaisesRegex(RuntimeError, "Cannot verify RF shutdown"):
                experiment.start()
            thread.assert_not_called()
        self.popen.assert_not_called()
        self.hardware.receive.assert_not_called()
        self.assertTrue(experiment.data["rf_tx_enabled"])

    def test_stop_idle_failure_preserves_enabled_flag_and_raises(self):
        experiment = self.make_experiment()
        experiment.data.update(state="transmitting", rf_tx_enabled=True)
        self.hardware.force_idle.side_effect = RuntimeError("Cannot verify RF shutdown")
        with self.assertRaisesRegex(RuntimeError, "Cannot verify RF shutdown"):
            experiment.stop()
        self.assertTrue(experiment.stop_event.is_set())
        self.assertTrue(experiment.data["rf_tx_enabled"])
        self.assertEqual(experiment.data["state"], "transmitting")
        self.hardware.force_idle.assert_called_once_with()
        self.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
