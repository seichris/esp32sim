# Agent entry point

Read `AGENTS.md` for repository instructions.

For Bicino firmware boot/UI debugging, read `examples/bicino175/README.md`.
Run `./tools/bicino.py run`, inspect `.local/bicino/latest/result.json`,
`serial.log`, `emulator.log`, and `screen.png`. Use `--firmware-dir` to snapshot
an existing 1.75-inch build. The runner never builds/flashes firmware, opens USB,
or requires a connected device. Keep physical testing separate and obey the
firmware repository's device-selection instructions before any device action.
