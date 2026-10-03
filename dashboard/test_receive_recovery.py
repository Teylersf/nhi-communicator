"""Offline checks for bounded receive startup recovery and terminal timing."""

from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch

import server


class ReceiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        temporary = self.patches.enter_context(tempfile.TemporaryDirectory())
        self.patches.enter_context(patch.object(server, 'RUNTIME', Path(temporary) / 'runtime'))
        self.patches.enter_context(patch.object(
            server.subprocess, 'Popen', side_effect=AssertionError('Hardware subprocess blocked in offline test.')))
        self.patches.enter_context(patch.object(
            server.subprocess, 'run', side_effect=AssertionError('Radio control blocked in offline test.')))
        self.patches.enter_context(patch.object(server.Hardware, 'info', return_value={'offline': True}))
        self.log = self.patches.enter_context(patch.object(server.Experiment, 'log'))
        self.experiment = server.Experiment(shielded_test=True)
        self.experiment.hardware = Mock(spec=server.Hardware)
        self.experiment.stop_event = Mock(spec=threading.Event)
        self.experiment.stop_event.is_set.return_value = False
        self.experiment.stop_event.wait.return_value = False
        self.transmit = self.patches.enter_context(patch.object(self.experiment, '_transmit_packet'))

    def _open_error(self):
        return server.ReceiveOpenError(
            'Receive child exited 1: hackrf_open() failed: '
            'Access denied (insufficient permissions) (-1000)')

    def _stall_error(self, received_samples=12_000_000, expected_samples=32_000_000):
        return server.ReceiveStallError(
            received_samples * 2, expected_samples * 2,
            "Couldn't transfer any bytes for one second.\n"
            'hackrf_stop_rx() done\nhackrf_close() done\nhackrf_exit() done')

    def test_two_open_failures_then_success_retries_the_same_receive_window(self):
        with patch.object(self.experiment, '_receive_window_once',
                          side_effect=[self._open_error(), self._open_error(), None]) as receive:
            self.experiment._receive_window(server.TX_FREQUENCY, 4)
        self.assertEqual(receive.call_args_list, [call(server.TX_FREQUENCY, 4)] * 3)
        self.assertEqual(self.experiment.stop_event.wait.call_args_list, [call(0.25), call(1)])
        self.assertEqual(self.experiment.data['rx']['open_retries'], 2)
        self.transmit.assert_not_called()

    def test_third_open_failure_exhausts_retry_budget(self):
        failure = self._open_error()
        with patch.object(self.experiment, '_receive_window_once', side_effect=failure) as receive:
            with self.assertRaises(server.ReceiveOpenError) as caught:
                self.experiment._receive_window(server.TX_FREQUENCY, 4)
        self.assertIs(caught.exception, failure)
        self.assertEqual(receive.call_count, 3)
        self.assertEqual(self.experiment.stop_event.wait.call_args_list, [call(0.25), call(1)])
        self.assertEqual(self.experiment.data['rx']['open_retries'], 2)
        self.transmit.assert_not_called()

    def test_capture_processing_and_native_crash_errors_are_not_retried(self):
        failures = [
            RuntimeError('Receive child exited before the finite sample target.'),
            RuntimeError('Receive processing: partial I/Q sample.'),
            RuntimeError('Receive child exited 3221226505: native fast-fail.'),
        ]
        for failure in failures:
            with self.subTest(failure=str(failure)):
                with patch.object(self.experiment, '_receive_window_once', side_effect=failure) as receive:
                    with self.assertRaises(RuntimeError) as caught:
                        self.experiment._receive_window(server.TX_FREQUENCY, 4)
                self.assertIs(caught.exception, failure)
                receive.assert_called_once_with(server.TX_FREQUENCY, 4)
        self.experiment.stop_event.wait.assert_not_called()
        self.assertEqual(self.experiment.data['rx']['open_retries'], 0)
        self.transmit.assert_not_called()

    def test_stop_during_backoff_launches_no_more_receive_or_transmit(self):
        def stopped_during_wait(_delay):
            self.experiment.stop_event.is_set.return_value = True
            return True

        self.experiment.stop_event.wait.side_effect = stopped_during_wait
        with patch.object(self.experiment, '_receive_window_once', side_effect=self._open_error()) as receive:
            self.experiment._receive_window(server.TX_FREQUENCY, 4)
        receive.assert_called_once_with(server.TX_FREQUENCY, 4)
        self.experiment.stop_event.wait.assert_called_once_with(0.25)
        self.assertEqual(self.experiment.data['rx']['open_retries'], 1)
        self.transmit.assert_not_called()

    def test_failed_shutdown_verification_overrides_open_error_without_retry(self):
        hardware = self.experiment.hardware
        hardware.poll.side_effect = self._open_error()
        shutdown_failure = RuntimeError('Idle shutdown verification failed.')
        hardware.close.side_effect = shutdown_failure
        with self.assertRaises(RuntimeError) as caught:
            self.experiment._receive_window(server.TX_FREQUENCY, 4)
        self.assertIs(caught.exception, shutdown_failure)
        hardware.receive.assert_called_once()
        hardware.poll.assert_called_once()
        hardware.close.assert_called_once()
        self.experiment.stop_event.wait.assert_called_once_with(0.1)
        self.assertEqual(self.experiment.data['rx']['open_retries'], 0)
        self.transmit.assert_not_called()

    def test_two_stalls_then_success_record_gaps_and_retry_receive_only(self):
        observed_states = []

        def recovering_during_wait(_delay):
            status = self.experiment.status()
            observed_states.append((status['state'], status['phase']))
            return False

        self.experiment.stop_event.wait.side_effect = recovering_during_wait
        failures = [self._stall_error(12_000_000), self._stall_error(16_000_000), None]
        stamp = '2026-10-03T05:01:00.000Z'
        with patch.object(self.experiment, '_receive_window_once', side_effect=failures) as receive, \
                patch.object(server, 'utc', return_value=stamp):
            self.experiment._receive_window(server.TX_FREQUENCY, 4)
        self.assertEqual(receive.call_args_list, [call(server.TX_FREQUENCY, 4)] * 3)
        self.assertEqual(self.experiment.stop_event.wait.call_args_list, [call(0.25), call(1)])
        self.assertEqual(observed_states, [('recovering', 'recovering')] * 2)
        rx = self.experiment.data['rx']
        self.assertEqual(rx['capture_gaps'], 2)
        self.assertEqual(rx['stall_retries'], 2)
        self.assertEqual(rx['open_retries'], 0)
        self.assertEqual(rx['last_gap'], {
            'time': stamp, 'received_samples': 16_000_000, 'expected_samples': 32_000_000})
        self.transmit.assert_not_called()

    def test_three_stalls_record_three_gaps_but_only_two_retries(self):
        failure = self._stall_error()
        with patch.object(self.experiment, '_receive_window_once', side_effect=failure) as receive:
            with self.assertRaises(server.ReceiveStallError) as caught:
                self.experiment._receive_window(server.TX_FREQUENCY, 4)
        self.assertIs(caught.exception, failure)
        self.assertEqual(receive.call_count, 3)
        self.assertEqual(self.experiment.stop_event.wait.call_args_list, [call(0.25), call(1)])
        rx = self.experiment.data['rx']
        self.assertEqual(rx['capture_gaps'], 3)
        self.assertEqual(rx['stall_retries'], 2)
        self.assertEqual(rx['open_retries'], 0)
        self.assertEqual(rx['last_gap']['received_samples'], 12_000_000)
        self.assertEqual(rx['last_gap']['expected_samples'], 32_000_000)
        self.assertTrue(rx['last_gap']['time'])
        self.transmit.assert_not_called()

    def test_open_and_stall_errors_share_one_three_attempt_budget(self):
        final_stall = self._stall_error(10_000_000)
        failures = [self._open_error(), self._stall_error(), final_stall, None]
        with patch.object(self.experiment, '_receive_window_once', side_effect=failures) as receive:
            with self.assertRaises(server.ReceiveStallError) as caught:
                self.experiment._receive_window(server.TX_FREQUENCY, 4)
        self.assertIs(caught.exception, final_stall)
        self.assertEqual(receive.call_count, 3)
        self.assertEqual(self.experiment.stop_event.wait.call_args_list, [call(0.25), call(1)])
        rx = self.experiment.data['rx']
        self.assertEqual(rx['open_retries'], 1)
        self.assertEqual(rx['stall_retries'], 1)
        self.assertEqual(rx['capture_gaps'], 2)
        self.assertEqual(rx['last_gap']['received_samples'], 10_000_000)
        self.transmit.assert_not_called()

    def test_stop_during_stall_backoff_starts_no_extra_operation(self):
        def stopped_during_wait(_delay):
            self.assertEqual(self.experiment.data['state'], 'recovering')
            self.assertEqual(self.experiment.data['phase'], 'recovering')
            self.experiment.stop_event.is_set.return_value = True
            return True

        self.experiment.stop_event.wait.side_effect = stopped_during_wait
        with patch.object(self.experiment, '_receive_window_once', side_effect=self._stall_error()) as receive:
            self.experiment._receive_window(server.TX_FREQUENCY, 4)
        receive.assert_called_once_with(server.TX_FREQUENCY, 4)
        self.experiment.stop_event.wait.assert_called_once_with(0.25)
        self.assertEqual(self.experiment.data['rx']['capture_gaps'], 1)
        self.assertEqual(self.experiment.data['rx']['stall_retries'], 1)
        self.transmit.assert_not_called()

    def test_failed_shutdown_verification_overrides_stall_without_retry(self):
        hardware = self.experiment.hardware
        hardware.poll.side_effect = self._stall_error()
        shutdown_failure = RuntimeError('Idle shutdown verification failed after stalled receive.')
        hardware.close.side_effect = shutdown_failure
        with self.assertRaises(RuntimeError) as caught:
            self.experiment._receive_window(server.TX_FREQUENCY, 4)
        self.assertIs(caught.exception, shutdown_failure)
        hardware.receive.assert_called_once()
        hardware.poll.assert_called_once()
        hardware.close.assert_called_once()
        self.experiment.stop_event.wait.assert_called_once_with(0.1)
        self.assertEqual(self.experiment.data['rx']['stall_retries'], 0)
        self.assertEqual(self.experiment.data['rx']['capture_gaps'], 0)
        self.transmit.assert_not_called()

    def test_partial_packet_is_decoded_before_close_and_restart_clears_the_ring(self):
        first_packet = server.build_packet(7, 8)
        second_packet = server.build_packet(8, 8)
        first_iq = server.encode_iq(first_packet['payload_text'].encode('ascii'),
                                    sample_rate=server.DECODE_RATE)
        second_iq = server.encode_iq(second_packet['payload_text'].encode('ascii'),
                                     sample_rate=server.DECODE_RATE)
        hardware = self.experiment.hardware
        hardware.completion_warning = None
        hardware.poll.side_effect = [self._stall_error(), 0]
        receive_count = [0]
        close_count = [0]
        observed = []

        def receive(*_args):
            receive_count[0] += 1
            # A new capture must not join samples across the RX gap.
            self.assertEqual(list(self.experiment.rx_chunks), [])
            if receive_count[0] == 1:
                hardware.bytes_received = 24_000_000
                self.experiment.rx_chunks.append(first_iq)
                observed.append('first-receive')
            else:
                self.assertEqual(close_count[0], 1)
                self.assertEqual(self.experiment.data['responses'][0]['packet']['sequence'], 7)
                hardware.bytes_received = 4 * server.SAMPLE_RATE * 2
                self.experiment.rx_chunks.append(second_iq)
                observed.append('second-receive')

        def close():
            close_count[0] += 1
            sequences = [response['packet']['sequence']
                         for response in self.experiment.data['responses']]
            self.assertIn(7, sequences, 'Valid partial-window packet was lost before cleanup.')
            if close_count[0] == 2:
                self.assertIn(8, sequences)
            observed.append(f'close-{close_count[0]}')

        hardware.receive.side_effect = receive
        hardware.close.side_effect = close
        with patch.object(server, 'decode_iq', wraps=server.decode_iq) as decoder:
            self.experiment._receive_window(server.TX_FREQUENCY, 4)
        self.assertEqual(decoder.call_args_list, [
            call(first_iq, sample_rate=server.DECODE_RATE),
            call(second_iq, sample_rate=server.DECODE_RATE)])
        self.assertEqual(observed, ['first-receive', 'close-1', 'second-receive', 'close-2'])
        self.assertEqual(self.experiment.data['rx']['capture_gaps'], 1)
        self.assertEqual(self.experiment.data['rx']['stall_retries'], 1)
        self.transmit.assert_not_called()

    def test_error_elapsed_time_is_positive_and_remains_frozen(self):
        clock = [123.4]
        self.experiment.started = 100

        def shutdown():
            clock[0] = 123.9

        self.experiment.hardware.close.side_effect = shutdown
        failure = RuntimeError('Capture failed after running.')
        with patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.experiment, '_receive_window', side_effect=failure):
            self.experiment._run(server.TX_FREQUENCY, False, 16, 4)
            terminal = self.experiment.status()
            clock[0] = 800
            later = self.experiment.status()
            clock[0] = 900
            much_later = self.experiment.status()
        self.assertEqual(terminal['state'], 'error')
        self.assertEqual(terminal['error'], str(failure))
        elapsed = terminal['rx']['elapsed_seconds']
        self.assertEqual(elapsed, 23.4)
        self.assertEqual(later['rx']['elapsed_seconds'], elapsed)
        self.assertEqual(much_later['rx']['elapsed_seconds'], elapsed)
        self.experiment.hardware.close.assert_called_once()
        self.transmit.assert_not_called()

    def test_later_shutdown_failure_preserves_first_error_elapsed_time(self):
        clock = [123.4]
        self.experiment.started = 100

        def shutdown():
            clock[0] = 140
            raise RuntimeError('Idle verification failed later.')

        self.experiment.hardware.close.side_effect = shutdown
        with patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.experiment, '_receive_window',
                             side_effect=RuntimeError('Original capture failure.')):
            self.experiment._run(server.TX_FREQUENCY, False, 16, 4)
            terminal = self.experiment.status()
            clock[0] = 800
            later = self.experiment.status()
        self.assertEqual(terminal['state'], 'error')
        self.assertIn('Could not verify radio shutdown', terminal['error'])
        self.assertEqual(terminal['rx']['elapsed_seconds'], 23.4)
        self.assertEqual(later['rx']['elapsed_seconds'], 23.4)
        self.experiment.hardware.close.assert_called_once()
        self.transmit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
