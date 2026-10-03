"""Local HackRF receive dashboard and contained, attenuated prime-packet test."""

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import numpy as np

from prime_packets import build_packet, decode_packet
from modem import encode_iq, decode_iq
from waterfall import WaterfallHistory
from app_config import APP_NAME, VERSION, ASSET_ROOT, data_directory, tools_directory, device_serial

ROOT = ASSET_ROOT
BIN = tools_directory()
SERIAL = None
RUNTIME = data_directory()
SAMPLE_RATE = 8_000_000
DECODE_RATE = 320_000
TX_FREQUENCY = 1_600_000_000
TX_AMPLITUDE = 8
WINDOWS_PROCESS_API = getattr(subprocess, '_winapi', None)
TX_OPEN_DENIED = 'hackrf_open() failed: Access denied (insufficient permissions) (-1000)'
# Pinned to the installed 2024.02.1 CLI. A changed/partial help body is not
# sufficient evidence that the failure happened before RF configuration.
TRANSFER_USAGE_LINES = tuple(line.strip() for line in
    (ROOT / 'native-transfer-usage.txt').read_text(encoding='utf-8').splitlines())


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def native_rf_off_receipt(returncode, output, mode, interrupted=False, allow_idle_warning=False):
    """Validate checked native stop/close operations, separately from capture success."""
    if interrupted or mode not in ('rx', 'tx'):
        return False
    receipt = output.decode('utf-8', 'replace') if isinstance(output, bytes) else str(output)
    if returncode != 0 and not (returncode == 1 and mode == 'rx' and allow_idle_warning
                              and "Couldn't transfer any bytes for one second." in receipt):
        return False
    if 'failed' in receipt.lower() or 'caught signal' in receipt.lower():
        return False
    lines = [line.strip() for line in receipt.splitlines() if line.strip()]
    markers = (f'hackrf_stop_{mode}() done', 'hackrf_close() done', 'hackrf_exit() done')
    if not lines or lines[-1] != 'exit' or any(marker not in lines for marker in markers):
        return False
    positions = [lines.index(marker) for marker in markers]
    return positions == sorted(positions)


def validated_tx_settings(rf_amp, gain_db):
    if not isinstance(rf_amp, bool):
        raise ValueError('RF amplifier setting must be true or false.')
    if isinstance(gain_db, bool) or not isinstance(gain_db, int) or not 0 <= gain_db <= 47:
        raise ValueError('TX VGA gain must be an integer from 0 to 47 dB.')
    return {'rf_amp': rf_amp, 'gain_db': gain_db}


def wait_for_tx_child(process, timeout):
    """On Windows, require a signaled handle even if returncode was cached."""
    handle = getattr(process, '_handle', None)
    if WINDOWS_PROCESS_API is None or handle is None:
        return process.wait(timeout=timeout)
    result = WINDOWS_PROCESS_API.WaitForSingleObject(handle, int(timeout * 1000))
    if result == WINDOWS_PROCESS_API.WAIT_TIMEOUT:
        raise subprocess.TimeoutExpired(getattr(process, 'args', 'transmit child'), timeout)
    if result != WINDOWS_PROCESS_API.WAIT_OBJECT_0:
        raise RuntimeError(f'Windows could not confirm transmit child exit (wait result {result}).')
    process.returncode = WINDOWS_PROCESS_API.GetExitCodeProcess(handle)
    return process.returncode


def tx_child_has_exited(process):
    if WINDOWS_PROCESS_API is None or getattr(process, '_handle', None) is None:
        return process.poll() is not None
    try:
        wait_for_tx_child(process, 0)
        return True
    except subprocess.TimeoutExpired:
        return False


def unopened_tx_receipt(returncode, output, interrupted=False):
    if returncode != 1 or interrupted:
        return False
    try:
        receipt = output.decode('utf-8') if isinstance(output, bytes) else str(output)
    except UnicodeDecodeError:
        return False
    lines = receipt.splitlines()
    return (bool(lines) and lines[0] == TX_OPEN_DENIED
            and tuple(line.strip() for line in lines[1:]) == TRANSFER_USAGE_LINES)


def transmit_error_detail(returncode, output):
    receipt = output.decode('utf-8', 'replace') if isinstance(output, bytes) else str(output)
    lines = [line.strip() for line in receipt.splitlines() if line.strip()]
    for line in lines:
        if any(marker in line.lower() for marker in ('failed', 'error:', "couldn't", 'cannot ')):
            return f'Transmit child exited {returncode}: {line[:1000]}'
    detail = lines[0][:1000] if lines else 'No native diagnostic was returned.'
    return f'Transmit child exited {returncode}: {detail}'


class TransmitOpenError(RuntimeError):
    """A complete access-denied/usage receipt before any TX configuration."""

    def __init__(self, receipt):
        self.receipt = receipt.decode('utf-8') if isinstance(receipt, bytes) else str(receipt)
        super().__init__(TX_OPEN_DENIED)


class ReceiveOpenError(RuntimeError):
    """A failed USB claim before the receiver started; no IQ was accepted."""


