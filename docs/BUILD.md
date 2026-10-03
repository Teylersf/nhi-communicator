# Build NHI Communicator

The Windows release is a portable, one-folder application. Extract the complete ZIP and run a launcher. It contains Python and NumPy, the browser UI, the HackRF command-line tools, and the checked native control helper. No driver or firmware is installed by the app or its build scripts.

## Requirements

- Windows 10/11 x64.
- Python 3.13 x64 for the documented standalone build.
- Visual Studio 2022 C++ Build Tools with the x64 compiler and Windows SDK.
- Internet access to the pinned PyPI, conda-forge, and official upstream release downloads.

From the repository root, run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r build-requirements.txt
.\Build-Windows.ps1
```

The output is `release/nhi-communicator-v0.1.1-windows-x64.zip`, a corresponding `third-party-source-v0.1.1.zip`, and `SHA256SUMS.txt`. The scripts verify pinned SHA-256 hashes before using downloaded native dependencies. The build recompiles the custom native helper and the guarded libhackrf DLL, builds an offline mock DLL for native tests, runs the test suite, then packages the app with PyInstaller. It does not open a HackRF or run an RF test.

The app's executable uses the console bootloader so checked child-process JSON receipts work. The `.cmd` launchers start it in the background and open the local browser UI. **Quit app** confirms radio shutdown and closes the service. Running the executable directly also works, with a console available for diagnostics.

## Native components and corresponding source

`scripts/prepare_native_tools.py` downloads pinned conda-forge packages. Their original build recipes and patches are preserved under `vendor/recipes`, their license texts under `licenses`, and download hashes/URLs under `vendor/native-provenance.json`. The transfer/info binaries are unchanged upstream-package files. Only the needed host programs and runtime DLLs are bundled; firmware-writing tools are excluded.

The source archive accompanying each release includes the complete HackRF 2024.02.1 and libusb 1.0.30 upstream source archives, build recipes, license texts, and the project's patched source. The archive URLs and hashes are pinned in the preparation script. Redistribution must preserve the third-party licenses and corresponding source; see [Third-party notices](../THIRD_PARTY_NOTICES.md).

The local libhackrf source is in `vendor/hackrf-guard/src`. It comes from upstream HackRF commit `18b485e3b6d2031c15a79ba89cdb42b5fa245f24`. `serial-descriptor-guards.patch` records the change: preserve signed USB serial-descriptor errors and reject negative, oversized, and too-short descriptors before indexing or comparing buffers. The patch does not change firmware, RF settings, or modulation. `scripts/Build-HackRFLibrary.ps1` builds it with the downloaded libusb and winpthreads SDK. Compiler timestamps and compiler versions can change binary hashes; this is a documented source build, not a bit-for-bit reproducibility claim.

The application's original native helper is `dashboard/radio_control_native.c`. It dynamically loads the selected BSD-licensed libhackrf, enumerates devices, requires an unambiguous HackRF One, and checks open/close/exit receipts. It accepts official board IDs 2 (original HackRF One) and 4 (R9 family); Jawbreaker, RAD1O, and unknown board IDs are rejected. Build it independently with:

```powershell
.\dashboard\Build-RadioControl.ps1
.\dashboard\Build-RadioControl.ps1 -TestFixture
```

The fixture is an offline DLL and is never included in the downloadable app.

## Offline verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s dashboard -p 'test_*.py'
.\.venv\Scripts\python.exe launch.py --demo --no-browser --port 8790 --data-dir .\build\preview-state
```

Open `http://127.0.0.1:8790` to check the UI. Demo mode does not probe USB, fabricate a spectrum, or transmit. Generate an offline prime packet, inspect the explanatory panel, then choose **Quit app**. A disconnected normal startup also keeps packet preview available; radio actions require a real, selected device.

## GitHub checks

The CI workflow builds the helper and offline fixture on a Windows runner and runs the offline suite. CI does not have a radio, exercise USB, or validate RF output. Hardware-specific firmware, drivers, USB faults, RF shielding, attenuation, and calibrated power remain outside those checks.
