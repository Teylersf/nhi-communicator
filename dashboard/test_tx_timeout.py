"""Offline TX deadlines and process ownership; no USB or radio operation runs."""

from contextlib import ExitStack
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import server


TX_RECEIPT = (b'Stop with Ctrl-C\nExiting...\n'
              b'hackrf_stop_tx() done\nhackrf_close() done\n'
              b'hackrf_exit() done\nexit\n')
PARTIAL_RECEIPT = b'call hackrf_set_amp_enable(1)\nStop with Ctrl-C\n'


class TxTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.root = Path(self.patches.enter_context(TemporaryDirectory(prefix='nhi-offline-tx-timeout-')))
        self.patches.enter_context(patch.object(server, 'RUNTIME', self.root / 'runtime'))
        self.patches.enter_context(patch.object(server.subprocess, 'run',
            side_effect=AssertionError('Native radio control is blocked in offline tests.')))
        self.popen = self.patches.enter_context(patch.object(server.subprocess, 'Popen',
            side_effect=AssertionError('Native RF process is blocked in offline tests.')))
        self.hardware = Mock(spec=server.Hardware)
        self.hardware.info.return_value = {'name': 'offline', 'serial': '0' * 32, 'firmware': 'offline'}
        self.patches.enter_context(patch.object(server, 'Hardware', return_value=self.hardware))
        self.patches.enter_context(patch.object(server.Experiment, 'log'))
        self.patches.enter_context(patch.object(server, 'encode_iq', return_value=b'\x00\x00' * 32))
        self.experiment = server.Experiment(shielded_test=True)
        self.addCleanup(lambda: self.experiment.tx_output and self.experiment.tx_output.close())
        self.events = []
        self.child = Mock(spec=['returncode', 'poll', 'wait', 'terminate', 'kill'])
        self.child.returncode = None
        self.child.poll.side_effect = lambda: self.child.returncode
        self.child.terminate.side_effect = lambda: self.events.append('terminate')
        self.child.kill.side_effect = lambda: self.events.append('kill')
        self.log_path = self.root / 'runtime' / 'last-transmit.log'

        def launch(*args, **kwargs):
            self.output = kwargs['stdout']
            self.assertNotEqual(self.output, subprocess.PIPE)
            self.assertEqual(kwargs['stderr'], subprocess.STDOUT)
            self.output.write(PARTIAL_RECEIPT)
            self.output.flush()
            return self.child

        def verify_idle():
            self.assertIsNotNone(self.child.returncode, 'Idle attempted while child still owns radio.')
            self.assertIsNone(self.experiment.tx_process)
            self.events.append('idle')

        self.popen.side_effect = launch
        self.hardware.force_idle.side_effect = verify_idle

    def timeout(self, seconds):
        self.events.append(f'wait-{seconds}')
        return subprocess.TimeoutExpired('offline-tx', seconds)

    def reap(self, code=1):
        self.events.append('reaped')
        self.child.returncode = code
        return code

    def test_timeout_is_eight_seconds_and_partial_receipt_survives_cleanup(self):
        calls = []
        def wait(timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise self.timeout(timeout)
            return self.reap()
        self.child.wait.side_effect = wait
        with self.assertRaisesRegex(RuntimeError, '8-second deadline.*no automatic retransmission') as failure:
            self.experiment._transmit_packet(8)
        self.assertIsInstance(failure.exception.__cause__, subprocess.TimeoutExpired)
        self.assertEqual(failure.exception.__cause__.timeout, 8)
        self.assertEqual(calls, [8, 3])
        self.child.terminate.assert_called_once_with()
        self.child.kill.assert_not_called()
        self.hardware.force_idle.assert_called_once_with()
        self.assertLess(self.events.index('reaped'), self.events.index('idle'))
        self.assertEqual(self.log_path.read_bytes(), PARTIAL_RECEIPT)
        self.assertEqual(self.experiment.tx_receipt, PARTIAL_RECEIPT)
        self.assertTrue(self.output.closed)
        self.assertIsNone(self.experiment.tx_process)
        self.assertFalse(self.experiment.data['rf_tx_enabled'])
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)
        self.popen.assert_called_once()

    def test_failed_terminate_wait_escalates_to_kill_and_preserves_primary_deadline(self):
        calls = []
        def wait(timeout):
            calls.append(timeout)
            if len(calls) < 3:
                raise self.timeout(timeout)
            return self.reap()
        self.child.wait.side_effect = wait
        with self.assertRaisesRegex(RuntimeError, '8-second deadline'):
            self.experiment._transmit_packet(8)
        self.assertEqual(calls, [8, 3, 3])
        self.child.terminate.assert_called_once_with()
        self.child.kill.assert_called_once_with()
        self.assertLess(self.events.index('terminate'), self.events.index('kill'))
        self.assertLess(self.events.index('kill'), self.events.index('reaped'))
        self.assertLess(self.events.index('reaped'), self.events.index('idle'))
        self.assertFalse(self.experiment.data['rf_tx_enabled'])
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)
        self.popen.assert_called_once()

    def test_unreaped_child_retains_ownership_log_and_both_deadline_failures(self):
        self.child.wait.side_effect = lambda timeout: (_ for _ in ()).throw(self.timeout(timeout))
        with self.assertRaisesRegex(RuntimeError, '8-second deadline.*RF shutdown unconfirmed.*3-second kill'):
            self.experiment._transmit_packet(8)
        self.assertIs(self.experiment.tx_process, self.child)
        self.assertIs(self.experiment.tx_output, self.output)
        self.assertFalse(self.output.closed)
        self.assertEqual(self.log_path.read_bytes(), PARTIAL_RECEIPT)
        self.assertEqual(self.experiment.tx_receipt, b'')
        self.assertTrue(self.experiment.data['rf_tx_enabled'])
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)
        self.hardware.force_idle.assert_not_called()
        self.child.kill.assert_called_once_with()
        # A retry of Start performs cleanup first and never opens another child.
        with patch.object(server.threading, 'Thread') as worker:
            with self.assertRaisesRegex(RuntimeError, 'overlapping radio access blocked'):
                self.experiment.start(loop=True)
            worker.assert_not_called()
        self.popen.assert_called_once()
        self.hardware.receive.assert_not_called()
        # The fake child may now exit; Stop retries cleanup without transmitting.
        self.reap()
        self.experiment.stop()
        self.assertIsNone(self.experiment.tx_process)
        self.assertTrue(self.output.closed)
        self.assertFalse(self.experiment.data['rf_tx_enabled'])
        self.hardware.force_idle.assert_called_once_with()
        self.popen.assert_called_once()

    def test_idle_helper_failure_keeps_original_timeout_and_unconfirmed_flag(self):
        calls = []
        def wait(timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise self.timeout(timeout)
            return self.reap()
        self.child.wait.side_effect = wait
        self.hardware.force_idle.side_effect = RuntimeError('USB mode OFF could not be verified')
        with self.assertRaisesRegex(RuntimeError, '8-second deadline.*RF shutdown unconfirmed.*USB mode OFF'):
            self.experiment._transmit_packet(8)
        self.assertIsNone(self.experiment.tx_process)
        self.assertTrue(self.output.closed)
        self.assertTrue(self.experiment.data['rf_tx_enabled'])
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)

    def test_new_log_truncates_previous_clean_receipt_before_timeout(self):
        self.log_path.parent.mkdir()
        self.log_path.write_bytes(TX_RECEIPT)
        calls = []
        def wait(timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise self.timeout(timeout)
            return self.reap()
        self.child.wait.side_effect = wait
        with self.assertRaisesRegex(RuntimeError, '8-second deadline'):
            self.experiment._transmit_packet(8)
        self.assertEqual(self.log_path.read_bytes(), PARTIAL_RECEIPT)
        self.assertNotIn(b'hackrf_close() done', self.experiment.tx_receipt)
        self.hardware.force_idle.assert_called_once_with()

    def test_stop_during_transfer_reaps_and_checks_idle_without_counting_packet(self):
        def wait(timeout):
            self.experiment.stop_event.set()
            self.experiment.tx_interrupted = True
            return self.reap()
        self.child.wait.side_effect = wait
        self.experiment._transmit_packet(8)
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)
        self.assertFalse(self.experiment.data['rf_tx_enabled'])
        self.assertIsNone(self.experiment.tx_process)
        self.hardware.force_idle.assert_called_once_with()

    def test_run_stops_loop_after_timeout_without_retransmitting(self):
        calls = []
        def wait(timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise self.timeout(timeout)
            return self.reap()
        self.child.wait.side_effect = wait
        with patch.object(self.experiment, '_receive_window') as receiver:
            self.experiment._run(server.TX_FREQUENCY, True, 8, 4)
        receiver.assert_called_once_with(server.TX_FREQUENCY, 2)
        self.assertEqual(self.experiment.data['state'], 'error')
        self.assertIn('8-second deadline', self.experiment.data['error'])
        self.assertFalse(self.experiment.data['test_loop_enabled'])
        self.assertFalse(self.experiment.data['rf_tx_enabled'])
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)
        self.popen.assert_called_once()

    def test_cached_exit_code_cannot_release_an_unsignaled_windows_process(self):
        child = Mock(spec=['returncode', 'poll', 'wait', 'terminate', 'kill', '_handle', 'args'])
        child.returncode = 1
        child.poll.return_value = 1
        child._handle = 123
        child.args = 'offline Windows TX child'
        api = Mock(WAIT_TIMEOUT=258, WAIT_OBJECT_0=0)
        api.WaitForSingleObject.return_value = 258
        self.experiment.tx_process = child
        self.experiment.data['rf_tx_enabled'] = True
        with patch.object(server, 'WINDOWS_PROCESS_API', api):
            with self.assertRaisesRegex(RuntimeError, '3-second kill waits'):
                self.experiment._finish_tx_process()
        self.assertIs(self.experiment.tx_process, child)
        self.assertTrue(self.experiment.data['rf_tx_enabled'])
        self.hardware.force_idle.assert_not_called()
        child.wait.assert_not_called()
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        api.GetExitCodeProcess.assert_not_called()
        self.assertEqual([call.args for call in api.WaitForSingleObject.call_args_list],
                         [(123, 0), (123, 3000), (123, 3000)])