class ReceiveStallError(RuntimeError):
    """The native idle timer ended an incomplete, cleanly closed RX window."""

    def __init__(self, received_bytes, expected_bytes, receipt):
        self.received_bytes = received_bytes
        self.expected_bytes = expected_bytes
        self.receipt = receipt
        super().__init__(f'Receive transfer stalled after {received_bytes // 2:,} of '
                         f'{expected_bytes // 2:,} requested samples.')


class Hardware:
    """Keep every native HackRF operation outside the dashboard process."""

    def __init__(self):
        self.launch_lock = threading.RLock()
        self.process = None
        self.reader = None
        self.stderr_reader = None
        self.reader_error = None
        self.bytes_received = 0
        self.expected_bytes = None
        self.completion_warning = None
        self.interrupted = False
        self.stream_started = threading.Event()
        self.startup_resolved = threading.Event()
        self.stderr_tail = deque(maxlen=24)

    @staticmethod
    def environment():
        environment = os.environ.copy()
        environment['PATH'] = str(BIN) + os.pathsep + environment.get('PATH', '')
        return environment

    def _control(self, idle=False):
        command = ([sys.executable, '--radio-control'] if getattr(sys, 'frozen', False)
                   else [sys.executable, str(ROOT / 'radio_control.py')])
        command.extend(['--tools-dir', str(BIN)])
        if SERIAL:
            command.extend(['--serial', SERIAL])
        if idle:
            command.append('--idle')
        result = subprocess.run(command, capture_output=True, timeout=8,
                                env=self.environment(),
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode != 0:
            detail = (result.stdout + result.stderr).decode('utf-8', 'replace')[-1000:]
            raise RuntimeError(f'Radio control child exited {result.returncode}: {detail}')
        return json.loads(result.stdout)

    def info(self):
        global SERIAL
        answer = self._control()
        SERIAL = device_serial(answer['serial'])
        return answer

    def receive(self, frequency, callback, stop_event, duration=None):
        command = [str(BIN / 'hackrf_transfer.exe'), '-d', SERIAL, '-r', '-',
                   '-f', str(frequency), '-s', str(SAMPLE_RATE),
                   '-a', '0', '-p', '0', '-l', '16', '-g', '16']
        if duration is not None:
            command.extend(['-n', str(int(duration * SAMPLE_RATE))])
        with self.launch_lock:
            if stop_event.is_set():
                return
            if self.process is not None:
                raise RuntimeError('A receive process is still owned; overlapping radio access blocked.')
            self.reader_error = None
            self.bytes_received = 0
            self.expected_bytes = int(duration * SAMPLE_RATE) * 2 if duration is not None else None
            self.completion_warning = None
            self.interrupted = False
            self.stream_started.clear()
            self.startup_resolved.clear()
            self.stderr_tail.clear()
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=self.environment(),
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.process = process

        def consume():
            try:
                while True:
                    raw = process.stdout.read(262144)
                    if not raw:
                        break
                    # Failed startup prints usage() to stdout in this tool version.
                    # It is not IQ. Only the stderr startup receipt opens this gate.
                    if not self.startup_resolved.wait(timeout=3):
                        raise RuntimeError('Receive child did not confirm startup.')
                    if not self.stream_started.is_set():
                        continue
                    if stop_event.is_set():
                        break
                    if len(raw) % 2:
                        raise RuntimeError('Receive child returned a partial I/Q sample.')
                    self.bytes_received += len(raw)
                    if callback(raw) != 0:
                        break
            except Exception as error:
                self.reader_error = str(error)
                if process.poll() is None:
                    self.interrupted = True
                    process.terminate()

        def consume_stderr():
            try:
                for line in iter(process.stderr.readline, b''):
                    text = line.decode('utf-8', 'replace').strip()[:1000]
                    self.stderr_tail.append(text)
                    if text == 'Stop with Ctrl-C':
                        self.stream_started.set()
                        self.startup_resolved.set()
            finally:
                self.startup_resolved.set()

        self.reader = threading.Thread(target=consume, daemon=True, name='hackrf-iq-pipe')
        self.stderr_reader = threading.Thread(target=consume_stderr, daemon=True, name='hackrf-receipt-pipe')
        self.stderr_reader.start()
        self.reader.start()

    def poll(self):
        if self.reader_error:
            raise RuntimeError(f'Receive processing: {self.reader_error}')
        if self.process is None:
            return 0
        result = self.process.poll()
        if result is not None and any(reader and reader.is_alive()
                                      for reader in (self.reader, self.stderr_reader)):
            return None
        if result not in (None, 0):
            receipt = '\n'.join(self.stderr_tail)
            open_denied = 'hackrf_open() failed: Access denied (insufficient permissions) (-1000)'
            if (result == 1 and not self.stream_started.is_set()
                    and self.bytes_received == 0 and receipt.strip() == open_denied):
                raise ReceiveOpenError(receipt)
            # 2024.02.1's timer checks byte_count==0 after the RX callback can
            # already have set do_exit on its sample limit (transfer.c:1367).
            # Accept only that specific diagnostic with the exact finite data
            # target and all normal shutdown receipts, never a partial capture.
            clean_timer_stop = (
                result == 1 and self.stream_started.is_set()
                and self.expected_bytes is not None
                and "Couldn't transfer any bytes for one second." in receipt
                and all(marker in receipt for marker in
                        ('hackrf_stop_rx() done', 'hackrf_close() done', 'hackrf_exit() done'))
                and 'failed' not in receipt.lower())
            if clean_timer_stop and self.bytes_received == self.expected_bytes:
                self.completion_warning = 'Finite RX target complete; native timer idle warning with normal shutdown.'
                return 0
            if clean_timer_stop and self.bytes_received < self.expected_bytes:
                raise ReceiveStallError(self.bytes_received, self.expected_bytes, receipt)
            raise RuntimeError(f'Receive child exited {result}: ' + receipt)
        return result

    def cancel(self):
        with self.launch_lock:
            if self.process is not None and self.process.poll() is None:
                self.interrupted = True
                self.process.terminate()

    def close(self):
        with self.launch_lock:
            process = self.process
        if process is None:
            return
        reaped = False
        readers_stopped = False
        try:
            if process.poll() is None:
                self.interrupted = True
                process.terminate()
            try:
                returncode = process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.interrupted = True
                process.kill()
                returncode = process.wait(timeout=3)
            reaped = True
            for reader in (self.reader, self.stderr_reader):
                if reader:
                    reader.join(timeout=2)
                    if reader.is_alive():
                        raise RuntimeError('Receive pipe reader has not stopped.')
            process.stdout.close()
            process.stderr.close()
            readers_stopped = True
            runtime = RUNTIME
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / 'last-receive.log').write_text('\n'.join(self.stderr_tail), encoding='utf-8')
        finally:
            if reaped:
                if readers_stopped:
                    with self.launch_lock:
                        self.process = None
                receipt = '\n'.join(self.stderr_tail)
                if not (readers_stopped and native_rf_off_receipt(
                        returncode, receipt, 'rx', interrupted=self.interrupted,
                        allow_idle_warning=self.stream_started.is_set())):
                    self._control(idle=True)

    def force_idle(self):
        # v2024.02.1 hackrf_close sends mode OFF even on a freshly opened handle.
        # hackrf_stop_tx on a fresh handle can return before sending mode OFF.
        if self.process is not None:
            self.close()
        self._control(idle=True)


