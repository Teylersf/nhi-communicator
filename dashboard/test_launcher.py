"""Offline launcher behavior; browsers, HTTP, services, and native calls are mocked."""

from contextlib import ExitStack
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import radio_control
import server


def load_launcher():
    specification = importlib.util.spec_from_file_location(
        'nhi_offline_launcher', Path(__file__).resolve().parent.parent / 'launch.py')
    module = importlib.util.module_from_spec(specification)
    original_path = list(sys.path)
    try:
        specification.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    return module


launcher = load_launcher()
probe_current_service = launcher.current_service


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.service_patch = patch.object(launcher, 'current_service', return_value=None)
        self.service = self.patches.enter_context(self.service_patch)
        self.browser = self.patches.enter_context(patch.object(launcher.webbrowser, 'open'))
        self.thread = self.patches.enter_context(patch.object(launcher.threading, 'Thread'))
        self.server_main = self.patches.enter_context(patch.object(server, 'main', return_value=0))
        self.control_main = self.patches.enter_context(patch.object(radio_control, 'main', return_value=0))
        self.http = self.patches.enter_context(patch.object(launcher.urllib.request, 'urlopen',
            side_effect=AssertionError('Unexpected HTTP request in offline launcher test.')))
        self.native = self.patches.enter_context(patch.object(subprocess, 'run',
            side_effect=AssertionError('Unexpected native operation in offline launcher test.')))
        self.rf = self.patches.enter_context(patch.object(subprocess, 'Popen',
            side_effect=AssertionError('Unexpected RF process in offline launcher test.')))
        self.info = self.patches.enter_context(patch.object(server.Hardware, 'info',
            side_effect=AssertionError('Unexpected USB discovery in offline launcher test.')))

    def existing(self, *, demo=False, lab=False, name='NHI Communicator'):
        self.service.return_value = {'application_name': name, 'demo_mode': demo,
                                     'shielded_test_authorized': lab}

    def assert_no_radio_or_service_start(self):
        self.server_main.assert_not_called()
        self.control_main.assert_not_called()
        self.native.assert_not_called()
        self.rf.assert_not_called()
        self.info.assert_not_called()
        self.thread.assert_not_called()

    def test_matching_existing_normal_instance_reopens_browser_without_starting_rf(self):
        self.existing()
        self.assertEqual(launcher.main([]), 0)
        self.service.assert_called_once_with(8787)
        self.browser.assert_called_once_with('http://127.0.0.1:8787/')
        self.assert_no_radio_or_service_start()

    def test_matching_existing_demo_instance_reuses_hardware_free_mode(self):
        self.existing(demo=True)
        self.assertEqual(launcher.main(['--demo']), 0)
        self.browser.assert_called_once_with('http://127.0.0.1:8787/')
        self.assert_no_radio_or_service_start()

    def test_matching_demo_with_lab_flag_remains_demo_and_does_not_enable_tx(self):
        self.existing(demo=True)
        self.assertEqual(launcher.main(['--demo', '--shielded-test']), 0)
        self.assert_no_radio_or_service_start()

    def test_matching_existing_lab_instance_is_reused_without_starting_transmission(self):
        self.existing(lab=True)
        self.assertEqual(launcher.main(['--shielded-test']), 0)
        self.assert_no_radio_or_service_start()

    def test_preview_mode_mismatch_rejects_in_both_directions(self):
        for existing_demo, arguments in ((False, ['--demo']), (True, [])):
            with self.subTest(existing_demo=existing_demo):
                self.existing(demo=existing_demo)
                with self.assertRaisesRegex(RuntimeError, 'different preview mode'):
                    launcher.main(arguments)
                self.browser.assert_not_called()
                self.assert_no_radio_or_service_start()

    def test_lab_mode_mismatch_rejects_in_both_directions(self):
        for existing_lab, arguments in ((False, ['--shielded-test']), (True, [])):
            with self.subTest(existing_lab=existing_lab):
                self.existing(lab=existing_lab)
                with self.assertRaisesRegex(RuntimeError, 'different lab mode'):
                    launcher.main(arguments)
                self.browser.assert_not_called()
                self.assert_no_radio_or_service_start()

    def test_foreign_existing_service_is_never_reused_or_changed(self):
        self.existing(name='Another app')
        with self.assertRaisesRegex(RuntimeError, 'belongs to another app'):
            launcher.main([])
        self.browser.assert_not_called()
        self.assert_no_radio_or_service_start()

    def test_no_browser_suppresses_browser_for_existing_service(self):
        self.existing()
        self.assertEqual(launcher.main(['--no-browser']), 0)
        self.browser.assert_not_called()
        self.assert_no_radio_or_service_start()

    def test_no_browser_fresh_instance_delegates_arguments_and_does_not_spawn_browser_thread(self):
        arguments = ['--demo', '--port', '8790', '--data-dir', 'test-state', '--no-browser']
        self.assertEqual(launcher.main(arguments), 0)
        self.service.assert_called_once_with(8790)
        self.server_main.assert_called_once_with(arguments[:-1])
        self.browser.assert_not_called()
        self.thread.assert_not_called()
        self.native.assert_not_called()
        self.rf.assert_not_called()
        self.info.assert_not_called()

    def test_equals_style_port_is_used_for_existing_reuse_and_fresh_server_delegation(self):
        arguments = ['--port=8790', '--no-browser']
        self.existing()
        self.assertEqual(launcher.main(arguments), 0)
        self.service.assert_called_once_with(8790)
        self.assert_no_radio_or_service_start()
        self.browser.assert_not_called()

        self.service.reset_mock()
        self.service.return_value = None
        self.assertEqual(launcher.main(arguments), 0)
        self.service.assert_called_once_with(8790)
        self.server_main.assert_called_once_with(['--port=8790'])
        self.browser.assert_not_called()
        self.thread.assert_not_called()
        self.native.assert_not_called()
        self.rf.assert_not_called()
        self.info.assert_not_called()

    def test_fresh_instance_starts_readiness_thread_and_delegates_default_idle_launch(self):
        self.assertEqual(launcher.main([]), 0)
        self.server_main.assert_called_once_with([])
        self.thread.assert_called_once_with(target=launcher.open_when_ready, args=(8787,), daemon=True)
        self.thread.return_value.start.assert_called_once_with()
        self.browser.assert_not_called()
        self.native.assert_not_called()
        self.rf.assert_not_called()
        self.info.assert_not_called()

    def test_help_and_version_delegate_without_http_or_browser(self):
        for arguments in (['--help'], ['-h'], ['--version']):
            with self.subTest(arguments=arguments):
                self.assertEqual(launcher.main(arguments), 0)
                self.server_main.assert_called_with(arguments)
        self.service.assert_not_called()
        self.browser.assert_not_called()
        self.thread.assert_not_called()
        self.http.assert_not_called()
        self.native.assert_not_called()
        self.rf.assert_not_called()

    def test_radio_control_route_preserves_native_arguments_and_skips_service_probe(self):
        arguments = ['--idle', '--serial', 'a' * 32, '--tools-dir', 'test-bin']
        self.control_main.return_value = 1
        self.assertEqual(launcher.main(['--radio-control', *arguments]), 1)
        self.control_main.assert_called_once_with(arguments)
        self.server_main.assert_not_called()
        self.service.assert_not_called()
        self.browser.assert_not_called()
        self.thread.assert_not_called()
        self.http.assert_not_called()
        self.native.assert_not_called()
        self.rf.assert_not_called()

    def test_actual_service_probe_reads_only_loopback_status_with_bounded_timeout(self):
        self.http.side_effect = None
        payload = {'application_name': 'NHI Communicator', 'demo_mode': True}
        self.http.return_value.__enter__.return_value = BytesIO(json.dumps(payload).encode())
        self.assertEqual(probe_current_service(8790), payload)
        self.http.assert_called_once_with('http://127.0.0.1:8790/api/status', timeout=1)
        self.assert_no_radio_or_service_start()

    def test_unavailable_service_probe_returns_none_without_launching_radio(self):
        self.http.side_effect = OSError('Service offline')
        self.assertIsNone(probe_current_service(8787))
        self.assert_no_radio_or_service_start()


if __name__ == '__main__':
    unittest.main()