class WindowsTxCompletionTests(unittest.TestCase):
    def child(self):
        return Mock(spec=['_handle', 'args', 'returncode', 'wait', 'poll'],
                    _handle=456, args='offline process', returncode=1)

    def test_signaled_handle_reads_exit_code_without_using_cached_wait(self):
        child = self.child()
        api = Mock(WAIT_TIMEOUT=258, WAIT_OBJECT_0=0)
        api.WaitForSingleObject.return_value = 0
        api.GetExitCodeProcess.return_value = 0
        with patch.object(server, 'WINDOWS_PROCESS_API', api):
            self.assertEqual(server.wait_for_tx_child(child, 8), 0)
        api.WaitForSingleObject.assert_called_once_with(456, 8000)
        api.GetExitCodeProcess.assert_called_once_with(456)
        child.wait.assert_not_called()
        self.assertEqual(child.returncode, 0)

    def test_unsignaled_handle_ignores_cached_exit_code(self):
        child = self.child()
        api = Mock(WAIT_TIMEOUT=258, WAIT_OBJECT_0=0)
        api.WaitForSingleObject.return_value = 258
        with patch.object(server, 'WINDOWS_PROCESS_API', api):
            self.assertFalse(server.tx_child_has_exited(child))
            with self.assertRaises(subprocess.TimeoutExpired) as failure:
                server.wait_for_tx_child(child, 8)
        self.assertEqual(failure.exception.timeout, 8)
        api.GetExitCodeProcess.assert_not_called()
        child.wait.assert_not_called()
        child.poll.assert_not_called()

    def test_unknown_windows_wait_result_cannot_claim_exit(self):
        child = self.child()
        api = Mock(WAIT_TIMEOUT=258, WAIT_OBJECT_0=0)
        api.WaitForSingleObject.return_value = 0xFFFFFFFF
        with patch.object(server, 'WINDOWS_PROCESS_API', api):
            with self.assertRaisesRegex(RuntimeError, 'could not confirm transmit child exit'):
                server.wait_for_tx_child(child, 3)
        api.GetExitCodeProcess.assert_not_called()
        child.wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
