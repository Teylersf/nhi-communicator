"""Offline receive-pipe and spectrum-work separation checks."""

from contextlib import ExitStack
import threading
import unittest
from unittest.mock import patch

import server


class ReceiveSpectrumTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(server.Hardware, 'info', return_value={'offline': True}))
        self.patches.enter_context(patch.object(server.Experiment, 'log'))
        self.patches.enter_context(patch.object(
            server.subprocess, 'Popen', side_effect=AssertionError('Offline test cannot launch a radio.')))
        self.patches.enter_context(patch.object(
            server.subprocess, 'run', side_effect=AssertionError('Offline test cannot open USB.')))
        self.experiment = server.Experiment(shielded_test=True)

    @staticmethod
    def iq(amplitude=8):
        return bytes((amplitude, 0)) * 8192

    def test_receive_pipe_counts_real_samples_without_running_fft(self):
        with patch.object(server.np.fft, 'fft', side_effect=AssertionError('FFT blocked the pipe reader.')):
            self.assertEqual(self.experiment._consume_rx(self.iq()), 0)
        received = self.experiment.data['rx']
        self.assertEqual(received['samples_received'], 8192)
        self.assertEqual(received['bytes_received'], 16384)
        self.assertIsNotNone(received['last_update_utc'])
        self.assertIsNone(received['relative_power_dbfs'])
        self.assertEqual(received['spectrum']['power_db'], [])

    def test_worker_measures_latest_real_iq_and_bounded_spectrum(self):
        self.experiment._consume_rx(self.iq())
        self.experiment._update_spectrum(force=True)
        received = self.experiment.data['rx']
        self.assertAlmostEqual(received['relative_power_dbfs'], -24.08, places=2)
        self.assertEqual(received['clipped_fraction'], 0)
        self.assertEqual(len(received['spectrum']['power_db']), 256)
        self.assertEqual(received['spectrum']['frequencies_mhz'][0], 1596)
        self.assertEqual(received['spectrum']['frequencies_mhz'][-1], 1604)

    def test_spectrum_overwrites_pending_display_work_without_losing_sample_counts(self):
        self.experiment._consume_rx(self.iq(8))
        self.experiment._consume_rx(self.iq(16))
        self.experiment._update_spectrum(force=True)
        self.assertEqual(self.experiment.data['rx']['samples_received'], 16384)
        self.assertAlmostEqual(self.experiment.data['rx']['relative_power_dbfs'], -18.06, places=2)
        self.assertIsNone(self.experiment.latest_rx)

    def test_pipe_can_accept_more_data_while_worker_fft_is_blocked(self):
        self.experiment._consume_rx(self.iq(8))
        entered = threading.Event()
        release = threading.Event()
        errors = []
        real_fft = server.np.fft.fft

        def blocked_fft(values):
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError('Offline FFT was not released.')
            return real_fft(values)

        def analyze():
            try:
                self.experiment._update_spectrum(force=True)
            except Exception as error:
                errors.append(error)

        with patch.object(server.np.fft, 'fft', side_effect=blocked_fft):
            worker = threading.Thread(target=analyze)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=1))
                self.assertEqual(self.experiment._consume_rx(self.iq(16)), 0)
                self.assertEqual(self.experiment.data['rx']['samples_received'], 16384)
            finally:
                release.set()
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        self.experiment._update_spectrum(force=True)
        self.assertAlmostEqual(self.experiment.data['rx']['relative_power_dbfs'], -18.06, places=2)

    def test_no_samples_does_not_produce_a_measurement(self):
        with patch.object(server.np.fft, 'fft') as fft:
            self.experiment._update_spectrum(force=True)
        fft.assert_not_called()
        self.assertIsNone(self.experiment.data['rx']['relative_power_dbfs'])

    def test_stop_prevents_accepting_further_samples(self):
        self.experiment.stop_event.set()
        self.assertEqual(self.experiment._consume_rx(self.iq()), -1)
        self.assertEqual(self.experiment.data['rx']['samples_received'], 0)
        self.assertIsNone(self.experiment.latest_rx)


if __name__ == '__main__':
    unittest.main()
