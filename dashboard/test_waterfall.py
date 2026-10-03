"""Offline measured-waterfall tests; no sockets, USB, or RF processes."""

import base64
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

import server
from waterfall import CEILING_DB, FFT_SIZE, FLOOR_DB, WaterfallHistory


def decode_row(row):
    return np.frombuffer(base64.b64decode(row['power_u8'], validate=True), dtype=np.uint8)


def measured_row(value=-60):
    return np.full(FFT_SIZE, value, dtype=np.float32)


def tone_iq(offset_hz=100_000, amplitude=40, count=FFT_SIZE):
    phase = 2 * np.pi * offset_hz * np.arange(count) / server.SAMPLE_RATE
    samples = np.column_stack((amplitude * np.cos(phase), amplitude * np.sin(phase)))
    return np.rint(samples).astype(np.int8).tobytes()


class WaterfallHistoryTests(unittest.TestCase):
    def setUp(self):
        self.history = WaterfallHistory()

    def snapshot(self, since=-1):
        return self.history.snapshot(since, server.TX_FREQUENCY, server.SAMPLE_RATE)

    def test_measured_row_decodes_to_8192_bins_with_declared_power_scale(self):
        measured = np.linspace(-140, 20, FFT_SIZE, dtype=np.float32)
        self.history.append(measured, '2026-10-03T12:00:00.000Z')
        snapshot = self.snapshot()
        levels = decode_row(snapshot['rows'][0])
        self.assertEqual(levels.size, 8192)
        self.assertEqual(levels[0], 0)
        self.assertEqual(levels[-1], 255)
        decoded_db = snapshot['floor_db'] + levels.astype(float) / 255 * (
            snapshot['ceiling_db'] - snapshot['floor_db'])
        quantization_half_step = (CEILING_DB - FLOOR_DB) / 255 / 2
        self.assertLessEqual(float(np.max(np.abs(decoded_db - np.clip(measured, FLOOR_DB, CEILING_DB)))),
                             quantization_half_step + 0.00001)
        self.assertEqual(snapshot['rows'][0]['time'], '2026-10-03T12:00:00.000Z')
        self.assertEqual(snapshot['rows'][0]['phase'], 'receiving')

    def test_short_or_nonfinite_input_never_adds_a_fake_row(self):
        invalid_rows = (np.zeros(256), np.zeros(4096), np.zeros(FFT_SIZE + 1),
                        np.full(FFT_SIZE, np.nan), np.full(FFT_SIZE, np.inf))
        for invalid in invalid_rows:
            with self.subTest(size=len(invalid), first=invalid[0]):
                self.history.append(invalid, 'invalid')
                self.assertEqual(self.snapshot()['rows'], [])
                self.assertEqual(self.snapshot()['latest_sequence'], 0)
        self.history.append(measured_row(), 'valid')
        self.assertTrue(self.snapshot()['rows'][0]['gap_before'])

    def test_history_and_incremental_batches_are_bounded_without_repeated_rows(self):
        row = measured_row()
        for number in range(1, 301):
            self.history.append(row, f'frame-{number}')
        latest = self.snapshot()
        self.assertEqual(len(self.history.rows), 256)
        self.assertEqual(latest['first_sequence'], 45)
        self.assertEqual(latest['latest_sequence'], 300)
        self.assertEqual([row['sequence'] for row in latest['rows']], list(range(281, 301)))

        cursor = 44
        recovered = []
        while cursor < 300:
            snapshot = self.snapshot(cursor)
            self.assertLessEqual(len(snapshot['rows']), 20)
            sequences = [row['sequence'] for row in snapshot['rows']]
            self.assertTrue(sequences)
            self.assertEqual(sequences[0], cursor + 1)
            recovered.extend(sequences)
            cursor = sequences[-1]
        self.assertEqual(recovered, list(range(45, 301)))
        self.assertEqual(self.snapshot(300)['rows'], [])
        self.assertEqual(self.snapshot(301)['rows'], [])
        self.assertEqual([row['sequence'] for row in self.snapshot(0)['rows']], list(range(45, 65)))

    def test_snapshots_cannot_mutate_stored_rows_or_history_metadata(self):
        self.history.append(measured_row(), 'original-time')
        snapshot = self.snapshot()
        original = self.snapshot()
        snapshot['epoch'] = 'changed'
        snapshot['frequency_hz'] = 0
        snapshot['rows'][0]['time'] = 'changed'
        snapshot['rows'][0]['power_u8'] = ''
        snapshot['rows'].clear()
        self.assertEqual(self.snapshot(), original)

    def test_frequency_and_sample_rate_describe_exact_fft_bin_spacing(self):
        snapshot = self.history.snapshot(-1, 1_420_000_000, 8_000_000)
        self.assertEqual(snapshot['frequency_hz'], 1_420_000_000)
        self.assertEqual(snapshot['sample_rate'], 8_000_000)
        self.assertEqual(snapshot['fft_size'], 8192)
        self.assertEqual(snapshot['bin_width_hz'], 976.5625)

    def test_reset_changes_epoch_and_clears_sequence_and_rows(self):
        self.history.append(measured_row(), 'old')
        old_epoch = self.snapshot()['epoch']
        self.history.reset()
        empty = self.snapshot()
        self.assertNotEqual(empty['epoch'], old_epoch)
        self.assertEqual(empty['latest_sequence'], 0)
        self.assertEqual(empty['first_sequence'], 0)
        self.assertEqual(empty['rows'], [])
        self.history.append(measured_row(), 'new')
        row = self.snapshot()['rows'][0]
        self.assertEqual(row['sequence'], 1)
        self.assertTrue(row['gap_before'])

    def test_gap_marker_belongs_only_to_next_measured_row(self):
        self.history.append(measured_row(), 'first')
        self.history.append(measured_row(), 'second')
        self.history.mark_gap()
        self.history.mark_gap()
        self.history.append(np.zeros(256), 'short')
        self.assertEqual(self.snapshot()['latest_sequence'], 2)
        self.history.append(measured_row(), 'after-gap')
        self.history.append(measured_row(), 'following')
        self.assertEqual([row['gap_before'] for row in self.snapshot()['rows']],
                         [True, False, True, False])
        self.assertEqual(self.snapshot(4)['rows'], [])
        self.assertFalse(self.history.gap_pending)

    def test_invalid_typed_cursors_are_rejected(self):
        for cursor in (True, False, -2, -100, 1.5, '1', None):
            with self.subTest(cursor=cursor):
                with self.assertRaisesRegex(ValueError, 'cursor'):
                    self.snapshot(cursor)


class WaterfallExperimentTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        temporary = self.patches.enter_context(tempfile.TemporaryDirectory(prefix='nhi-waterfall-test-'))
        self.patches.enter_context(patch.object(server, 'RUNTIME', Path(temporary) / 'runtime'))
        self.patches.enter_context(patch.object(server.Hardware, 'info', return_value={'offline': True}))
        self.patches.enter_context(patch.object(server.Experiment, 'log'))
        self.popen = self.patches.enter_context(patch.object(
            server.subprocess, 'Popen', side_effect=AssertionError('Offline waterfall test cannot launch RF.')))
        self.run = self.patches.enter_context(patch.object(
            server.subprocess, 'run', side_effect=AssertionError('Offline waterfall test cannot open USB.')))
        self.experiment = server.Experiment(shielded_test=False)

    def tearDown(self):
        self.popen.assert_not_called()
        self.run.assert_not_called()

    def measure(self, raw=None, timestamp='2026-10-03T12:00:00.000Z'):
        with patch.object(server, 'utc', return_value=timestamp):
            self.experiment._consume_rx(tone_iq() if raw is None else raw)
        self.experiment._update_spectrum(force=True)
        return self.experiment.waterfall()

    def test_actual_iq_tone_peaks_at_center_plus_100khz_within_one_fft_bin(self):
        snapshot = self.measure()
        row = snapshot['rows'][0]
        levels = decode_row(row)
        peak = int(np.argmax(levels))
        peak_hz = snapshot['frequency_hz'] - snapshot['sample_rate'] / 2 + peak * snapshot['bin_width_hz']
        self.assertLessEqual(abs(peak_hz - (server.TX_FREQUENCY + 100_000)), snapshot['bin_width_hz'])
        mirror = FFT_SIZE - peak
        self.assertGreater(int(levels[peak]) - int(levels[mirror]), 40)
        peak_db = FLOOR_DB + levels[peak] / 255 * (CEILING_DB - FLOOR_DB)
        self.assertAlmostEqual(float(peak_db), 20 * np.log10(40 / 128), delta=1.6)
        self.assertEqual(row['time'], '2026-10-03T12:00:00.000Z')

    def test_waterfall_uses_real_8192_fft_bins_and_reuses_single_spectrum_fft(self):
        real_fft = server.np.fft.fft
        with patch.object(server.np.fft, 'fft', wraps=real_fft) as fft:
            snapshot = self.measure()
        fft.assert_called_once()
        self.assertEqual(len(fft.call_args.args[0]), 8192)
        levels = decode_row(snapshot['rows'][0])
        self.assertEqual(len(levels), 8192)
        self.assertEqual(len(self.experiment.data['rx']['spectrum']['power_db']), 256)
        peak = int(np.argmax(levels))
        group_start = peak // 32 * 32
        self.assertGreater(len(np.unique(levels[group_start:group_start + 32])), 8,
                           'Measured bins were replaced by repeated coarse spectrum values.')

    def test_only_latest_8192_actual_samples_supply_the_waterfall(self):
        raw = tone_iq(offset_hz=-1_000_000) + tone_iq(offset_hz=100_000)
        snapshot = self.measure(raw)
        levels = decode_row(snapshot['rows'][0])
        peak_hz = snapshot['frequency_hz'] - snapshot['sample_rate'] / 2 + int(np.argmax(levels)) * snapshot['bin_width_hz']
        self.assertLessEqual(abs(peak_hz - (server.TX_FREQUENCY + 100_000)), snapshot['bin_width_hz'])
        self.assertEqual(self.experiment.data['rx']['samples_received'], 2 * FFT_SIZE)

    def test_duplicate_display_update_without_new_iq_never_appends_or_runs_fft(self):
        self.measure()
        before = self.experiment.waterfall()
        with patch.object(server.np.fft, 'fft') as fft:
            for _ in range(3):
                self.experiment._update_spectrum(force=True)
        fft.assert_not_called()
        self.assertEqual(self.experiment.waterfall(), before)

    def test_empty_receive_history_never_generates_a_row(self):
        with patch.object(server.np.fft, 'fft') as fft:
            self.experiment._update_spectrum(force=True)
        fft.assert_not_called()
        snapshot = self.experiment.waterfall()
        self.assertEqual(snapshot['rows'], [])
        self.assertEqual(snapshot['latest_sequence'], 0)

    def test_short_actual_capture_is_not_padded_into_a_waterfall_row(self):
        for sample_count in (0, 255, 256, 4096, 8191):
            with self.subTest(sample_count=sample_count):
                snapshot = self.measure(tone_iq(count=sample_count))
                self.assertEqual(snapshot['rows'], [])
                self.assertEqual(snapshot['latest_sequence'], 0)
        self.assertIsNone(self.experiment.latest_rx)

    def test_five_hz_display_limit_preserves_pending_actual_samples(self):
        with patch.object(server.time, 'monotonic', return_value=100):
            self.measure()
            self.experiment._consume_rx(tone_iq(offset_hz=200_000))
            with patch.object(server.np.fft, 'fft') as fft:
                self.experiment._update_spectrum()
            fft.assert_not_called()
            self.assertIsNotNone(self.experiment.latest_rx)
        with patch.object(server.time, 'monotonic', return_value=100.19):
            self.experiment._update_spectrum()
            self.assertEqual(self.experiment.waterfall()['latest_sequence'], 1)
        with patch.object(server.time, 'monotonic', return_value=100.21):
            self.experiment._update_spectrum()
        self.assertEqual(self.experiment.waterfall()['latest_sequence'], 2)
        self.assertIsNone(self.experiment.latest_rx)
        self.assertEqual(self.experiment.data['rx']['samples_received'], 2 * FFT_SIZE)

    def test_receive_start_resets_epoch_without_launching_radio_in_test(self):
        old = self.measure()
        frequency = 1_420_000_000
        with patch.object(self.experiment, '_stop'), patch.object(server.threading, 'Thread') as worker:
            self.experiment.start(frequency=frequency, loop=False)
        worker.return_value.start.assert_called_once_with()
        snapshot = self.experiment.waterfall()
        self.assertNotEqual(snapshot['epoch'], old['epoch'])
        self.assertEqual(snapshot['frequency_hz'], frequency)
        self.assertEqual(snapshot['sample_rate'], server.SAMPLE_RATE)
        self.assertEqual(snapshot['rows'], [])
        self.assertEqual(snapshot['latest_sequence'], 0)

    def test_new_receive_window_marks_gap_without_fabricating_samples(self):
        self.measure()
        hardware = Mock(spec=server.Hardware)
        hardware.poll.return_value = 0
        hardware.bytes_received = server.SAMPLE_RATE * 2
        hardware.completion_warning = None
        self.experiment.hardware = hardware
        with patch.object(self.experiment.stop_event, 'wait', return_value=False):
            self.experiment._receive_window_once(server.TX_FREQUENCY, 1)
        hardware.close.assert_called_once_with()
        self.assertEqual(self.experiment.waterfall()['latest_sequence'], 1)
        snapshot = self.measure(timestamp='after-window-boundary')
        self.assertTrue(snapshot['rows'][-1]['gap_before'])
        self.assertEqual(snapshot['rows'][-1]['time'], 'after-window-boundary')

    def handler_request(self, path):
        # Bypass BaseHTTPRequestHandler initialization: no sockets or listener.
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.server = SimpleNamespace(experiment=self.experiment)
        handler.send_json = Mock()
        handler.do_GET()
        handler.send_json.assert_called_once()
        args = handler.send_json.call_args.args
        return args[0], args[1] if len(args) > 1 else 200

    def test_handler_serves_default_latest_and_explicit_incremental_cursor(self):
        self.measure()
        body, status = self.handler_request('/api/waterfall')
        self.assertEqual(status, 200)
        self.assertEqual([row['sequence'] for row in body['rows']], [1])
        body, status = self.handler_request('/api/waterfall?since=1')
        self.assertEqual(status, 200)
        self.assertEqual(body['rows'], [])
        self.assertEqual(body['latest_sequence'], 1)

    def test_handler_returns_bounded_recent_and_incremental_batches(self):
        row = measured_row()
        for number in range(1, 301):
            self.experiment.waterfall_history.append(row, f'frame-{number}')
        cases = (
            ('/api/waterfall', list(range(281, 301))),
            ('/api/waterfall?since=-1', list(range(281, 301))),
            ('/api/waterfall?since=0', list(range(45, 65))),
            ('/api/waterfall?since=64', list(range(65, 85))),
            ('/api/waterfall?since=300', []),
        )
        for path, sequences in cases:
            with self.subTest(path=path):
                body, status = self.handler_request(path)
                self.assertEqual(status, 200)
                self.assertEqual(body['first_sequence'], 45)
                self.assertEqual(body['latest_sequence'], 300)
                self.assertEqual([row['sequence'] for row in body['rows']], sequences)
                self.assertLessEqual(len(body['rows']), 20)

    def test_handler_rejects_invalid_or_ambiguous_cursors(self):
        queries = ('since=-2', 'since=abc', 'since=1.5', 'since=1&since=2',
                   'since=' + '9' * 21, 'since=', 'since=1&since=')
        for query in queries:
            with self.subTest(query=query):
                body, status = self.handler_request('/api/waterfall?' + query)
                self.assertEqual(status, 400)
                self.assertIn('error', body)
        self.assertEqual(self.experiment.waterfall()['latest_sequence'], 0)


if __name__ == '__main__':
    unittest.main()
