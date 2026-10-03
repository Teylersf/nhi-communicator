"""Entry point for the source and standalone Windows NHI Communicator app."""

from pathlib import Path
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser


APP_NAME = 'NHI Communicator'
ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / 'dashboard'))


def option_value(arguments, flag, default):
    for argument in arguments:
        if argument.startswith(flag + '='):
            return argument[len(flag) + 1:]
    try:
        return arguments[arguments.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def current_service(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=1) as reply:
            return json.load(reply)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def open_when_ready(port):
    for _ in range(60):
        status = current_service(port)
        if status and status.get('application_name') == APP_NAME:
            webbrowser.open(f'http://127.0.0.1:{port}/')
            return
        time.sleep(0.25)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ['--radio-control']:
        import radio_control
        return radio_control.main(arguments[1:])
    import server
    browser_requested = '--no-browser' not in arguments
    arguments = [argument for argument in arguments if argument != '--no-browser']
    if '--help' in arguments or '-h' in arguments or '--version' in arguments:
        return server.main(arguments)
    port = int(option_value(arguments, '--port', 8787))
    existing = current_service(port)
    if existing:
        if existing.get('application_name') != APP_NAME:
            raise RuntimeError(f'Port {port} belongs to another app. Choose a different --port.')
        expected_demo = '--demo' in arguments
        if expected_demo != (existing.get('demo_mode') is True):
            raise RuntimeError('An instance with a different preview mode is already running. '
                               'Choose Quit app before switching launchers.')
        expected_lab = '--shielded-test' in arguments and not expected_demo
        if expected_lab != (existing.get('shielded_test_authorized') is True):
            raise RuntimeError('An instance with a different lab mode is already running. '
                               'Close that instance before changing launch mode.')
        if browser_requested:
            webbrowser.open(f'http://127.0.0.1:{port}/')
        return 0
    if browser_requested:
        threading.Thread(target=open_when_ready, args=(port,), daemon=True).start()
    return server.main(arguments)


if __name__ == '__main__':
    raise SystemExit(main())
