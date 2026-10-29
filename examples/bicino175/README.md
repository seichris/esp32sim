# Bicino 1.75 firmware development

Run an existing Bicino firmware binary, inspect its real serial diagnostics, and
capture the mounted 466x466 display. Emulator changes live in this fork; firmware
development stays in `open-bike-computer`. There is no new required CI job,
firmware rebuild, physical-device connection, or cloud dependency. Live mode uses
the emulator’s existing loopback web server; capture mode needs no server.

## Everyday use (Codex, Claude, or a terminal)

From this repository:

```sh
./tools/bicino.py run
```

The command checks the incremental emulator build, boots the cached firmware for
five emulated seconds, and returns success only after the firmware reports the
correct target, boot readiness, BLE advertising, and a nonblank 466x466 screen.
An emulator fault, guest panic, or wall timeout fails the run. The `f37a084f`
image reached ready at 1.73 emulated seconds without a card; a five-second capture
took 6.2 to 11.2 seconds of wall time in local runs on a shared, loaded host,
excluding the cached build check.
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

## Live screen, touch and logs

```sh
./tools/bicino.py start                     # once, then returns after ready + nonblank screen
./tools/bicino.py status                    # JSON: process, readiness, panic, URL, log directory
./tools/bicino.py screen                    # fresh mounted PNG, path + dimensions + hash
./tools/bicino.py input '{"t":"touch","x":135,"y":415,"down":1}'
./tools/bicino.py input '{"t":"touch","x":135,"y":415,"down":0}'
./tools/bicino.py restart                   # stop the owned session and boot cached firmware
./tools/bicino.py stop
```

Open `http://127.0.0.1:17500/` in Brave. Click/drag the real streamed display,
use the modeled BOOT button for pairing, and inspect USB or ROM logs. The page’s
Restart resets the guest; CLI `restart` starts a fresh flash image. `live` runs
in the foreground until Ctrl-C; `start` explicitly leaves one owned background
session. Duplicate starts fail. `--port` selects another loopback port.

`.local/bicino/live/` points to the session: `serial.log`, `emulator.log`,
`manifest.json`, `command.json`, `screen.png`, `inputs.jsonl`, and, after exit,
`live-result.json`. Tail the two logs from either agent or any terminal. `screen`
fetches current pixels directly from the emulator, so no browser automation or
image library is needed. The start capture is not continuously overwritten.
Inputs record observed guest time, not an exact input execution timestamp;
use `--script` for deterministic guest-time replay. Live termination is never
reported as a passing test; `run` performs bounded boot checks.

Session startup verifies cached image hashes. `--no-build` reuses the emulator
binary explicitly. `--timeout 120` can help while heavy compiler jobs saturate
the host. A timeout/panic/start failure retains logs and stops the owned worker.
No launch agent, login item, daemon installation, or ordinary firmware-command
hook is added.

## First setup / switch firmware

Install current stable Rust and Python 3.11+ once. Then point at an **existing
1.75-inch** PlatformIO build directory with `bootloader.bin`, `partitions.bin`,
`firmware.bin` and its matching **unstripped** `firmware.elf`. A build from
open-bike-computer's locked builder (`python3 tools/build_firmware.py
WAVESHARE_AMOLED_175` in its `esp32/`) also leaves `open-bike-flash-plan.json`;
setup then snapshots every other image it flashes (`boot_app0.bin`'s OTA data)
and writes it at the same offset (`0xe000`):

```sh
./tools/bicino.py setup --firmware-dir /path/to/open-bike-computer/esp32/.pio/build/WAVESHARE_AMOLED_175
./tools/bicino.py run
```

Setup snapshots the files, verifies hashes, fetches checksum-pinned
Espressif ROMs, and builds the emulator. On macOS it uses APFS copy-on-write
clones to avoid duplicating large ELF files. Existing snapshots and ROMs are
reused. It does not build or flash firmware. Source worktree HEAD is recorded as
context; `BOOT_META` in the run is the actual compiled firmware identity.

To test the next firmware build without changing repositories:

```sh
/path/to/esp32sim/tools/bicino.py run --firmware-dir /path/to/build/WAVESHARE_AMOLED_175
```

The original boot input is `seichris/open-bike-computer` commit
`03ea4cb4796dc519d70cd234080ddedfb5fbad75`, embedded version 0.3.4/build 104.
Its firmware SHA-256 is
`883a0e7ba96cf9180d3773e5a6bdf5601f9464ed63d8aab982a798abbf076d76`.
The upright welcome capture SHA-256 is
`38ce3ac0f270180313a760c2331dc83e3ab8fe26c6e7af1c85e655c41100e7cd`.
The image is not committed here and needs to be supplied on another machine.

The navigation replay first used `seichris/open-bike-computer` commit
`50aaf9b134ae1de88f1769fcb7742cbea88c7f69` from
[firmware PR #632](https://github.com/seichris/open-bike-computer/pull/632)
(firmware SHA-256 `5be95483ca387d6ac60ce5351cee90b1e818758033c6fa449dd8cfdcaab0eef5`),
which fixes the FFat render-worker stack assertion. The current input is `main`
at `f37a084facc8a286ad506bd51bfc2e6adbd19957`, version 0.3.4/build 105,
fingerprint `289B86E9`, firmware SHA-256
`ada0975cb17d1378b33861d67ddc1fef3abfca473668b49fe4463dfe53150787`, built by the
locked builder from a clean checkout. No image has been flashed as part of this workflow.

Validation and retained negative results are in
[`verification.md`](verification.md).

## microSD card and maps

The guest's storage is one of three explicit modes, recorded as `storage` in
`result.json`, the live `manifest.json` and `scenario-result.json`:

| Mode | Option | What runs |
| --- | --- | --- |
| `no-card-substitution` | default | `Storage::initSD()` returns `ESP_FAIL` at once; FFat fallback |
| `empty-socket` | `--full-storage-probe` | the real SDMMC driver meets an empty socket, then the SPI fallback and FFat |
| `sd-card` | `--sd-image card.img` | the real SDMMC driver and FatFs on a modelled card in the board's socket |

The card sits in the 1.75-inch board's one-bit socket (CLK 2, CMD 1, D0 3) behind
the ESP32-S3 SD/MMC host. The image is attached copy-on-write: guest writes stay
in memory, so the file and its hash never change. `--debug sdmmc` logs every
command into `emulator.log`, and the checks count the block reads that start in
each file's sectors.

`tools/bicino_sdcard.py` writes a reproducible FAT32 image (fixed timestamps,
long file names) and a JSON manifest of every file's SHA-256 and sectors.
`--map` installs a renderer tree under `VECTMAP/.maps/<mapId>` with a
session-less `VECTMAP/active-map.json`, which the firmware's boot probe selects:

```sh
FIXTURES=/path/to/open-bike-computer/map-platform/backend/tests/fixtures/map-compatibility-extraction-v1
python3 tools/bicino_sdcard.py build --out .local/bicino/sd/golden-gate-t3.img \
  --map $FIXTURES/golden-gate-labels-t3 --map-id golden-gate-labels-t3
