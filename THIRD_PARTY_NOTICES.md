# Third-party notices

NHI Communicator's original Python, browser UI, and custom native control helper are licensed under the [MIT License](LICENSE). Included third-party components retain their respective copyright notices and licenses. Invoking a bundled command-line program does not change its license.

## HackRF host tools

`hackrf_transfer` and `hackrf_info` are from **HackRF v2024.02.1**, copyright Great Scott Gadgets and its contributors. The tools are licensed under **GPL-2.0-or-later**. Their original source headers and GNU license text must be preserved in redistributions.

- [Pinned upstream release](https://github.com/greatscottgadgets/hackrf/tree/v2024.02.1)
- [Pinned transfer source and license header](https://github.com/greatscottgadgets/hackrf/blob/v2024.02.1/host/hackrf-tools/src/hackrf_transfer.c)
- Upstream commit: `18b485e3b6d2031c15a79ba89cdb42b5fa245f24`

Windows releases include native host binaries and an accompanying source archive/build information for their distributed versions. The app itself is an independent program that launches the host tools as child processes.

## libhackrf

The bundled `hackrf-0.dll` is a locally rebuilt **libhackrf 2024.02.1** with USB serial-descriptor guards. The original library source is **BSD-3-Clause** licensed, with notices for Great Scott Gadgets, Jared Boone, and Benjamin Vernoux. Its source notices are retained. This is a project-local build, not an official Great Scott Gadgets release or endorsement.

The patch preserves signed USB descriptor return values and rejects failed, oversized, or too-short descriptors before indexing or comparing serial buffers. It changes no firmware, gain, frequency, or modulation code. Corresponding patched source, patch, provenance, and build instructions accompany the release. [Original source](https://github.com/greatscottgadgets/hackrf/blob/v2024.02.1/host/libhackrf/src/hackrf.c).

## Native runtime dependencies

| Component | License | Upstream |
| --- | --- | --- |
| libusb 1.0.30 | LGPL-2.1-or-later | [libusb](https://github.com/libusb/libusb/tree/v1.0.30) |
| winpthreads | MIT, with incorporated BSD notices | [mingw-w64 winpthreads](https://github.com/mingw-w64/mingw-w64/tree/master/mingw-w64-libraries/winpthreads) |
| Microsoft Visual C++ runtime | Microsoft's redistribution license | [Microsoft runtime redistribution documentation](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170) |

Consult the release's bundled license files and manifest for the exact dependency files and builds. Component source and build recipes are provided with the native dependency source archive. System-provided Windows components are not relicensed by this application.

## Python application dependencies and packaging

| Component | License | Upstream |
| --- | --- | --- |
| Python | PSF License, with historical notices | [Python](https://www.python.org/psf/license/) |
| NumPy | BSD-3-Clause, with dependency notices in its wheel | [NumPy](https://numpy.org/doc/stable/license.html) |
| PyInstaller bootloader | GPL-2.0-or-later with its bootloader exception | [PyInstaller license](https://pyinstaller.org/en/stable/license.html) |

The PyInstaller exception permits distributing bundled applications under their own licenses. The exception does not replace the licenses of dependencies inside the bundle. Redistribution must keep their supplied notices, including NumPy wheel dependency notices. Refer to the release manifest and bundled licenses for versions actually distributed.

## Names and references

HackRF One and Great Scott Gadgets identify the supported hardware and its upstream project. HISTORY and The Secret of Skinwalker Ranch identify the television inspiration. NHI Communicator is independent and unaffiliated. No television footage, logos, show artwork, or third-party ranch data is bundled. All trademarks belong to their respective owners.
