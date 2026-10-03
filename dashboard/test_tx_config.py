"""Offline TX configuration tests; no native radio process or DLL may run.

Persistence is confined to a temporary ROOT. The only permitted Popen results
are explicit mock children with complete, successful native shutdown receipts.
"""

from contextlib import ExitStack
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import server


TX_RECEIPT = (
    b"Stop with Ctrl-C\n"
    b"Exiting...\n"
    b"hackrf_stop_tx() done\n"
    b"hackrf_close() done\n"
    b"hackrf_exit() done\n"
    b"fclose() done\n"
    b"exit\n"
)


class TxConfigTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.runtime_root = Path(self.patches.enter_context(
            TemporaryDirectory(prefix="nhi-offline-tx-config-")))
        self.patches.enter_context(patch.object(server, "RUNTIME", self.runtime_root / "runtime"))
        self.run_guard = self.patches.enter_context(patch.object(
            server.subprocess, "run",
            side_effect=AssertionError("Native control is blocked in offline TX config tests.")))
        self.popen_guard = self.patches.enter_context(patch.object(
            server.subprocess, "Popen",
            side_effect=AssertionError("Native RF processes are blocked in offline TX config tests.")))
        self.thread_guard = self.patches.enter_context(patch.object(
            server.threading, "Thread",
            side_effect=AssertionError("TX configuration must not start a radio worker.")))
        self.hardware = Mock(spec=server.Hardware)
        self.hardware.info.return_value = {
            "name": "Offline hardware stub", "firmware": "offline", "serial": "0" * 32,
        }
        self.patches.enter_context(patch.object(server, "Hardware", return_value=self.hardware))
        self.patches.enter_context(patch.object(server.Experiment, "log"))
        self.settings_path = self.runtime_root / "runtime" / "tx-settings.json"

    def make_experiment(self, shielded_test=True):
        return server.Experiment(shielded_test=shielded_test)

    def write_settings(self, value):
        self.settings_path.parent.mkdir(exist_ok=True)
        self.settings_path.write_text(json.dumps(value), encoding="utf-8")

    def completed_child(self):
        child = Mock(spec=["returncode", "poll", "wait", "terminate", "kill"])
        child.returncode = 0
        child.poll.return_value = 0
        child.wait.return_value = 0
        return child

    def launch_children(self, *children):
        pending = iter(children)
        def launch(*args, **kwargs):
            kwargs['stdout'].write(TX_RECEIPT)
            kwargs['stdout'].flush()
            return next(pending)
        self.popen_guard.side_effect = launch

    def assert_no_radio_operation(self):
        self.run_guard.assert_not_called()
        self.popen_guard.assert_not_called()
        self.thread_guard.assert_not_called()
        self.hardware.receive.assert_not_called()
        self.hardware.cancel.assert_not_called()
        self.hardware.close.assert_not_called()
        self.hardware.force_idle.assert_not_called()

    def test_validation_accepts_boolean_amplifier_and_each_integer_gain(self):
        for rf_amp in (False, True):
            for gain_db in range(48):
                with self.subTest(rf_amp=rf_amp, gain_db=gain_db):
                    self.assertEqual(server.validated_tx_settings(rf_amp, gain_db),
                                     {"rf_amp": rf_amp, "gain_db": gain_db})
        self.assert_no_radio_operation()

    def test_validation_rejects_truthy_strings_numbers_and_noninteger_gains(self):
        for rf_amp in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(rf_amp=rf_amp):
                with self.assertRaisesRegex(ValueError, "true or false"):
                    server.validated_tx_settings(rf_amp, 0)
        for gain_db in (None, False, True, -1, 48, 0.0, 47.0, "0", "47", [], {}):
            with self.subTest(gain_db=gain_db):
                with self.assertRaisesRegex(ValueError, "integer from 0 to 47"):
                    server.validated_tx_settings(False, gain_db)
        self.assert_no_radio_operation()

    def test_new_session_defaults_to_amplifier_off_zero_gain_and_no_transmit(self):
        experiment = self.make_experiment()
        self.assertIs(experiment.data["tx"]["rf_amp"], False)
        self.assertEqual(experiment.data["tx"]["gain_db"], 0)
        self.assertEqual(experiment.data["tx"]["amplitude"], 8)
        self.assertIsNone(experiment.data["tx"]["last_tx_settings"])
        self.assertIsNone(experiment.tx_process)
        self.assertIsNone(experiment.worker)
        self.assertFalse(experiment.data["rf_tx_enabled"])
        self.assertFalse(self.settings_path.exists())
        self.assert_no_radio_operation()

    def test_maximum_settings_persist_and_reload_without_starting_transmit(self):
        experiment = self.make_experiment()
        experiment.configure_tx(True, 47)
        self.assertEqual(json.loads(self.settings_path.read_text(encoding="utf-8")),
                         {"rf_amp": True, "gain_db": 47})
        self.assertFalse(self.settings_path.with_suffix(".tmp").exists())
        restored = self.make_experiment()
        for session in (experiment, restored):
            self.assertIs(session.data["tx"]["rf_amp"], True)
            self.assertEqual(session.data["tx"]["gain_db"], 47)
            self.assertEqual(session.data["tx"]["amplitude"], 8)
            self.assertEqual(session.data["tx"]["packets_sent"], 0)
            self.assertIsNone(session.data["tx"]["last_tx_settings"])
            self.assertFalse(session.data["rf_tx_enabled"])
            self.assertFalse(session.data["test_loop_enabled"])
            self.assertIsNone(session.worker)
            self.assertIsNone(session.tx_process)
        self.assert_no_radio_operation()

    def test_saved_settings_require_valid_types_and_required_fields_at_startup(self):
        invalid_values = (
            {"rf_amp": 1, "gain_db": 47},
            {"rf_amp": "true", "gain_db": 47},
            {"rf_amp": True, "gain_db": True},
            {"rf_amp": True, "gain_db": 47.0},
            {"rf_amp": True, "gain_db": "47"},
            {"rf_amp": True, "gain_db": -1},
            {"rf_amp": True, "gain_db": 48},
            {"rf_amp": True},
            {"gain_db": 47},
            None,
            [],
        )
        for saved in invalid_values:
            with self.subTest(saved=saved):
                self.write_settings(saved)
                experiment = self.make_experiment()
                self.assertFalse(experiment.data['tx']['rf_amp'])
                self.assertEqual(experiment.data['tx']['gain_db'], 0)
        self.settings_path.write_text("{broken json", encoding="utf-8")
        experiment = self.make_experiment()
        self.assertFalse(experiment.data['tx']['rf_amp'])
        self.assertEqual(experiment.data['tx']['gain_db'], 0)
        self.assert_no_radio_operation()

    def test_receive_only_profile_ignores_saved_tx_gain_and_rejects_config(self):
        self.write_settings({"rf_amp": True, "gain_db": 47})
        experiment = self.make_experiment(shielded_test=False)
        self.assertIs(experiment.data["tx"]["rf_amp"], False)
        self.assertEqual(experiment.data["tx"]["gain_db"], 0)
        with self.assertRaisesRegex(ValueError, "declared shielded conducted setup"):
            experiment.configure_tx(True, 47)
        self.assertFalse(experiment.data["rf_tx_enabled"])
        self.assertEqual(json.loads(self.settings_path.read_text(encoding="utf-8")),
                         {"rf_amp": True, "gain_db": 47})
        self.assert_no_radio_operation()

    def test_invalid_config_does_not_replace_saved_or_in_memory_settings(self):
        experiment = self.make_experiment()
        experiment.configure_tx(False, 11)
        saved = self.settings_path.read_bytes()
        before = copy.deepcopy(experiment.data)
        for rf_amp, gain_db in ((1, 47), ("true", 47), (True, True),
                                (True, 47.0), (True, "47"), (True, -1), (True, 48)):
            with self.subTest(rf_amp=rf_amp, gain_db=gain_db):
                with self.assertRaises(ValueError):
                    experiment.configure_tx(rf_amp, gain_db)
                self.assertEqual(self.settings_path.read_bytes(), saved)
                self.assertEqual(experiment.data, before)
        self.assert_no_radio_operation()

    def test_unconfirmed_shutdown_and_recovery_reject_settings_changes(self):
        experiment = self.make_experiment()
        experiment.configure_tx(False, 11)
        saved = self.settings_path.read_bytes()
        cases = (
            {"state": "error", "phase": "idle", "rf_tx_enabled": True},
            {"state": "receiving", "phase": "receiving", "rf_tx_enabled": True},
            {"state": "recovering", "phase": "recovering", "rf_tx_enabled": False},
        )
        for state in cases:
            with self.subTest(state=state):
                experiment.data.update(state, test_loop_enabled=True)
                before = copy.deepcopy(experiment.data)
                with self.assertRaisesRegex(ValueError, "shutdown|recovery"):
                    experiment.configure_tx(True, 47)
                self.assertEqual(experiment.data, before)
                self.assertEqual(self.settings_path.read_bytes(), saved)
        self.assert_no_radio_operation()

    def test_normal_running_loop_can_change_next_settings_without_touching_child(self):
        experiment = self.make_experiment()
        current_child = self.completed_child()
        experiment.tx_process = current_child
        for phase in ("receiving", "transmitting"):
            with self.subTest(phase=phase):
                experiment.data.update(state=phase, phase=phase, test_loop_enabled=True,
                                       rf_tx_enabled=phase == "transmitting")
                experiment.data["tx"]["last_tx_settings"] = {"rf_amp": False, "gain_db": 0}
                experiment.configure_tx(True, 47)
                self.assertEqual(experiment.data["tx"]["last_tx_settings"],
                                 {"rf_amp": False, "gain_db": 0})
                self.assertIs(experiment.tx_process, current_child)
                self.assertEqual(experiment.data["phase"], phase)
                self.assertTrue(experiment.data["test_loop_enabled"])
                current_child.assert_not_called()
                self.assertEqual(current_child.mock_calls, [])
        self.assert_no_radio_operation()

    def test_next_packet_uses_maximum_rf_gain_with_fixed_waveform_and_finite_command(self):
        experiment = self.make_experiment()
        experiment.configure_tx(True, 47)
        child = self.completed_child()
        self.launch_children(child)
        waveform = bytes(range(32))
        with patch.object(server, "encode_iq", return_value=waveform) as encoder:
            experiment._transmit_packet(2)
        self.popen_guard.assert_called_once()
        command = self.popen_guard.call_args.args[0]
        self.assertEqual(command[0], str(server.BIN / "hackrf_transfer.exe"))
        self.assertEqual(dict(zip(command[1::2], command[2::2])), {
            "-d": server.SERIAL,
            "-t": str(self.runtime_root / "runtime" / "current-packet.iq"),
            "-f": "1600000000", "-s": "8000000", "-n": "16",
            "-a": "1", "-p": "0", "-x": "47",
        })
        self.assertNotIn("-R", command)
        self.assertNotIn("-c", command)
        self.assertNotIn("shell", self.popen_guard.call_args.kwargs)
        encoder.assert_called_once_with(
            experiment.data["packet"]["payload_text"].encode("ascii"), amplitude=8)
        self.assertEqual((self.runtime_root / "runtime" / "current-packet.iq").read_bytes(), waveform)
        self.assertEqual(experiment.data["tx"]["last_tx_settings"], {"rf_amp": True, "gain_db": 47})
        self.assertEqual(experiment.data["tx"]["packets_sent"], 1)
        self.assertFalse(experiment.data["rf_tx_enabled"])
        self.assertIsNone(experiment.tx_process)
        child.wait.assert_called_once_with(timeout=8)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        self.hardware.force_idle.assert_not_called()
        self.run_guard.assert_not_called()
        self.thread_guard.assert_not_called()

    def test_config_during_transmission_preserves_current_snapshot_and_applies_to_next_child(self):
        experiment = self.make_experiment()
        experiment.configure_tx(True, 47)
        first_child, second_child = self.completed_child(), self.completed_child()
        self.launch_children(first_child, second_child)

        def change_pending_settings(timeout):
            self.assertEqual(timeout, 8)
            self.assertEqual(experiment.data["phase"], "transmitting")
            self.assertTrue(experiment.data["rf_tx_enabled"])
            self.assertIs(experiment.tx_process, first_child)
            experiment.configure_tx(False, 7)
            self.assertEqual(experiment.data["tx"]["last_tx_settings"],
                             {"rf_amp": True, "gain_db": 47})
            self.assertEqual(self.popen_guard.call_count, 1)
            return 0

        first_child.wait.side_effect = change_pending_settings
        with patch.object(server, "encode_iq", return_value=bytes(range(32))) as encoder:
            experiment._transmit_packet(2)
            first_command = self.popen_guard.call_args_list[0].args[0]
            self.assertEqual(first_command[first_command.index("-a") + 1], "1")
            self.assertEqual(first_command[first_command.index("-x") + 1], "47")
            self.assertIs(experiment.data["tx"]["rf_amp"], False)
            self.assertEqual(experiment.data["tx"]["gain_db"], 7)
            experiment._transmit_packet(2)
        self.assertEqual(self.popen_guard.call_count, 2)
        second_command = self.popen_guard.call_args_list[1].args[0]
        self.assertEqual(second_command[second_command.index("-a") + 1], "0")
        self.assertEqual(second_command[second_command.index("-x") + 1], "7")
        self.assertEqual(first_command[first_command.index("-a") + 1], "1")
        self.assertEqual(first_command[first_command.index("-x") + 1], "47")
        self.assertEqual(experiment.data["tx"]["last_tx_settings"], {"rf_amp": False, "gain_db": 7})
        self.assertEqual(experiment.data["tx"]["packets_sent"], 2)
        self.assertTrue(all(call.kwargs["amplitude"] == 8 for call in encoder.call_args_list))
        for child in (first_child, second_child):
            child.terminate.assert_not_called()
            child.kill.assert_not_called()
        self.hardware.force_idle.assert_not_called()
        self.hardware.receive.assert_not_called()
        self.run_guard.assert_not_called()
        self.thread_guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