class Experiment:
    def __init__(self, shielded_test=False, demo=False):
        self.lock = threading.RLock()
        self.commands = threading.Lock()
        self.quitting = False
        self.shutdown_confirmed = False
        self.hardware = Hardware()
        self.hardware.launch_lock = self.lock
        self.demo = demo
        self.shielded_test = shielded_test and not demo
        connection_error = 'Offline preview mode; no USB device is opened.' if demo else None
        info = {'name': 'HackRF One', 'serial': None, 'firmware': None}
        available = False
        if not demo:
            try:
                info = self.hardware.info()
                available = True
            except Exception as error:
                connection_error = str(error)
        self.stop_event = threading.Event()
        self.worker = None
        self.tx_process = None
        self.tx_output = None
        self.tx_output_path = None
        self.tx_cleanup_lock = threading.Lock()
        self.tx_receipt = b''
        self.tx_interrupted = False
        self.rx_chunks = deque(maxlen=160)
        self.latest_rx = None
        self.waterfall_history = WaterfallHistory()
        self.decoded_keys = set()
        self.started = time.monotonic()
        self.last_spectrum = 0
        self.last_decode = 0
        self.sequence = 1
        tx_settings = validated_tx_settings(False, 0)
        settings_path = RUNTIME / 'tx-settings.json'
        if settings_path.exists() and self.shielded_test:
            try:
                saved_settings = json.loads(settings_path.read_text(encoding='utf-8'))
                tx_settings = validated_tx_settings(saved_settings['rf_amp'], saved_settings['gain_db'])
            except (ValueError, KeyError, TypeError, OSError):
                pass  # A damaged settings file cannot prevent offline preview.
        self.data = {
            'state': 'stopped', 'phase': 'idle', 'device': info,
            'application_name': APP_NAME, 'version': VERSION, 'demo_mode': demo,
            'device_available': available, 'device_error': connection_error,
            'frequency_hz': TX_FREQUENCY, 'sample_rate': SAMPLE_RATE,
            'rf_tx_enabled': False, 'test_loop_enabled': False,
            'shielded_test_authorized': self.shielded_test,
            'test_setup': 'shielded conducted with attenuation' if self.shielded_test else 'receive only',
            'rx': {'samples_received': 0, 'bytes_received': 0, 'elapsed_seconds': 0,
                   'open_retries': 0, 'stall_retries': 0, 'capture_gaps': 0, 'last_gap': None,
                   'relative_power_dbfs': None, 'clipped_fraction': 0,
                   'last_update_utc': None, 'spectrum': {'frequencies_mhz': [], 'power_db': []}},
            'tx': {'packets_sent': 0, 'open_retries': 0, 'last_tx_utc': None, 'amplitude': TX_AMPLITUDE,
                   **tx_settings, 'last_payload_sequence': None, 'last_tx_settings': None},
            'packet': build_packet(self.sequence, 16), 'responses': [], 'logs': [], 'error': None,
            'protocol': {'modulation': '2-FSK', 'baud': 8000, 'offset_hz': 100000,
                         'deviation_hz': 20000, 'encrypted': False,
                         'interpretation': 'CRC validates data integrity, not sender identity or origin.'},
        }
        self.log('Offline preview; all hardware operations disabled.' if demo else
                 ('HackRF connected. Firmware: ' + str(info.get('firmware')) if available
                  else 'HackRF unavailable. Offline packet preview remains available: ' + connection_error))
        self.log('Shielded, attenuated conducted setup declared by user; no over-the-air operation.'
                 if self.shielded_test else 'Receive only; RF transmission disabled.')

    def log(self, message):
        with self.lock:
            entry = {'time': utc(), 'message': message}
            self.data['logs'].append(entry)
            self.data['logs'] = self.data['logs'][-80:]
            runtime = RUNTIME
            runtime.mkdir(parents=True, exist_ok=True)
            events_path = runtime / 'events.jsonl'
            if events_path.exists() and events_path.stat().st_size > 2_000_000:
                events_path.replace(runtime / 'events.previous.jsonl')
            with events_path.open('a', encoding='utf-8') as events_file:
                events_file.write(json.dumps(entry) + '\n')
        if sys.stdout.isatty() or 'error' in message.lower():
            print(message, flush=True)

    def status(self):
        with self.lock:
            result = copy.deepcopy(self.data)
        if result['state'] in ('receiving', 'transmitting', 'recovering'):
            result['rx']['elapsed_seconds'] = round(time.monotonic() - self.started, 1)
        return result

    def packet(self, count):
        with self.lock:
            self.sequence = self.sequence % 999999 + 1
            result = build_packet(self.sequence, count)
            self.data['packet'] = result
            return copy.deepcopy(result)

    def waterfall(self, since=-1):
        with self.lock:
            return self.waterfall_history.snapshot(since, self.data['frequency_hz'], SAMPLE_RATE)

    def configure_tx(self, rf_amp, gain_db):
        settings = validated_tx_settings(rf_amp, gain_db)
        if not self.shielded_test:
            raise ValueError('Transmit settings require the declared shielded conducted setup.')
        with self.commands:
            if self.quitting:
                raise ValueError('Application is shutting down; transmit settings are unavailable.')
            with self.lock:
                if (self.data['rf_tx_enabled'] and self.data['phase'] != 'transmitting'
                        or self.data['state'] == 'recovering'):
                    raise ValueError('Confirm radio shutdown or wait for receive recovery before changing TX settings.')
                runtime = RUNTIME
                runtime.mkdir(parents=True, exist_ok=True)
                settings_path = runtime / 'tx-settings.json'
                pending_path = runtime / 'tx-settings.tmp'
                pending_path.write_text(json.dumps(settings), encoding='utf-8')
                pending_path.replace(settings_path)
                self.data['tx'].update(settings)
            self.log(f'Next packet settings: RF amp {"ON" if rf_amp else "OFF"}; TX VGA {gain_db} dB.')

    def _stop(self):
        with self.lock:
            self.stop_event.set()
            process = self.tx_process
        if process is not None and not tx_child_has_exited(process):
            self.tx_interrupted = True
            try:
                process.terminate()
            except OSError:
                if not tx_child_has_exited(process):
                    raise
        self.hardware.cancel()
        if self.worker and self.worker.is_alive():
            # A TX timeout can need both bounded termination attempts and the
            # checked idle helper. Let its sole owner finish that cleanup.
            self.worker.join(timeout=16 if process is not None else 5)
            if self.worker.is_alive():
                raise RuntimeError('Radio worker has not stopped; no second operation was started.')
        self.worker = None
        self._finish_tx_process()
        with self.lock:
            needs_idle_check = self.data['rf_tx_enabled']
        if needs_idle_check:
            self.hardware.force_idle()
        with self.lock:
            if self.data['state'] in ('receiving', 'transmitting', 'recovering'):
                self.data['rx']['elapsed_seconds'] = round(time.monotonic() - self.started, 1)
            self.data.update(state='stopped', phase='idle', rf_tx_enabled=False,
                             test_loop_enabled=False)

    def stop(self):
        with self.commands:
            self._stop()
            self.log('Stopped. RF transmission and receive streaming are off.')

    def start(self, frequency=TX_FREQUENCY, loop=False, count=16, listen_seconds=4):
        if self.demo:
            raise ValueError('Offline preview mode cannot start receive or transmit operations.')
        if isinstance(frequency, bool) or not isinstance(frequency, int) or not 1_000_000 <= frequency <= 6_000_000_000:
            raise ValueError('Receive frequency must be 1 MHz through 6 GHz.')
        build_packet(1, count)
        if isinstance(listen_seconds, bool) or not isinstance(listen_seconds, (int, float)) or not 2 <= listen_seconds <= 30:
            raise ValueError('Listen interval must be 2 through 30 seconds.')
        if loop and (not self.shielded_test or frequency != TX_FREQUENCY):
            raise ValueError('Transmit loop requires the declared shielded conducted setup at 1.600 GHz.')
        with self.commands:
            if self.quitting:
                raise ValueError('Application is shutting down; new radio operations are blocked.')
            self._stop()
            if not self.data['device_available']:
                try:
                    self.data['device'] = self.hardware.info()
                    self.data.update(device_available=True, device_error=None)
                except Exception as error:
                    self.data.update(device_available=False, device_error=str(error))
                    raise RuntimeError('HackRF connection unavailable: ' + str(error)) from error
            self.stop_event = threading.Event()
            self.rx_chunks.clear()
            self.latest_rx = None
            self.started = time.monotonic()
            self.last_spectrum = 0
            self.last_decode = 0
            with self.lock:
                self.waterfall_history.reset()
                self.data['rx']['elapsed_seconds'] = 0
                self.data.update(state='receiving', phase='receiving', frequency_hz=frequency,
                                 test_loop_enabled=loop, rf_tx_enabled=False, error=None)
                self.data['rx']['samples_received'] = 0
                self.data['rx']['bytes_received'] = 0
                self.data['rx'].update(open_retries=0, stall_retries=0, capture_gaps=0, last_gap=None,
                                      relative_power_dbfs=None,
                                      clipped_fraction=0, last_update_utc=None,
                                      spectrum={'frequencies_mhz': [], 'power_db': []})
            self.worker = threading.Thread(target=self._run, args=(frequency, loop, count, listen_seconds),
                                           daemon=True, name='hackrf-experiment')
            self.worker.start()
        self.log('Starting shielded TX/listen loop.' if loop else f'Starting live reception at {frequency / 1e6:g} MHz.')

    def _consume_rx(self, raw):
        if self.stop_event.is_set():
            return -1
        try:
            iq = np.frombuffer(raw, dtype=np.int8).reshape(-1, 2)
            sampled_at = utc()
            with self.lock:
                previous = self.data['rx']['samples_received']
                self.data['rx']['samples_received'] += len(iq)
                self.data['rx']['bytes_received'] += len(raw)
                self.rx_chunks.append(iq[(-previous) % 25::25].copy().tobytes())
                # Never calculate the spectrum on the stdout drain thread.
                # A single replaceable block keeps memory and UI work bounded.
                self.latest_rx = (raw, sampled_at)
                self.data['rx']['last_update_utc'] = sampled_at
            return 0
        except Exception as error:
            with self.lock:
                self.data['error'] = f'Receive callback: {error}'
            raise RuntimeError(str(error)) from error

    def _update_spectrum(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_spectrum < 0.2:
            return
        with self.lock:
            latest = self.latest_rx
            self.latest_rx = None
            center = self.data['frequency_hz'] / 1e6
        if latest is None:
            return
        raw, sampled_at = latest
        subset = np.frombuffer(raw, dtype=np.int8).reshape(-1, 2)[-8192:].astype(np.float32)
        if len(subset) < 256:
            return
        self.last_spectrum = now
        values = (subset[:, 0] + 1j * subset[:, 1]) / 128
        rms = float(np.mean(np.abs(values) ** 2))
        power = 10 * np.log10(max(rms, 1e-12))
        window = np.hanning(len(values))
        transform = np.fft.fftshift(np.fft.fft(values * window))
        measured_db = 20 * np.log10(np.maximum(np.abs(transform) / max(window.sum(), 1), 1e-9))
        spectrum = measured_db
        groups = len(spectrum) // 256
        spectrum = spectrum[:groups * 256].reshape(256, groups).max(axis=1)
        with self.lock:
            self.waterfall_history.append(measured_db, sampled_at)
            self.data['rx'].update(relative_power_dbfs=round(float(power), 2),
                clipped_fraction=float(np.mean((subset == -128) | (subset == 127))),
                spectrum={'frequencies_mhz': np.linspace(center - 4, center + 4, 256).round(6).tolist(),
                          'power_db': spectrum.round(2).tolist()})

    def _receive_window(self, frequency, duration=None):
        for attempt in range(3):
            try:
                return self._receive_window_once(frequency, duration)
            except (ReceiveOpenError, ReceiveStallError) as error:
                stalled = isinstance(error, ReceiveStallError)
                if stalled:
                    gap = {'time': utc(), 'received_samples': error.received_bytes // 2,
                           'expected_samples': error.expected_bytes // 2}
                    with self.lock:
                        self.data['rx']['capture_gaps'] += 1
                        self.data['rx']['last_gap'] = gap
                    runtime = RUNTIME
                    runtime.mkdir(parents=True, exist_ok=True)
                    (runtime / 'last-receive-stall.json').write_text(
                        json.dumps({**gap, 'receipt': error.receipt}, indent=2), encoding='utf-8')
                    self.log(f'{error} Partial capture retained; receive interruption recorded.')
                if self.stop_event.is_set():
                    return
                if attempt == 2:
                    raise
                # The failed child and both readers were reaped by the window's
                # finally block. Retry RX only; never repeat the prior TX burst.
                with self.lock:
                    self.data['rx']['stall_retries' if stalled else 'open_retries'] += 1
                    self.data.update(state='recovering', phase='recovering', rf_tx_enabled=False)
                self.log(f'{error}. Retrying receive only ({attempt + 2}/3).')
                if self.stop_event.wait((0.25, 1.0)[attempt]):
                    return

    def _receive_window_once(self, frequency, duration=None):
        with self.lock:
            self.waterfall_history.mark_gap()
            self.data.update(state='receiving', phase='receiving', rf_tx_enabled=False)
            self.rx_chunks.clear()
            self.latest_rx = None
        self.hardware.receive(frequency, self._consume_rx, self.stop_event, duration)
        begin = time.monotonic()
        try:
            while not self.stop_event.wait(0.1):
                result = self.hardware.poll()
                if result is not None:
                    if duration is None:
                        raise RuntimeError('Receive child exited before Stop was requested.')
                    if self.hardware.bytes_received < int(duration * SAMPLE_RATE) * 2:
                        raise RuntimeError('Receive child exited before the finite sample target.')
                    if self.hardware.completion_warning:
                        self.log(self.hardware.completion_warning)
                    self._update_spectrum(force=True)
                    self._decode_ring()
                    break
                now = time.monotonic()
                self._update_spectrum()
                if now - self.last_decode >= 1:
                    self.last_decode = now
                    self._decode_ring()
                if duration is not None and now - begin > duration + 8:
                    raise RuntimeError('Finite receive child exceeded its deadline.')
        except ReceiveStallError:
            # poll() waits for pipe EOF. Decode the final genuine partial data
            # before cleanup/restart, and never join buffers across the gap.
            self._update_spectrum(force=True)
            self._decode_ring()
            raise
        finally:
            self.hardware.close()

    def _decode_ring(self):
        with self.lock:
            raw = b''.join(self.rx_chunks)
        for payload in decode_iq(raw, sample_rate=DECODE_RATE):
            try:
                decoded = decode_packet(payload)
            except ValueError:
                continue
            key = decoded['payload_hex']
            if key in self.decoded_keys:
                continue
            self.decoded_keys.add(key)
            if len(self.decoded_keys) > 1000:
                self.decoded_keys.clear()
            with self.lock:
                same = decoded['sequence'] == self.data['tx']['last_payload_sequence']
                result = {'time': utc(), 'packet': decoded,
                          'classification': 'matches latest outbound packet; echo/replay possible' if same
                          else 'valid laboratory packet; source not authenticated'}
                self.data['responses'].append(result)
                self.data['responses'] = self.data['responses'][-30:]
            self.log(f'CRC-valid received laboratory packet, sequence {decoded["sequence"]}. Source not authenticated.')

    def _finish_tx_process(self):
        with self.tx_cleanup_lock:
            with self.lock:
                process = self.tx_process
            if process is None:
                return False
            if not tx_child_has_exited(process):
                self.tx_interrupted = True
                try:
                    process.terminate()
                except OSError:
                    if not tx_child_has_exited(process):
                        raise
                try:
                    wait_for_tx_child(process, 3)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        if not tx_child_has_exited(process):
                            raise
                    try:
                        wait_for_tx_child(process, 3)
                    except subprocess.TimeoutExpired as error:
                        raise RuntimeError('Transmit child has not exited after 3-second terminate '
                                           'and 3-second kill waits; '
                                           'RF shutdown unconfirmed, overlapping radio access blocked.') from error
            if not tx_child_has_exited(process):
                raise RuntimeError('Transmit child has not exited; overlapping radio access blocked.')
            # File output avoids Windows communicate() reader threads waiting
            # indefinitely for pipe EOF even after the child was terminated.
            receipt_available = True
            try:
                if self.tx_output is not None:
                    self.tx_output.close()
                if self.tx_output_path is not None:
                    self.tx_receipt = self.tx_output_path.read_bytes()
            except OSError as error:
                receipt_available = False
                self.tx_receipt = b''
                self.log(f'Transmit shutdown receipt could not be read: {error}')
            finally:
                with self.lock:
                    if self.tx_process is process:
                        self.tx_process = None
                        self.tx_output = None
                        self.tx_output_path = None
            return receipt_available and native_rf_off_receipt(
                process.poll(), self.tx_receipt, 'tx', interrupted=self.tx_interrupted)

    def _transmit_packet(self, count):
        packet = self.packet(count)
        waveform = encode_iq(packet['payload_text'].encode('ascii'), amplitude=TX_AMPLITUDE)
        runtime = RUNTIME
        runtime.mkdir(parents=True, exist_ok=True)
        waveform_path = runtime / 'current-packet.iq'
        waveform_path.write_bytes(waveform)
        command = [str(BIN / 'hackrf_transfer.exe'), '-d', SERIAL, '-t', str(waveform_path),
                   '-f', str(TX_FREQUENCY), '-s', str(SAMPLE_RATE), '-n', str(len(waveform) // 2),
                   '-a', '0', '-p', '0', '-x', '0']
        with self.lock:
            if self.stop_event.is_set():
                return
            tx_settings = validated_tx_settings(self.data['tx']['rf_amp'], self.data['tx']['gain_db'])
        command[command.index('-a') + 1] = str(int(tx_settings['rf_amp']))
        command[command.index('-x') + 1] = str(tx_settings['gain_db'])
        for attempt in range(3):
            try:
                return self._transmit_prepared_packet(packet, count, command, tx_settings, runtime)
            except TransmitOpenError as error:
                # This exception is delivered only after the failed child was
                # reaped and the independent mode-OFF helper succeeded.
                (runtime / 'last-transmit-open-failure.json').write_text(json.dumps({
                    'time': utc(), 'packet_sequence': packet['sequence'], 'attempt': attempt + 1,
                    'receipt': error.receipt}, indent=2), encoding='utf-8')
                if self.stop_event.is_set():
                    return
                if attempt == 2:
                    raise
                with self.lock:
                    self.data['tx']['open_retries'] += 1
                    self.data.update(state='recovering', phase='tx_open_retry',
                                     rf_tx_enabled=False, test_loop_enabled=True)
                self.log(f'{error}. Retrying USB open only ({attempt + 2}/3); '
                         f'packet {packet["sequence"]} has not started.')
                if self.stop_event.wait((0.25, 1.0)[attempt]):
                    return

    def _transmit_prepared_packet(self, packet, count, command, tx_settings, runtime):
        environment = os.environ.copy()
        environment['PATH'] = str(BIN) + os.pathsep + environment.get('PATH', '')
        launched = False
        transfer_error = None
        try:
            with self.lock:
                if self.stop_event.is_set():
                    return
                self.tx_receipt = b''
                self.tx_interrupted = False
                self.data['tx']['last_tx_settings'] = tx_settings
                self.tx_output_path = runtime / 'last-transmit.log'
                self.tx_output = self.tx_output_path.open('wb')
                try:
                    self.tx_process = subprocess.Popen(command, stdout=self.tx_output,
                        stderr=subprocess.STDOUT, env=environment,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                except Exception:
                    self.tx_output.close()
                    self.tx_output = None
                    self.tx_output_path = None
                    raise
                process = self.tx_process
                launched = True
                self.data.update(state='transmitting', phase='transmitting', rf_tx_enabled=True)
            self.log(f'Transmitting contained 2-FSK packet {packet["sequence"]}: {count} primes, no encryption.')
            try:
                wait_for_tx_child(process, 8)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError('Transmit transfer exceeded its 8-second deadline. '
                                   'Packet completion is unconfirmed; no automatic retransmission.') from error
            output = self.tx_output_path.read_bytes()
            self.tx_receipt = output
            if self.stop_event.is_set():
                return
            if process.returncode != 0:
                if unopened_tx_receipt(process.returncode, output, interrupted=self.tx_interrupted):
                    raise TransmitOpenError(output)
                raise RuntimeError(transmit_error_detail(process.returncode, output))
            if not native_rf_off_receipt(process.returncode, output, 'tx',
                                        interrupted=self.tx_interrupted):
                raise RuntimeError('Transmit tool did not confirm a checked native shutdown: '
                                   + output.decode('utf-8', 'replace')[-1000:])
            with self.lock:
                self.data['tx']['packets_sent'] += 1
                self.data['tx']['last_tx_utc'] = utc()
                self.data['tx']['last_payload_sequence'] = packet['sequence']
            self.log(f'Packet {packet["sequence"]} transfer completed; switching to listen.')
        except Exception as error:
            transfer_error = error
            raise
        finally:
            if launched:
                try:
                    native_confirmed = self._finish_tx_process()
                    if not native_confirmed:
                        self.hardware.force_idle()
                except Exception as cleanup_error:
                    if transfer_error is not None:
                        raise RuntimeError(f'{transfer_error}; RF shutdown unconfirmed: '
                                           f'{cleanup_error}') from transfer_error
                    raise RuntimeError(f'RF shutdown unconfirmed: {cleanup_error}') from cleanup_error
                with self.lock:
                    self.data['rf_tx_enabled'] = False

    def _run(self, frequency, loop, count, listen_seconds):
        try:
            if loop:
                self._receive_window(frequency, 2)
                while not self.stop_event.is_set():
                    self._transmit_packet(count)
                    if not self.stop_event.is_set():
                        self._receive_window(frequency, listen_seconds)
            else:
                self._receive_window(frequency)
        except Exception as error:
            self.log(f'Radio error: {error}')
            with self.lock:
                if self.data['state'] != 'error':
                    self.data['rx']['elapsed_seconds'] = round(time.monotonic() - self.started, 1)
                self.data.update(state='error', error=str(error), phase='idle',
                                 test_loop_enabled=False)
        finally:
            try:
                self.hardware.close()
            except Exception as error:
                self.log(f'Could not verify radio shutdown: {error}')
                with self.lock:
                    if self.data['state'] != 'error':
                        self.data['rx']['elapsed_seconds'] = round(time.monotonic() - self.started, 1)
                    self.data.update(state='error', error=f'Could not verify radio shutdown: {error}',
                                     test_loop_enabled=False)
            with self.lock:
                if self.stop_event.is_set() and self.data['state'] != 'error':
                    self.data['rx']['elapsed_seconds'] = round(time.monotonic() - self.started, 1)
                    self.data.update(state='stopped', phase='idle', test_loop_enabled=False)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, data, status=200):
        encoded = json.dumps(data, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        target = urlsplit(self.path)
        if target.path == '/api/waterfall':
            try:
                values = parse_qs(target.query, keep_blank_values=True).get('since', ['-1'])
                if len(values) != 1 or len(values[0]) > 20:
                    raise ValueError('Invalid waterfall cursor.')
                return self.send_json(self.server.experiment.waterfall(int(values[0])))
            except (ValueError, TypeError) as error:
                return self.send_json({'error': str(error)}, 400)
        if self.path == '/api/status':
            return self.send_json(self.server.experiment.status())
        if self.path in ('/', '/index.html'):
            payload = (ROOT / 'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return
        self.send_json({'error': 'Not found'}, 404)

    def do_POST(self):
        expected_hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if self.headers.get('Host') not in expected_hosts:
            return self.send_json({'error': 'Local dashboard access only'}, 403)
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + host for host in expected_hosts}:
            return self.send_json({'error': 'Cross-origin radio control is disabled'}, 403)
        quit_previous = None
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0 or length > 4096:
                raise ValueError('Invalid request length')
            body = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body, dict):
                raise ValueError('Request must be an object')
            experiment = self.server.experiment
            if self.path == '/api/quit':
                quit_previous = experiment.quitting
                experiment.quitting = True
                experiment.stop()
                snapshot = experiment.status()
                if (snapshot.get('state') != 'stopped' or snapshot.get('rf_tx_enabled') is not False
                        or snapshot.get('test_loop_enabled') is not False):
                    raise RuntimeError('Radio shutdown is unconfirmed; application remains open.')
                experiment.shutdown_confirmed = True
                self.send_json({**snapshot, 'closed': True})
                # HTTPServer.shutdown must run outside serve_forever's thread.
                # Reply only after checked RF cleanup; never kill pending USB IO.
                threading.Thread(target=self.server.shutdown, daemon=True,
                                 name='nhi-dashboard-shutdown').start()
                return
            if self.path == '/api/start':
                experiment.start(frequency=body.get('frequency_hz', TX_FREQUENCY))
            elif self.path in ('/api/stop', '/api/loop/stop'):
                experiment.stop()
            elif self.path == '/api/loop/start':
                if body.get('shielded_setup_confirmed') is not True:
                    raise ValueError('Declare the shielded, attenuated conducted setup before starting TX.')
                experiment.start(loop=True, count=body.get('prime_count', 16),
                                 listen_seconds=body.get('listen_seconds', 4))
            elif self.path == '/api/packet':
                experiment.packet(body.get('prime_count', 16))
            elif self.path == '/api/tx/config':
                experiment.configure_tx(body.get('rf_amp'), body.get('gain_db'))
            else:
                return self.send_json({'error': 'Not found'}, 404)
            self.send_json(experiment.status())
        except (ValueError, TypeError) as error:
            if quit_previous is not None and self.server.experiment.shutdown_confirmed is not True:
                self.server.experiment.quitting = quit_previous
            self.send_json({'error': str(error)}, 400)
        except Exception as error:
            if quit_previous is not None and self.server.experiment.shutdown_confirmed is not True:
                self.server.experiment.quitting = quit_previous
            self.send_json({'error': str(error)}, 500)


def main(argv=None):
    global BIN, SERIAL, RUNTIME
    parser = argparse.ArgumentParser(description='NHI Communicator: local HackRF receive and laboratory prime packets.')
    parser.add_argument('--version', action='version', version=APP_NAME + ' ' + VERSION)
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--shielded-test', action='store_true',
                        help='Enable contained TX only for the user-declared shielded, attenuated setup.')
    parser.add_argument('--receive', action='store_true')
    parser.add_argument('--frequency', type=int, default=TX_FREQUENCY, help='Receive center frequency in Hz (default: 1600000000).')
    parser.add_argument('--tools-dir', help='Directory containing HackRF host tools and libraries; alternatively NHI_HACKRF_BIN.')
    parser.add_argument('--serial', type=device_serial, help='Select one 32-digit serial; automatic selection requires exactly one HackRF.')
    parser.add_argument('--data-dir', help='Writable local settings/log directory (default: user application data).')
    parser.add_argument('--demo', action='store_true', help='Offline packet/UI preview; never probe or open radio hardware.')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1 through 65535')
    if not 1_000_000 <= args.frequency <= 6_000_000_000:
        parser.error('receive frequency must be 1 MHz through 6 GHz')
    if args.demo and args.receive:
        parser.error('--demo cannot be combined with --receive')
    BIN, SERIAL, RUNTIME = tools_directory(args.tools_dir), args.serial, data_directory(args.data_dir)
    experiment = Experiment(shielded_test=args.shielded_test, demo=args.demo)
    experiment.data['frequency_hz'] = args.frequency
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.experiment = experiment
    if args.receive:
        try:
            experiment.start(frequency=args.frequency)
        except Exception as error:
            experiment.data.update(state='error', error=str(error))
    print(f'Dashboard: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        experiment.stop()
        server.server_close()


if __name__ == '__main__':
    main()
