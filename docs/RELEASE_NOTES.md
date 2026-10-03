# NHI Communicator v0.1.0

A free local HackRF One application for prime-number packet experiments and live spectrum viewing. The Windows x64 download includes the application runtime and radio host tools; Python is not required.

- 8,192-bin receive waterfall with display zoom, color levels, pause, and expansion.
- Unencrypted ASCII prime packets, sequence numbers, CRC32 checks, and a simple 8 kbit/s binary FSK modem.
- A contained transmit/listen loop for shielded, attenuated laboratory setups, with amplifier and TX gain controls.
- Offline preview that never opens a radio, plus checked Stop and Quit app behavior.
- Skinwalker Ranch show context and a full explanation of the protocol and measurements.

Extract the entire Windows ZIP and run **Start NHI Communicator.cmd**. **Start Preview.cmd** opens the interface without a radio. HackRF use requires a working WinUSB driver installed separately. The app does not change firmware or automatically start RF operations.

The app is independently inspired by HISTORY's *The Secret of Skinwalker Ranch*. It is unaffiliated, uses its own laboratory protocol, and does not identify a signal's origin or establish NHI contact. The 1.600 GHz lab transmit profile requires a shielded conducted setup with attenuation.

Validation: 209 offline Python/native-fixture tests, 32 offline UI/waterfall checks, and a standalone Windows preview smoke test covering packet generation and service shutdown. These checks do not validate calibrated RF output or every hardware/driver combination. The Windows app is unsigned.

Original application code is MIT licensed. Bundled dependencies retain their own licenses. Download **third-party-source-v0.1.0.zip** for corresponding native source, recipes, patches, and notices, and **SHA256SUMS.txt** to verify both archives.
