"""Offline native-helper wrapper tests. Every process/DLL entry point is mocked."""

from contextlib import redirect_stdout
import ctypes
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import radio_control


IDLE_RECEIPT = (
    b"begin:load\ndone:load\n"
    b"begin:init\ndone:init\n"
    b"begin:open\ndone:open\n"
    b"begin:close\ndone:close\n"
    b"begin:exit\ndone:exit\n"
    b"begin:unload\ndone:unload\n"
)
INFO_RECEIPT = IDLE_RECEIPT.replace(
    b"begin:close\n", b"begin:firmware\ndone:firmware\nbegin:close\n"
)


class RadioControlTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = TemporaryDirectory(prefix="nhi-control-wrapper-test-")
        self.addCleanup(temporary_directory.cleanup)
        self.runtime_root = Path(temporary_directory.name)
        # A harmless file supports wrappers that also check executable existence.
        (self.runtime_root / "radio_control_native.exe").write_bytes(b"offline placeholder")
        root_patch = patch.object(radio_control, "ROOT", self.runtime_root)
        run_patch = patch.object(
            subprocess, "run",
            side_effect=AssertionError("Unexpected native process in offline tests"),
        )
        popen_patch = patch.object(
            subprocess, "Popen",
            side_effect=AssertionError("Unexpected alternate process in offline tests"),
        )
        dll_patch = patch.object(
            ctypes, "CDLL",
            side_effect=AssertionError("No DLL loading is permitted in offline wrapper tests"),
        )
        for patcher in (root_patch, run_patch, popen_patch, dll_patch):
            self.addCleanup(patcher.stop)
        root_patch.start()
        serial_patch = patch.object(radio_control, "SERIAL", "0" * 32)
        self.addCleanup(serial_patch.stop)
        serial_patch.start()
        self.run = run_patch.start()
        self.popen = popen_patch.start()
        self.dll = dll_patch.start()
        self.info = {
            "name": "HackRF One", "serial": radio_control.SERIAL,
            "firmware": "2024.02.1",
        }
        self.idle = {"idle": True, "serial": radio_control.SERIAL}

    def child_result(self, payload, receipt=INFO_RECEIPT, returncode=0):
        stdout = payload if isinstance(payload, bytes) else json.dumps(payload).encode("ascii")
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], returncode, stdout, receipt)

    def assert_rejected(self, payload, *, idle=False, receipt=INFO_RECEIPT):
        self.child_result(payload, receipt=receipt)
        with self.assertRaises(RuntimeError):
            radio_control.run_control(idle=idle)
        self.run.assert_called_once()
        self.run.reset_mock()

    def assert_command(self, idle):
        expected = [str(self.runtime_root / "radio_control_native.exe"), "--dll",
                    str(radio_control.BIN / "hackrf-0.dll"), "--serial", radio_control.SERIAL]
        if idle:
            expected.append("--idle")
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0], expected)
        keywords = self.run.call_args.kwargs
        self.assertTrue(keywords["capture_output"])
        self.assertEqual(keywords["timeout"], 8)
        self.assertEqual(keywords["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(keywords["env"]["PATH"].split(os.pathsep)[0], str(radio_control.BIN))
        self.assertNotIn("shell", keywords)
        self.popen.assert_not_called()
        self.dll.assert_not_called()

    def test_info_uses_one_native_child_and_validates_control_receipts(self):
        self.child_result(self.info)
        self.assertEqual(radio_control.run_control(), self.info)
        self.assert_command(idle=False)

    def test_idle_uses_one_native_child_and_explicit_idle_argument(self):
        self.child_result(self.idle, receipt=IDLE_RECEIPT)
        self.assertEqual(radio_control.run_control(idle=True), self.idle)
        self.assert_command(idle=True)

    def test_wrong_serial_is_rejected_in_both_modes(self):
        for idle, template in ((False, self.info), (True, self.idle)):
            with self.subTest(idle=idle):
                self.assert_rejected(dict(template, serial="f" * 32), idle=idle)

    def test_missing_or_false_idle_mode_is_rejected(self):
        payloads = (
            {"serial": radio_control.SERIAL},
            dict(self.idle, idle=False),
            dict(self.idle, idle=1),
            dict(self.idle, idle="true"),
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assert_rejected(payload, idle=True, receipt=IDLE_RECEIPT)

    def test_info_requires_matching_name(self):
        for name in (None, "Unknown device"):
            with self.subTest(name=name):
                payload = dict(self.info)
                if name is None:
                    del payload["name"]
                else:
                    payload["name"] = name
                self.assert_rejected(payload)

    def test_info_requires_nonempty_ascii_firmware_at_most_255_bytes(self):
        missing = dict(self.info)
        del missing["firmware"]
        payloads = (missing,) + tuple(
            dict(self.info, firmware=value) for value in ("", None, 123, "é", "x" * 256)
        )
        for payload in payloads:
            with self.subTest(firmware=payload.get("firmware")):
                self.assert_rejected(payload)

    def test_info_accepts_maximum_ascii_firmware_length(self):
        payload = dict(self.info, firmware="x" * 255)
        self.child_result(payload)
        self.assertEqual(radio_control.run_control()["firmware"], "x" * 255)
        self.run.assert_called_once()

    def test_malformed_json_and_multiple_stdout_documents_are_rejected(self):
        for stdout in (b"not json", b"{", b'{}\n{}', b"\xff"):
            with self.subTest(stdout=stdout):
                self.assert_rejected(stdout)

    def test_stdout_json_must_be_an_object(self):
        for payload in (None, [], True, 7, "text"):
            with self.subTest(payload=payload):
                self.assert_rejected(payload)

    def test_each_required_exact_control_receipt_is_required(self):
        for marker in (b"done:open\n", b"done:close\n", b"done:exit\n"):
            with self.subTest(missing=marker):
                self.assert_rejected(self.info, receipt=INFO_RECEIPT.replace(marker, b""))
            with self.subTest(inexact=marker):
                self.assert_rejected(
                    self.info, receipt=INFO_RECEIPT.replace(marker, marker.rstrip() + b" unexpected\n")
                )

    def test_required_receipts_must_appear_in_control_order(self):
        receipts = (
            b"done:close\ndone:open\ndone:exit\n",
            b"done:open\ndone:exit\ndone:close\n",
        )
        for receipt in receipts:
            with self.subTest(receipt=receipt):
                self.assert_rejected(self.info, receipt=receipt)

    def test_error_json_is_rejected_even_with_success_exit_and_receipts(self):
        self.assert_rejected(dict(self.info, error="close failed"))

    def test_failed_native_phase_is_rejected_even_with_done_receipts(self):
        self.assert_rejected(self.info, receipt=INFO_RECEIPT + b"failed:close:-1000\n")

    def test_native_crash_preserves_unsigned_hex_code_and_last_phase(self):
        for returncode in (3221226505, -1073740791):
            with self.subTest(returncode=returncode):
                self.child_result(
                    b"", returncode=returncode,
                    receipt=b"begin:load\ndone:load\nbegin:open\ndone:open\nbegin:close\n",
                )
                with self.assertRaises(RuntimeError) as error:
                    radio_control.run_control(idle=True)
                message = str(error.exception)
                self.assertIn("0XC0000409", message.upper())
                self.assertIn("begin:close", message)
                self.run.assert_called_once()
                self.run.reset_mock()

    def test_nonzero_native_error_preserves_primary_and_cleanup_details(self):
        self.child_result(
            {"error": "firmware: USB error (-1000); close: USB error (-1000)"},
            returncode=1,
            receipt=b"done:open\nfailed:firmware:-1000\nbegin:close\nfailed:close:-1000\n",
        )
        with self.assertRaises(RuntimeError) as error:
            radio_control.run_control()
        self.assertIn("firmware: USB error", str(error.exception))
        self.assertIn("close: USB error", str(error.exception))
        self.assertIn("failed:close:-1000", str(error.exception))
        self.run.assert_called_once()

    def test_timeout_propagates_without_retry_or_alternate_process(self):
        self.run.side_effect = subprocess.TimeoutExpired("mock-native-control", 8)
        with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
            radio_control.run_control(idle=True)
        self.run.assert_called_once()
        self.popen.assert_not_called()
        self.dll.assert_not_called()

    def test_cli_success_emits_one_info_json_document(self):
        self.child_result(self.info)
        output = StringIO()
        with redirect_stdout(output):
            code = radio_control.main([])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), self.info)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.run.assert_called_once()

    def test_cli_idle_failure_emits_one_error_json_and_nonzero_status(self):
        self.child_result(
            b"", returncode=3221226505,
            receipt=b"done:open\nbegin:close\n",
        )
        output = StringIO()
        with redirect_stdout(output):
            code = radio_control.main(["--idle"])
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertIn("0XC0000409", payload["error"].upper())
        self.assertIn("begin:close", payload["error"])
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
