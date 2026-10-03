# NHI Communicator v0.1.1

Fixes HackRF One detection in the native control helper. Version 0.1.0 incorrectly treated board ID 1 (Jawbreaker) as HackRF One, which rejected the actual supported device. The helper now accepts the official original HackRF One board ID 2 and R9-family board ID 4. Jawbreaker, RAD1O, and unknown board IDs are rejected with checked device cleanup.

Download and extract **nhi-communicator-v0.1.1-windows-x64.zip**, then run **Start NHI Communicator.cmd**. Python is included; a working WinUSB driver is still required separately. **Start Preview.cmd** opens the interface without accessing a radio. The app is unsigned.

Validation: 210 offline Python/native-fixture tests, a rebuilt Windows bundle, and frozen executable tests accepting mock board IDs 2 and 4 and rejecting unsupported boards. These checks exercise the actual native helper without USB or RF operations; they do not validate every hardware/driver combination or calibrated RF output.

The app remains a free local spectrum/waterfall viewer and contained prime-packet experiment inspired by HISTORY's *The Secret of Skinwalker Ranch*. The laboratory protocol does not identify a signal's origin or establish NHI contact. Transmission requires the shielded, attenuated lab profile.

Original application code is MIT licensed. Bundled dependencies retain their own licenses. **third-party-source-v0.1.1.zip** provides corresponding native source, recipes, patches, and notices; **SHA256SUMS.txt** verifies both archives. The previous v0.1.0 release remains available.
