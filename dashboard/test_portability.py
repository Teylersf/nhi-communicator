"""Public app startup and safety tests, always isolated from USB and user state."""

from contextlib import ExitStack, redirect_stdout
from io import StringIO, BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app_config
import radio_control
import server


class PortableStartupTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.root = Path(self.patches.enter_context(tempfile.TemporaryDirectory()))
        self.patches.enter_context(patch.object(server, 'RUNTIME', self.root / 'user-state'))
        self.native = self.patches.enter_context(patch.object(subprocess, 'run',
            side_effect=AssertionError('Unexpected native/USB operation in offline test.')))
        self.rf = self.patches.enter_context(patch.object(subprocess, 'Popen',
            side_effect=AssertionError('Unexpected RF child in offline test.')))

    def test_demo_never_probes_hardware_and_has_offline_packet_preview(self):
        experiment = server.Experiment(shielded_test=True, demo=True)
        self.assertFalse(experiment.data['device_available'])
        self.assertFalse(experiment.data['shielded_test_authorized'])
        self.assertEqual(experiment.packet(64)['prime_count'], 64)
        self.assertEqual(experiment.waterfall()['rows'], [])
        experiment.stop()
        self.native.assert_not_called()
        self.rf.assert_not_called()

    def test_demo_rejects_all_receive_and_loop_starts(self):
        experiment = server.Experiment(demo=True)
        for loop in (False, True):
            with self.subTest(loop=loop), self.assertRaisesRegex(ValueError, 'Offline preview'):
                experiment.start(loop=loop)
        self.native.assert_not_called()
        self.rf.assert_not_called()

    def test_missing_hardware_keeps_dashboard_preview_available(self):
        with patch.object(server.Hardware, 'info', side_effect=RuntimeError('Found 0 devices')):
            experiment = server.Experiment()
        self.assertEqual(experiment.data['state'], 'stopped')
        self.assertFalse(experiment.data['device_available'])
        self.assertIn('Found 0 devices', experiment.data['device_error'])
        self.assertEqual(experiment.packet(8)['prime_count'], 8)
        self.assertIsNone(experiment.worker)
        self.native.assert_not_called()
        self.rf.assert_not_called()

    def test_receive_reconnect_failure_does_not_start_a_worker(self):
        with patch.object(server.Hardware, 'info', side_effect=RuntimeError('Device disconnected')) as info:
            experiment = server.Experiment()
            with self.assertRaisesRegex(RuntimeError, 'connection unavailable'):
                experiment.start()
        self.assertEqual(info.call_count, 2)
        self.assertIsNone(experiment.worker)
        self.assertFalse(experiment.data['rf_tx_enabled'])
        self.rf.assert_not_called()

    def test_default_profile_does_not_read_or_apply_saved_tx_settings(self):
        state = self.root / 'user-state'
        state.mkdir()
        (state / 'tx-settings.json').write_text(json.dumps({'rf_amp': True, 'gain_db': 47}))
        experiment = server.Experiment(demo=True)
        self.assertFalse(experiment.data['tx']['rf_amp'])
        self.assertEqual(experiment.data['tx']['gain_db'], 0)
        self.assertFalse(experiment.data['test_loop_enabled'])

    def test_frozen_control_routes_to_launcher_subcommand_with_explicit_bin_serial(self):
        result = subprocess.CompletedProcess([], 0, b'{"idle":true}', b'')
        self.native.side_effect = None
        self.native.return_value = result
        with patch.object(server.sys, 'frozen', True, create=True), \
             patch.object(server, 'SERIAL', 'a' * 32), patch.object(server, 'BIN', self.root / 'bin'):
            server.Hardware()._control(idle=True)
        command = self.native.call_args.args[0]
        self.assertEqual(command[:2], [server.sys.executable, '--radio-control'])
        self.assertEqual(command[2:], ['--tools-dir', str(self.root / 'bin'), '--serial', 'a' * 32, '--idle'])

    def test_version_and_help_do_not_probe_hardware(self):
        for argument in ('--version', '--help'):
            with self.subTest(argument=argument), redirect_stdout(StringIO()) as output:
                with self.assertRaises(SystemExit) as stopped:
                    server.main([argument])
                self.assertEqual(stopped.exception.code, 0)
                self.assertIn('NHI Communicator', output.getvalue())
        self.native.assert_not_called()
        self.rf.assert_not_called()

    def test_loop_http_requires_actual_boolean_setup_declaration(self):
        experiment = Mock()
        experiment.quitting = False
        for declaration in (None, False, 1, 'true'):
            request = server.Handler.__new__(server.Handler)
            request.path = '/api/loop/start'
            body = json.dumps({'shielded_setup_confirmed': declaration}).encode()
            request.headers = {'Host': '127.0.0.1:8787', 'Content-Length': str(len(body))}
            request.rfile = BytesIO(body)
            request.server = SimpleNamespace(server_port=8787, experiment=experiment)
            request.send_json = Mock()
            request.do_POST()
            self.assertEqual(request.send_json.call_args.args[1], 400)
        experiment.start.assert_not_called()

    def quit_request(self, *, host='127.0.0.1:8787', origin=None):
        request = server.Handler.__new__(server.Handler)
        request.path = '/api/quit'
        request.headers = {'Host': host, 'Content-Length': '2'}
        if origin:
            request.headers['Origin'] = origin
        request.rfile = BytesIO(b'{}')
        experiment = Mock()
        experiment.quitting = False
        experiment.shutdown_confirmed = False
        experiment.status.return_value = {'state': 'stopped', 'rf_tx_enabled': False,
                                          'test_loop_enabled': False}
        request.server = SimpleNamespace(server_port=8787, experiment=experiment, shutdown=Mock())
        request.send_json = Mock()
        return request, experiment

    def test_quit_replies_after_checked_stop_then_schedules_server_shutdown(self):
        request, experiment = self.quit_request()
        events = []
        experiment.stop.side_effect = lambda: events.append('stopped')
        request.send_json.side_effect = lambda *_: events.append('replied')
        with patch.object(server.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = lambda: events.append('shutdown-scheduled')
            request.do_POST()
        self.assertEqual(events, ['stopped', 'replied', 'shutdown-scheduled'])
        self.assertTrue(request.send_json.call_args.args[0]['closed'])
        self.assertTrue(experiment.quitting)
        thread.assert_called_once_with(target=request.server.shutdown, daemon=True,
                                       name='nhi-dashboard-shutdown')

    def test_quit_failure_keeps_service_open_without_closed_receipt(self):
        request, experiment = self.quit_request()
        experiment.stop.side_effect = RuntimeError('RF shutdown unconfirmed')
        with patch.object(server.threading, 'Thread') as thread:
            request.do_POST()
            thread.assert_not_called()
        self.assertEqual(request.send_json.call_args.args[1], 500)
        self.assertNotIn('closed', request.send_json.call_args.args[0])
        self.assertFalse(experiment.quitting)
        request.server.shutdown.assert_not_called()

    def test_quit_rejects_inconsistent_shutdown_snapshot(self):
        request, experiment = self.quit_request()
        experiment.status.return_value['rf_tx_enabled'] = True
        with patch.object(server.threading, 'Thread') as thread:
            request.do_POST()
            thread.assert_not_called()
        self.assertEqual(request.send_json.call_args.args[1], 500)
        request.server.shutdown.assert_not_called()

    def test_quit_host_and_origin_guards_block_radio_control(self):
        for host, origin in (('evil.example', None), ('127.0.0.1:8787', 'http://evil.example')):
            with self.subTest(host=host, origin=origin):
                request, experiment = self.quit_request(host=host, origin=origin)
                with patch.object(server.threading, 'Thread') as thread:
                    request.do_POST()
                    thread.assert_not_called()
                self.assertEqual(request.send_json.call_args.args[1], 403)
                experiment.stop.assert_not_called()

    def test_quit_in_progress_blocks_serialized_radio_start_before_cleanup_or_thread(self):
        with patch.object(server.Hardware, 'info', return_value={'name': 'HackRF One', 'serial': '0' * 32}):
            experiment = server.Experiment(shielded_test=True)
        experiment.quitting = True
        with patch.object(experiment, '_stop') as stop, patch.object(server.threading, 'Thread') as thread:
            for loop in (False, True):
                with self.subTest(loop=loop), self.assertRaisesRegex(ValueError, 'shutting down'):
                    experiment.start(loop=loop)
            stop.assert_not_called()
            thread.assert_not_called()
        self.rf.assert_not_called()

    def test_quit_in_progress_blocks_tx_configuration_without_persisting(self):
        with patch.object(server.Hardware, 'info', return_value={'name': 'HackRF One', 'serial': '0' * 32}):
            experiment = server.Experiment(shielded_test=True)
        experiment.quitting = True
        with self.assertRaisesRegex(ValueError, 'shutting down'):
            experiment.configure_tx(True, 47)
        self.assertFalse((self.root / 'user-state' / 'tx-settings.json').exists())

    def test_failed_quit_http_reply_does_not_reopen_radio_admission_after_checked_stop(self):
        request, experiment = self.quit_request()
        request.send_json.side_effect = [BrokenPipeError('Browser disconnected'), None]
        with patch.object(server.threading, 'Thread') as thread:
            request.do_POST()
            thread.assert_not_called()
        self.assertTrue(experiment.quitting)
        self.assertTrue(experiment.shutdown_confirmed)


class PortablePathTests(unittest.TestCase):
    def test_explicit_tools_and_environment_paths_work_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='nhi tools ') as directory:
            expected = Path(directory).resolve()
            with patch.dict(os.environ, {'NHI_HACKRF_BIN': directory}):
                self.assertEqual(app_config.tools_directory(), expected)
                self.assertEqual(app_config.tools_directory(directory), expected)

    def test_data_directory_is_outside_installed_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LOCALAPPDATA': directory, 'XDG_STATE_HOME': directory}):
                chosen = app_config.data_directory()
                self.assertEqual(chosen.parent, Path(directory))
                self.assertFalse(chosen.exists())
                self.assertNotEqual(chosen.parent, app_config.ASSET_ROOT)

    def test_serial_validation_is_strict_and_normalizes_hex(self):
        self.assertIsNone(app_config.device_serial(None))
        self.assertEqual(app_config.device_serial('A' * 32), 'a' * 32)
        for value in ('a' * 31, 'a' * 33, 'z' * 32, '../hackrf', 123, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app_config.device_serial(value)

    def test_idle_requires_selected_serial_before_subprocess(self):
        with patch.object(radio_control, 'SERIAL', None), patch.object(subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'explicitly selected'):
                radio_control.run_control(idle=True)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
