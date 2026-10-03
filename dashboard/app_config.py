"""Portable application paths; installed assets and writable state are separate."""

import os
from pathlib import Path
import re
import sys

APP_NAME = 'NHI Communicator'
VERSION = '0.1.0'
ASSET_ROOT = (Path(sys._MEIPASS) / 'dashboard' if getattr(sys, 'frozen', False)
              else Path(__file__).resolve().parent)


def tools_directory(value=None):
    chosen = value or os.environ.get('NHI_HACKRF_BIN')
    return Path(chosen).expanduser().resolve() if chosen else ASSET_ROOT.parent / 'tools' / 'hackrf' / 'bin'


def data_directory(value=None):
    if value:
        return Path(value).expanduser().resolve()
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local'))
        return base / 'NHICommunicator'
    return Path(os.environ.get('XDG_STATE_HOME') or (Path.home() / '.local' / 'state')) / 'nhi-communicator'


def device_serial(value):
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-fA-F]{32}', value) is None:
        raise ValueError('HackRF serial must contain exactly 32 hexadecimal characters.')
    return value.lower()
