"""Offline regressions for finite RX windows and owned-child TX shutdown.

All subprocess entry points are blocked unless explicitly replaced by a fake.
These tests never load the HackRF library or access radio hardware.
"""

from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import server


class StubbornProcess:
    """Ignore terminate; reap after kill, or remain unreapable when requested."""

    def __init__(self, events, unreapable=False):
        self.events = events
        self.unreapable = unreapable
        self.killed = False
        self.returncode = None
        self.stdout = Mock()
        self.stderr = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.events.append('terminate')

    def kill(self):
        self.events.append('kill')
        self.killed = True

    def _reap(self, timeout):
        if not self.killed or self.unreapable:
            self.events.append('timeout')
            raise subprocess.TimeoutExpired('offline-fake-child', timeout)
        self.events.append('reaped')
        self.returncode = 1

    def wait(self, timeout=None):
        self._reap(timeout)
        return self.returncode

    def communicate(self, timeout=None):
        self._reap(timeout)
        return b'offline child receipt', None


class WindowShutdownTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.popen_guard = self.patches.enter_context(patch.object(
            server.subprocess, 'Popen', side_effect=AssertionError('Hardware subprocess blocked in offline test.')))
        self.patches.enter_context(patch.object(
            server.subprocess, 'run', side_effect=AssertionError('Radio control blocked in offline test.')))
        self.patches.enter_context(patch.object(server.Hardware, 'info', return_value={'offline': True}))
        self.patches.enter_context(patch.object(server.Experiment, 'log'))
        self.experiment = server.Experiment(shielded_test=True)
        self.experiment.hardware = Mock()
        self.experiment.hardware.bytes_received = 0
        # Prevent the ordinary one-second decode timer from masking an EOF bug.
        self.experiment.last_decode = time.monotonic()

    def _finished_receive(self, byte_count, final_iq=b''):
        hardware = self.experiment.hardware
        hardware.bytes_received = byte_count
        hardware.poll.return_value = 0

        def receive(*_args):
            # _receive_window clears its ring first, just as a new capture does.
            self.experiment.rx_chunks.append(final_iq)

        hardware.receive.side_effect = receive
        return hardware

    def test_finite_eof_decodes_the_fully_drained_final_packet(self):
        packet = server.build_packet(7, 8)
        final_iq = server.encode_iq(packet['payload_text'].encode('ascii'),
                                    sample_rate=server.DECODE_RATE)
        hardware = self._finished_receive(2 * server.SAMPLE_RATE * 2, final_iq)
        with patch.object(server, 'decode_iq', wraps=server.decode_iq) as decoder:
            self.experiment._receive_window(server.TX_FREQUENCY, 2)
        decoder.assert_called_with(final_iq, sample_rate=server.DECODE_RATE)
        self.assertEqual(len(self.experiment.data['responses']), 1)
        self.assertEqual(self.experiment.data['responses'][0]['packet']['sequence'], 7)
        hardware.close.assert_called_once()

    def test_finite_eof_before_sample_target_is_an_error(self):
        hardware = self._finished_receive(2 * server.SAMPLE_RATE * 2 - 2)
        with self.assertRaisesRegex(RuntimeError, 'finite sample target'):
            self.experiment._receive_window(server.TX_FREQUENCY, 2)
        hardware.close.assert_called_once()

    def test_continuous_receive_exit_without_stop_is_an_error(self):
        hardware = self._finished_receive(0)
        with self.assertRaisesRegex(RuntimeError, 'before Stop'):
            self.experiment._receive_window(server.TX_FREQUENCY)
        hardware.close.assert_called_once()

    def _owned_tx(self, unreapable=False):
        events = []
        process = StubbornProcess(events, unreapable)
        self.experiment.tx_process = process
        self.experiment.data.update(state='transmitting', phase='transmitting', rf_tx_enabled=True)

        def force_idle():
            self.assertIsNotNone(process.returncode, 'Mode OFF attempted before child was reaped.')
            self.assertIsNone(self.experiment.tx_process, 'Reaped child ownership was not released.')
            events.append('idle')

        self.experiment.hardware.force_idle.side_effect = force_idle
        return process, events

    def test_tx_cleanup_kills_and_reaps_before_idle(self):
        _process, events = self._owned_tx()
        self.experiment._stop()
        self.assertLess(events.index('terminate'), events.index('kill'))
        self.assertLess(events.index('kill'), events.index('reaped'))
        self.assertLess(events.index('reaped'), events.index('idle'))
        self.assertIsNone(self.experiment.tx_process)
        self.assertFalse(self.experiment.data['rf_tx_enabled'])

    def test_transmit_timeout_runs_guarded_cleanup(self):
        events = []
        process = StubbornProcess(events)

        def force_idle():
            self.assertIsNotNone(process.returncode)
            self.assertIsNone(self.experiment.tx_process)
            events.append('idle')

        self.experiment.hardware.force_idle.side_effect = force_idle
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(server, 'RUNTIME', Path(temporary) / 'runtime'), \
                    patch.object(server, 'encode_iq', return_value=b'\x00\x00' * 32), \
                    patch.object(server.subprocess, 'Popen', return_value=process):
                with self.assertRaisesRegex(RuntimeError, '8-second deadline'):
                    self.experiment._transmit_packet(8)
        self.assertLess(events.index('terminate'), events.index('kill'))
        self.assertLess(events.index('kill'), events.index('reaped'))
        self.assertLess(events.index('reaped'), events.index('idle'))
        self.assertEqual(self.experiment.data['tx']['packets_sent'], 0)
        self.assertIsNone(self.experiment.tx_process)
        self.assertFalse(self.experiment.data['rf_tx_enabled'])

    def test_unreaped_tx_is_retained_without_idle(self):
        process, events = self._owned_tx(unreapable=True)
        with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
            self.experiment._finish_tx_process()
        self.assertIn('kill', events)
        self.assertNotIn('reaped', events)
        self.assertIs(self.experiment.tx_process, process)
        self.assertTrue(self.experiment.data['rf_tx_enabled'])
        self.experiment.hardware.force_idle.assert_not_called()

    def test_unreaped_tx_blocks_a_new_operation(self):
        process, events = self._owned_tx(unreapable=True)
        with patch.object(server.threading, 'Thread') as worker:
            with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                self.experiment.start(loop=True)
        self.assertIs(self.experiment.tx_process, process)
        self.assertTrue(self.experiment.data['rf_tx_enabled'])
        self.assertNotIn('idle', events)
        self.experiment.hardware.force_idle.assert_not_called()
        self.experiment.hardware.receive.assert_not_called()
        self.popen_guard.assert_not_called()
        worker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
