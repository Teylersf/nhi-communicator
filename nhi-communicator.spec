# Run with .venv/Scripts/python.exe -m PyInstaller nhi-communicator.spec.
from pathlib import Path

root = Path(SPECPATH)
data = [(str(root / 'dashboard' / 'index.html'), 'dashboard'),
        (str(root / 'dashboard' / 'native-transfer-usage.txt'), 'dashboard')]
binary_files = [(str(root / 'dashboard' / 'radio_control_native.exe'), 'dashboard')]
binary_files += [(str(path), 'tools/hackrf/bin')
                 for path in (root / 'tools/hackrf/bin').iterdir() if path.is_file()]
a = Analysis([str(root / 'launch.py')], pathex=[str(root / 'dashboard')],
             binaries=binary_files, datas=data, hiddenimports=['server', 'radio_control'],
             hookspath=[], runtime_hooks=[], excludes=['tkinter'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='NHI Communicator',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='NHI Communicator')
