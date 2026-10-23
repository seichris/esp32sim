# Bicino 1.75 development capture

Run an existing Bicino firmware binary, inspect its real serial diagnostics, and
capture the mounted 466x466 display. Emulator changes live in this fork; firmware
development stays in `open-bike-computer`. There is no new required CI job,
firmware rebuild, physical-device connection, server, or cloud dependency.

## Everyday use (Codex, Claude, or a terminal)

From this repository:

```sh
./tools/bicino.py run
```

The command checks the incremental emulator build, boots the cached firmware for
five emulated seconds, and returns success only after the firmware reports the
correct target, boot readiness, BLE advertising, and a nonblank 466x466 screen.
An emulator fault, guest panic, or wall timeout fails the run. First boot on the
validated image reached ready at about 3.8 emulated seconds; a five-second capture
took 5.58 seconds wall time in one local run, excluding the cached build check.
This is a workflow observation, not an ESP32 performance benchmark.

Read `.local/bicino/latest/result.json` first. Other files in that same directory:

| File | Purpose |
| --- | --- |
| `screen.png` | Upright screen, 466x466; view this directly |
| `serial.log` | Firmware BOOT_META/BOOT_STAGE, errors, BLE and UI logs |
| `emulator.log` | Stop reason, unsupported operations, counters and function substitutions |
| `native-gram.png` | Panel-native pixels, rotated relative to the mounted view, exported at 2x |
| `command.json`, `command.txt` | Exact replay arguments |
| `README.md` | Human-readable result |

Every run has its own directory. `latest` always points to the latest run,
including a failed one. The JSON records firmware file hashes, embedded build
identity, emulator commit/dirty state and binary hash, checks, and timing.
Artifacts and firmware stay in ignored `.local/`; neither agents nor CI need to
upload them. Raw diagnostic logs may contain build paths or synthetic identities.

## First setup / switch firmware

Install current stable Rust and Python 3.11+ once. Then point at an **existing
1.75-inch** PlatformIO build directory with `bootloader.bin`, `partitions.bin`,
`firmware.bin` and its matching **unstripped** `firmware.elf`:

```sh
./tools/bicino.py setup --firmware-dir /path/to/open-bike-computer/esp32/.pio/build/WAVESHARE_AMOLED_175
./tools/bicino.py run
```

Setup snapshots the four files, verifies hashes, fetches checksum-pinned
Espressif ROMs, and builds the emulator. On macOS it uses APFS copy-on-write
clones to avoid duplicating large ELF files. Existing snapshots and ROMs are
reused. It does not build or flash firmware. Source worktree HEAD is recorded as
context; `BOOT_META` in the run is the actual compiled firmware identity.

To test the next firmware build without changing repositories:

```sh
/path/to/esp32sim/tools/bicino.py run --firmware-dir /path/to/build/WAVESHARE_AMOLED_175
```

The validated input is `seichris/open-bike-computer` commit
`03ea4cb4796dc519d70cd234080ddedfb5fbad75`, embedded version 0.3.4/build 104.
Its firmware SHA-256 is
`883a0e7ba96cf9180d3773e5a6bdf5601f9464ed63d8aab982a798abbf076d76`.
The upright welcome capture SHA-256 is
`38ce3ac0f270180313a760c2331dc83e3ab8fe26c6e7af1c85e655c41100e7cd`.
The image is not committed here and needs to be supplied on another machine.

Prototype validation on 2026-10-23:

- 145 ESP32-S3 library tests passed, including panel corner/offset/mounting and
  CST9217 packet/IRQ tests (`cargo +stable test --release -p esp32s3 --lib`).
- Native Clippy passed for the S3 and CLI libraries with warnings denied
  (`cargo +stable clippy --release -p esp32s3 -p esp32sim --lib -- -D warnings`).
- Five-second boot capture passed; scripted touch press/release was decoded by
  the unchanged firmware at raw coordinates (415,330). That run took 5.16s wall.
- A deliberately short 0.1-second run correctly failed readiness and blank-screen
  checks, with exit code 1. Artifacts are retained for both successful and failed runs.
- Python compilation, Git whitespace checks and evidence privacy checks passed.
- The full upstream pre-push suite has not run. Installing the WASM target failed
  with `No space left on device`; broader workspace/WASM qualification remains
  necessary before publishing an upstream PR. This is a locally validated prototype.

## Debug a failure

```sh
./tools/bicino.py run --seconds 8 --script examples/bicino175/touch.txt
./tools/bicino.py run --emulator-arg=--log-periph
./tools/bicino.py run --emulator-arg=--trace-fn --emulator-arg=_ZN7Storage
./tools/bicino.py run --seconds 1 --emulator-arg=--no-jit
```

Short diagnostic runs can intentionally fail readiness checks. The default wall
timeout is 45 seconds; adjust with `--timeout`. `--no-build` skips the incremental
emulator build check when only scripts/inputs change. Script touch coordinates
match the upright screen. Read `docs/cli.md` for trace, breakpoint and memory
inspection flags. Use the saved ELF to symbolize addresses.

## Prototype boundary

- The firmware `.bin` is unchanged. The runner explicitly substitutes
  `Storage::initSD()` with `ESP_FAIL` at runtime because SDMMC is unmodeled.
  Firmware's normal FFat fallback formats and runs on emulated flash. Fresh
  flash is used each run. This proves the welcome-screen/no-card boot path,
  not SD map rendering. `--full-storage-probe` disables that substitution;
  the initial 12-second probe spent its time in real SDMMC timeout/retry paths.
- The CO5300 model receives real QSPI writes with the 466x466 window and 6-pixel
  column offset. Presentation compensates for Bicino's mounted orientation;
  native GRAM remains available. No screenshot is fabricated or replaced.
- CST9217 accepts one scripted contact, with its real packet format and GPIO21
  interrupt. TCA9554 reset wiring, analog touch behavior and multi-touch are not
  modeled.
- AXP2101, RTC and codec are initialization stubs. Battery `--%` is expected:
  there is no battery measurement. GPS and IMU are absent.
- Virtual BLE runs the guest NimBLE host and advertises; this is not an iPhone
  connection, authentication, real RF, or transfer-throughput test. The virtual
  controller's MTU/pairing limitations still apply.
- Timing, power, brightness, tearing and touch hardware reliability require the
  physical device. A screenshot's existence alone is not a passing boot test.

## Project layout

Keep this fork as a sibling of `open-bike-computer` and add its folder as a
separate Codex project. `AGENTS.md` and `CLAUDE.md` point both agents here.
Use this on demand for UI/boot debugging. Keep emulator changes out of the
firmware repository; do not add emulator builds to every ordinary dev command.
