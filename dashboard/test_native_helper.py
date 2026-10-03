"""Exercise the actual native helper against a fake DLL, never radio hardware.

Build fixtures on Windows with Build-RadioControl.ps1 and the -TestFixture flag.
Source-only/non-Windows test runs skip these integration cases explicitly.
"""

import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent
HELPER = ROOT / 'radio_control_native.exe'
FAKE_DLL = ROOT / 'testdata' / 'mock-hackrf.dll'


@unittest.skipUnless(os.name == 'nt' and HELPER.is_file() and FAKE_DLL.is_file(),
                     'Windows native helper and offline fixture must be built first.')
class NativeHelperIntegrationTests(unittest.TestCase):
    def invoke(self, arguments=(), *, count=1, serial=None, board=1):
        environment = os.environ.copy()
        environment.update(NHI_TEST_DEVICE_COUNT=str(count), NHI_TEST_BOARD_ID=str(board))
        environment.pop('NHI_TEST_SERIAL', None)
        if serial is not None:
            environment['NHI_TEST_SERIAL'] = serial
        return subprocess.run([str(HELPER), '--dll', str(FAKE_DLL), *arguments],
                              capture_output=True, timeout=3, env=environment,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_single_valid_device_is_discovered_and_closed(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        answer = json.loads(result.stdout)
        self.assertEqual(answer['serial'], 'a' * 32)
        self.assertEqual(answer['firmware'], 'offline-fixture')
        for marker in (b'done:discovery', b'done:open', b'done:board_id', b'done:close', b'done:exit'):
            self.assertIn(marker, result.stderr)

    def test_zero_and_multiple_devices_never_open_a_device(self):
        for count in (0, 2, 7):
            with self.subTest(count=count):
                result = self.invoke(count=count)
                self.assertEqual(result.returncode, 1)
                self.assertIn('requires exactly one', json.loads(result.stdout)['error'])
                self.assertNotIn(b'begin:open', result.stderr)
                self.assertIn(b'done:exit', result.stderr)

    def test_invalid_discovered_serial_never_opens_a_device(self):
        for serial in ('a' * 31, 'z' * 32):
            with self.subTest(serial=serial):
                result = self.invoke(serial=serial)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(b'begin:open', result.stderr)
                self.assertIn(b'done:exit', result.stderr)

    def test_explicit_serial_bypasses_ambiguous_discovery(self):
        result = self.invoke(('--serial', 'B' * 32), count=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['serial'], 'b' * 32)
        self.assertNotIn(b'begin:discovery', result.stderr)

    def test_idle_requires_serial_without_loading_any_dll(self):
        result = self.invoke(('--idle',))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b'begin:load', result.stderr)

    def test_explicit_idle_has_checked_shutdown_without_firmware_or_board_reads(self):
        result = self.invoke(('--serial', 'a' * 32, '--idle'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['idle'])
        self.assertNotIn(b'begin:firmware', result.stderr)
        self.assertNotIn(b'begin:board_id', result.stderr)
        for marker in (b'done:open', b'done:close', b'done:exit'):
            self.assertIn(marker, result.stderr)

    def test_other_hackrf_models_are_rejected_and_closed(self):
        result = self.invoke(board=2)
        self.assertEqual(result.returncode, 1)
        self.assertIn('requires HackRF One', json.loads(result.stdout)['error'])
        self.assertNotIn(b'begin:firmware', result.stderr)
        self.assertIn(b'done:close', result.stderr)
        self.assertIn(b'done:exit', result.stderr)

    def test_invalid_requested_serial_does_not_load_dll(self):
        result = self.invoke(('--serial', 'z' * 32))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b'begin:load', result.stderr)


if __name__ == '__main__':
    unittest.main()
