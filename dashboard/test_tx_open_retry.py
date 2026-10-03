"""Offline pre-open TX recovery; completed/possibly-started packets never retry."""

from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import server


OPEN_RECEIPT = (Path(__file__).parent / 'testdata' / 'tx-open-denied.log').read_bytes()
TX_RECEIPT = (b'Stop with Ctrl-C\nExiting...\n'
              b'hackrf_stop_tx() done\nhackrf_close() done\n'
              b'hackrf_exit() done\nexit\n')


class TransmitOpenReceiptTests(unittest.TestCase):
    def test_actual_stock_pre_open_failure_accepts_lf_and_crlf(self):
        normalized = OPEN_RECEIPT.replace(b'\r\n', b'\n')
        self.assertTrue(server.unopened_tx_receipt(1, normalized))
        self.assertTrue(server.unopened_tx_receipt(1, normalized.replace(b'\n', b'\r\n')))

    def test_receipt_requires_exit_one_and_no_interruption(self):
        for code in (0, 2, -1, 3221226505, -1073740791):
            with self.subTest(code=code):
                self.assertFalse(server.unopened_tx_receipt(code, OPEN_RECEIPT))
        self.assertFalse(server.unopened_tx_receipt(1, OPEN_RECEIPT, interrupted=True))

    def test_partial_changed_extra_or_started_receipts_never_claim_unopened_tx(self):
        variants = (
            server.TX_OPEN_DENIED.encode() + b'\n',
            OPEN_RECEIPT.split(b'\n', 3)[0] + b'\nUsage:\n',
            OPEN_RECEIPT.replace(b'Usage:', b'usage:'),
            OPEN_RECEIPT.replace(b'[-H]', b'[-new-version-option]'),
            OPEN_RECEIPT.replace(b'(-1000)', b'(-5)'),
            OPEN_RECEIPT.replace(b'hackrf_open()', b'hackrf_start_tx()'),
            b' ' + OPEN_RECEIPT,
            b'Unexpected warning\n' + OPEN_RECEIPT,
            b'call hackrf_set_freq(1600000000 Hz)\n' + OPEN_RECEIPT,
            b'Stop with Ctrl-C\n' + OPEN_RECEIPT,
            OPEN_RECEIPT + b'call hackrf_set_amp_enable(1)\n',
            OPEN_RECEIPT + b'call hackrf_set_sample_rate(8000000 Hz)\n',
            OPEN_RECEIPT + b'Stop with Ctrl-C\n',
            OPEN_RECEIPT + b'hackrf_start_tx() failed: USB error\n',
            OPEN_RECEIPT + b'hackrf_close() failed\n',
            OPEN_RECEIPT + b'\xff\n',
            OPEN_RECEIPT + OPEN_RECEIPT,
            OPEN_RECEIPT + b'\n',
        )
        for receipt in variants:
            with self.subTest(receipt=receipt[-100:]):
                self.assertFalse(server.unopened_tx_receipt(1, receipt))

    def test_error_summary_returns_first_native_error_instead_of_usage_tail(self):
        detail = server.transmit_error_detail(1, OPEN_RECEIPT)
        self.assertIn(server.TX_OPEN_DENIED, detail)
        self.assertNotIn('Usage:', detail)
        self.assertNotIn('[-H]', detail)
        self.assertIn('failed: configuration rejected', server.transmit_error_detail(
            2, b'call hackrf_set_freq(1600000000 Hz)\nfailed: configuration rejected\n'))
        self.assertIn('No native diagnostic', server.transmit_error_detail(2, b''))


class TransmitOpenRetryTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.root = Path(self.patches.enter_context(TemporaryDirectory(prefix='nhi-offline-tx-open-retry-')))
        self.patches.enter_context(patch.object(server, 'RUNTIME', self.root / 'runtime'))
        self.patches.enter_context(patch.object(server.subprocess, 'run',
            side_effect=AssertionError('Native hardware control blocked in offline retry tests.')))
        self.popen = self.patches.enter_context(patch.object(server.subprocess, 'Popen',
            side_effect=AssertionError('Native TX blocked in offline retry tests.')))
        self.hardware = Mock(spec=server.Hardware)
        self.hardware.info.return_value = {'name': 'offline', 'serial': '0' * 32, 'firmware': 'offline'}
        self.patches.enter_context(patch.object(server, 'Hardware', return_value=self.hardware))
        self.log = self.patches.enter_context(patch.object(server.Experiment, 'log'))
        self.waveform = b'\x02\xfe' * 32
        self.encoder = self.patches.enter_context(patch.object(server, 'encode_iq', return_value=self.waveform))
        self.experiment = server.Experiment(shielded_test=True)
        self.events = []
        self.commands = []
        self.waveforms = []
        self.children = []
        self.outputs = []
        self.backoff = self.patches.enter_context(patch.object(self.experiment.stop_event, 'wait', return_value=False))
        self.hardware.force_idle.side_effect = self.checked_idle
        self.addCleanup(lambda: self.experiment.tx_output and self.experiment.tx_output.close())

    def checked_idle(self):
        self.assertIsNone(self.experiment.tx_process)
        self.assertIsNotNone(self.children[-1].returncode)
        self.assertTrue(self.outputs[-1].closed)
        self.events.append('checked-off')

    def install_children(self, receipts, codes=None):
        codes = codes or [1 if receipt == OPEN_RECEIPT else 0 for receipt in receipts]
        pending = iter(zip(receipts, codes))
        def launch(command, **kwargs):
            if self.outputs:
                self.assertTrue(self.outputs[-1].closed)
                if self.children[-1].returncode == 1:
                    self.assertEqual(self.events[-1], 'checked-off')
            self.commands.append(list(command))
            self.waveforms.append(Path(command[command.index('-t') + 1]).read_bytes())
            output = kwargs['stdout']
            self.outputs.append(output)
            receipt, code = next(pending)
            output.write(receipt)
            output.flush()
            child = Mock(spec=['returncode', 'wait', 'poll', 'terminate', 'kill'])
            child.returncode = code
            child.wait.return_value = code
            child.poll.side_effect = lambda: child.returncode
            self.children.append(child)
            self.events.append('opened-child')
            return child
        self.popen.side_effect = launch

    def test_two_open_denials_then_success_retry_same_packet_waveform_and_settings(self):
        self.experiment.configure_tx(True, 47)
        self.install_children([OPEN_RECEIPT, OPEN_RECEIPT, TX_RECEIPT])
        with patch.object(self.experiment, 'packet', wraps=self.experiment.packet) as packet_builder:
            self.experiment._transmit_packet(1)
        packet_builder.assert_called_once_with(1)
        self.encoder.assert_called_once()
        self.assertEqual(self.experiment.sequence, 2)
        self.assertEqual(self.commands, [self.commands[0]] * 3)
        self.assertEqual(self.waveforms, [self.waveform] * 3)
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 1)
        self.assertEqual(self.experiment.data['tx']['last_payload_sequence'], 2)
        self.assertEqual(self.experiment.data['tx']['open_retries'], 2)
        self.assertEqual(self.experiment.data['tx']['last_tx_settings'], {'rf_amp': True, 'gain_db': 47})
        self.assertEqual([call.args for call in self.backoff.call_args_list], [(0.25,), (1.0,)])
        self.assertEqual(self.hardware.force_idle.call_count, 2)
        self.assertFalse(self.experiment.data['rf_tx_enabled'])
        self.assertIsNone(self.experiment.tx_process)
        for child in self.children:
            child.wait.assert_called_once_with(timeout=8)
            child.terminate.assert_not_called()
            child.kill.assert_not_called()
        failure = json.loads((self.root / 'runtime' / 'last-transmit-open-failure.json').read_text())
        self.assertEqual(failure['packet_sequence'], 2)
        self.assertEqual(failure['attempt'], 2)
        self.assertEqual(failure['receipt'], OPEN_RECEIPT.decode())

    def test_third_open_denial_stops_loop_with_short_error_and_no_further_receive(self):
        self.install_children([OPEN_RECEIPT] * 3)
        with patch.object(self.experiment, '_receive_window') as receive:
            self.experiment._run(server.TX_FREQUENCY, True, 1, 2)
        receive.assert_called_once_with(server.TX_FREQUENCY, 2)
        self.assertEqual(self.popen.call_count, 3)
        self.assertEqual(self.hardware.force_idle.call_count, 3)
        self.assertEqual(self.experiment.data['tx']['open_retries'], 2)
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)
        self.assertEqual(self.experiment.sequence, 2)
        self.encoder.assert_called_once()
        self.assertEqual(self.experiment.data['state'], 'error')
        self.assertFalse(self.experiment.data['test_loop_enabled'])
        self.assertFalse(self.experiment.data['rf_tx_enabled'])
        self.assertEqual(self.experiment.data['error'], server.TX_OPEN_DENIED)

    def test_cleanup_failure_prevents_even_a_pre_open_retry(self):
        self.install_children([OPEN_RECEIPT, TX_RECEIPT])
        self.hardware.force_idle.side_effect = RuntimeError('USB mode OFF not verified')
        with self.assertRaisesRegex(RuntimeError, 'Access denied.*RF shutdown unconfirmed.*USB mode OFF'):
            self.experiment._transmit_packet(1)
        self.popen.assert_called_once()
        self.assertEqual(self.experiment.data['tx']['open_retries'], 0)
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)
        self.assertTrue(self.experiment.data['rf_tx_enabled'])
        self.backoff.assert_not_called()

    def test_stop_during_backoff_does_not_launch_second_child(self):
        self.install_children([OPEN_RECEIPT, TX_RECEIPT])
        def stop(delay):
            self.assertEqual(delay, 0.25)
            self.assertEqual(self.experiment.data['phase'], 'tx_open_retry')
            self.assertEqual(self.experiment.data['state'], 'recovering')
            self.assertFalse(self.experiment.data['rf_tx_enabled'])
            self.experiment.stop_event.set()
            return True
        self.backoff.side_effect = stop
        self.experiment._transmit_packet(1)
        self.popen.assert_called_once()
        self.hardware.force_idle.assert_called_once_with()
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)

    def test_stop_before_failed_child_result_does_not_retry(self):
        self.install_children([OPEN_RECEIPT, TX_RECEIPT])
        original_launch = self.popen.side_effect
        def launch(*args, **kwargs):
            child = original_launch(*args, **kwargs)
            def wait(timeout):
                self.experiment.stop_event.set()
                return 1
            child.wait.side_effect = wait
            return child
        self.popen.side_effect = launch
        self.experiment._transmit_packet(1)
        self.popen.assert_called_once()
        self.backoff.assert_not_called()
        self.assertEqual(self.experiment.data['tx']['open_retries'], 0)

    def test_pending_settings_change_cannot_alter_prepared_open_retry(self):
        self.experiment.configure_tx(True, 47)
        self.install_children([OPEN_RECEIPT, TX_RECEIPT, TX_RECEIPT])
        original_launch = self.popen.side_effect
        def launch(*args, **kwargs):
            child = original_launch(*args, **kwargs)
            if len(self.children) == 1:
                def wait(timeout):
                    self.experiment.configure_tx(False, 7)
                    return 1
                child.wait.side_effect = wait
            return child
        self.popen.side_effect = launch
        self.experiment._transmit_packet(1)
        for command in self.commands:
            self.assertEqual(command[command.index('-a') + 1], '1')
            self.assertEqual(command[command.index('-x') + 1], '47')
        self.assertEqual(self.experiment.data['tx']['gain_db'], 7)
        self.assertFalse(self.experiment.data['tx']['rf_amp'])
        self.experiment._transmit_packet(1)
        self.assertEqual(self.commands[-1][self.commands[-1].index('-a') + 1], '0')
        self.assertEqual(self.commands[-1][self.commands[-1].index('-x') + 1], '7')
        self.assertEqual(self.experiment.sequence, 3)
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 2)

    def test_transfer_timeout_never_retries_even_when_log_looks_like_open_denial(self):
        self.install_children([OPEN_RECEIPT, TX_RECEIPT])
        original_launch = self.popen.side_effect
        def launch(*args, **kwargs):
            child = original_launch(*args, **kwargs)
            child.wait.side_effect = subprocess.TimeoutExpired('offline TX child', 8)
            return child
        self.popen.side_effect = launch
        with self.assertRaisesRegex(RuntimeError, '8-second deadline.*no automatic retransmission'):
            self.experiment._transmit_packet(1)
        self.popen.assert_called_once()
        self.backoff.assert_not_called()
        self.assertEqual(self.experiment.data['tx']['open_retries'], 0)
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)
        self.hardware.force_idle.assert_called_once_with()
        self.assertFalse(self.experiment.data['rf_tx_enabled'])

    def test_unknown_started_partial_and_native_crash_failures_are_not_retried(self):
        cases = (
            (1, OPEN_RECEIPT + b'call hackrf_set_freq(1600000000 Hz)\n'),
            (1, OPEN_RECEIPT + b'Stop with Ctrl-C\n'),
            (1, OPEN_RECEIPT + b'hackrf_start_tx() failed\n'),
            (1, server.TX_OPEN_DENIED.encode() + b'\nUsage:\n'),
            (1, OPEN_RECEIPT.replace(b'(-1000)', b'(-5)')),
            (3221226505, OPEN_RECEIPT),
            (-1073740791, OPEN_RECEIPT),
        )
        for code, receipt in cases:
            with self.subTest(code=code, receipt=receipt[-70:]):
                self.popen.reset_mock()
                self.hardware.force_idle.reset_mock()
                self.install_children([receipt], [code])
                with self.assertRaises(RuntimeError) as error:
                    self.experiment._transmit_packet(1)
                self.assertNotIsInstance(error.exception, server.TransmitOpenError)
                self.popen.assert_called_once()
                self.assertEqual(self.experiment.data['tx']['open_retries'], 0)
                self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)
        self.backoff.assert_not_called()


if __name__ == '__main__':
    unittest.main()
