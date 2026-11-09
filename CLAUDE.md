# Agent entry point

Read `AGENTS.md` for repository instructions.

For Bicino firmware boot/UI debugging, read `examples/bicino175/README.md`.
Run `./tools/bicino.py run`, inspect `.local/bicino/latest/result.json`,
`serial.log`, `emulator.log`, and `screen.png`. Use `--firmware-dir` to snapshot
an existing 1.75-inch build. The runner never builds/flashes firmware, opens USB,
or requires a connected device. Keep physical testing separate and obey the
firmware repository's device-selection instructions before any device action.

For live development, use `./tools/bicino.py start`, open `http://127.0.0.1:17500/`,
and read `.local/bicino/live/{serial.log,emulator.log,manifest.json}`.
Use `screen` for current pixels, `input` for the documented web ABI, and `stop`
when done. `python3 tools/bicino_ble.py probe` checks virtual notifications and
reconnect; the optional macOS helper is `tools/bicino_bridge.py`. Read the README
for its physical-iPhone limits and the synthetic navigation scenario.