./tools/bicino.py run --sd-image .local/bicino/sd/golden-gate-t3.img --seconds 8
./tools/bicino.py restart --sd-image .local/bicino/sd/golden-gate-t3.img
python3 tools/bicino_scenario.py --position 37.8199,-122.4783
```

With a card, `run` also requires the native mount, the boot selection of the
image's active map and a block read from it. The scenario additionally requires
block reads after its GPS update and the firmware's own `mapFound=1` status.
The Golden Gate fixture is frozen worker output (OpenStreetMap data,
© OpenStreetMap contributors, ODbL 1.0); keep images and fixtures out of Git.
A hand-written selection is not an app-installed durable map: no map transfer,
manifest receipt or signature check runs. The original firmware snapshot is retained
because it reproduces a real FFat/PSRAM render-worker assertion after pairing
and route delivery. A welcome screen alone does not qualify navigation.

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

## BLE bridge and navigation replay

Start a live session, then inspect the **guest-discovered** GATT table or exercise
notifications and reconnect without an iPhone:

```sh
python3 tools/bicino_ble.py discover
python3 tools/bicino_ble.py probe
```

The peer’s default remains MTU 23. The opt-in raw ATT client negotiates with the
real guest (512 in the supplied snapshot); it preserves application values,
CCC subscriptions, ATT errors and notifications. HCI fragments carry larger
PDUs. `probe` checks INFO notifications on two fresh virtual links; it does not
claim authentication or physical connectivity.

The optional macOS peripheral proxy exposes the guest’s application service as
`BikeComputer SIM` through CoreBluetooth:

```sh
python3 tools/bicino_bridge.py --build-only  # compile/sign only; no Bluetooth access
python3 tools/bicino_bridge.py              # opt-in Bluetooth peripheral; Ctrl-C to stop
```

Use the Bicino **Dev** app’s Add Device flow and compare both pairing codes
before clicking the yellow **BOOT (GPIO 0)** button below the browser screen.
This reaches the guest firmware's normal confirmation handler. The simulator
is a separate bike computer, not a mirror of a USB-connected physical board;
the phone sends navigation to its selected BLE device. A fresh CLI restart
discards the simulator's pairing, so deregister its temporary phone record
before restarting or forget that record afterward.

The Mac may ask for Bluetooth permission for **Bicino Emulator Bridge**. The
runner launches its verified app through LaunchServices from a content-addressed
user cache, with private temporary FIFOs for the JSON transport. This avoids
depending on the calling terminal's Bluetooth permission or granting access to
the firmware workspace. It installs no background service. iOS can cache the
Mac's system name instead of the requested local name; check the comparison code
on both screens before confirming the intended device. The
helper keeps credentials and all protocol handling in the real guest; it does
not manufacture auth replies or inject an owner key. Physical manufacturer
advertising data is not cloned; CoreBluetooth publishes the service UUID and a
distinct local name. Only one central is supported. Guest MTU follows the
central’s notification limit. Guest errors are returned for acknowledged writes;
unacknowledged writes retain their usual lack of application acceptance proof.
Notifications use bounded backpressure; overflow, state loss and transport
failure stop the helper. Reconnect tears down the guest when the last native
subscription ends before advertising again. CoreBluetooth has no peripheral
connection/disconnection delegate: this subscription-based lifecycle has passed
the bounded iPhone checks below, not arbitrary central behavior.
Compound/offset native writes are refused.

`bridge.log` records event metadata, not application payloads. Guest raw ATT
and firmware logs can include synthetic or real session data; keep them local.
Do not run the bridge while another task owns the iPhone. An actual iPhone run
passed discovery, matching-code registration, protected settings/GPS/navigation,
bridge and app restart recovery, and deregistration. Full map transfers remain
unproven, and that iPhone run had no card in the simulator. The firmware has a
separate pairing deadline bug, still present at `f37a084f`: confirmation after
13 modeled seconds registers ownership then disconnects before authentication.
Prompt confirmation passes; this is recorded as a firmware failure, not hidden
by the proxy. See [`verification.md`](verification.md) for the exact scope.

For a reproducible synthetic ownership/navigation scenario, install the optional
Python `cryptography` package in your own environment, restart to a fresh guest,
and run:

```sh
./tools/bicino.py restart
python3 tools/bicino_scenario.py
```

To reproduce the supplied firmware's longer-confirmation failure, restart to a
fresh guest and run `python3 tools/bicino_scenario.py --confirmation-delay 13`.
The flag uses modeled seconds; it does not bypass the physical-confirmation gate.

Two negative controls check the unauthenticated-link deadline. With
`--abandon connected` the client sends nothing after its first INFO; with
`--abandon registered` it stops right after `PAIRED`. Each requires the guest
to drop the link 11 to 14 modeled seconds after that point and records the
measured time as `abandon_seconds_to_drop`. Firmware that counts the window
from the connection fails the second control: it drops a registered client
early.

The scenario uses a public synthetic owner identity, real P-256/HKDF/HMAC and
AES-GCM, a modeled BOOT press after rendering the comparison screen, and neutral
coordinates. It waits in modeled time and verifies the physical-confirmation requirement
before claiming, then sends navigation, route, GPS and settings and rejects a
replayed GPS frame. It verifies encrypted notifications on two fresh authenticated
sessions, cycles a configured page through modeled BOOT, and checks for one boot
with no guest panic.
It retains `scenario-result.json` and pairing/navigation screenshots in the live
session directory. It deliberately fails against the original crashing firmware;
a failed result is not a bridge or physical-device success. No real credential
is imported or saved by the client. With `--sd-image` and `--position` inside
the card's map, the same replay renders map blocks from the card (see
[microSD card and maps](#microsd-card-and-maps)); the SD/MMC host is upstream
[PR #274](https://github.com/mikroverk/esp32sim/pull/274)'s model, wired into the S3.

The host attachment design is tracked in
[upstream #279](https://github.com/mikroverk/esp32sim/issues/279), extending
[EX200](../../docs/experiments.md#ex200). Apple’s
[peripheral role guide](https://developer.apple.com/library/archive/documentation/NetworkingInternetWeb/Conceptual/CoreBluetooth_concepts/PerformingCommonPeripheralRoleTasks/PerformingCommonPeripheralRoleTasks.html)
and [notification backpressure callback](https://developer.apple.com/documentation/corebluetooth/cbperipheralmanagerdelegate/peripheralmanagerisready(toupdatesubscribers:))
describe the native boundary.

## Prototype boundary

- The firmware `.bin` is unchanged. By default the runner substitutes
  `Storage::initSD()` with `ESP_FAIL` at runtime; the firmware's normal FFat
  fallback formats and runs on emulated flash. Fresh flash is used each run.
  `--full-storage-probe` and `--sd-image` run the real SDMMC driver instead.
  The SD bus is untimed (commands complete within their register write) and
  the card is an ideal one: no card-clock timing, power or insertion events.
- The CO5300 model receives real QSPI writes with the 466x466 window and 6-pixel
  column offset. Presentation compensates for Bicino's mounted orientation;
  native GRAM remains available. No screenshot is fabricated or replaced.
- CST9217 accepts one scripted contact, with its real packet format and GPIO11
  interrupt. TCA9554 reset wiring, analog touch behavior and multi-touch are not
  modeled.
- AXP2101, RTC and codec are initialization stubs. The AXP2101 PWR status
  acknowledgement is W1C so clearing IRQs cannot synthesize a button press;
  PWR input itself is not modeled. BOOT is the explicit pairing input. Battery `--%` is expected:
  there is no battery measurement. GPS and IMU are absent.
- Virtual BLE and the synthetic ATT client run guest NimBLE/authentication,
  separately from the optional native peripheral helper. Neither a virtual
  advertisement nor a compiled helper proves an iPhone connection or RF fidelity.
- Timing, power, brightness, tearing and touch hardware reliability require the
  physical device. A screenshot's existence alone is not a passing boot test.

## Project layout

Keep this fork as a sibling of `open-bike-computer` and add its folder as a
separate Codex project. `AGENTS.md` and `CLAUDE.md` point both agents here.
Use this on demand for UI/boot debugging. Keep emulator changes out of the
firmware repository; do not add emulator builds to every ordinary dev command.
