# Bicino workflow verification — 2026-10-24, updated 2026-10-27

The initial October 24 checks below use the native emulator and a synthetic
client. The October 26–27 follow-up also uses a real USB-connected iPhone and
the Mac's CoreBluetooth radio. No board was flashed. A separate existing-image
physical-board check is scoped explicitly below.

## iPhone and physical-board follow-up

Emulator Rust remains `2c055eb7bd3e55bfce44a32a4361ba9feccca7d5`; firmware
remains `50aaf9b1` with the SHA-256 below. The installed Bicino Dev app reports
1.11 (30). The native helper required two host fixes: launch its signed bundle
through LaunchServices from a verified user cache, using private temporary
FIFOs; drain complete JSON lines from unbuffered descriptor reads instead of
mixing `select` with buffered `readline`. Coalesced and partial events now drain
without waiting for another descriptor-readiness event; oversized frames fail.
No guest authentication handler or credential was substituted.

The real iPhone passed service discovery, matching-code registration with
explicit modeled BOOT input, protected GPS/settings/navigation, encrypted
notifications, reconnect after bridge restart, and reconnect after cold app
relaunch. A selected route delivered 26 points. The phone and guest display
showed matching 21 m guidance; the guest still displayed **No microSD card**.
A mid-navigation checkpoint records `authOK=3 nav=4 route=4 gps=1715 settings=20`,
3,455 native notifications and no guest panic. Before intentional deregistration,
the counters reached `authOK=4 nav=6 route=6 gps=2010 settings=26 rejectAuth=0`.
Deregistration restarted the guest unclaimed and removed the temporary phone
record. The phone briefly reported an unconfirmed disconnect before completing
that verification. Navigation was stopped; no workout was started.

Retained failures: the original direct-child launch reported Bluetooth
unauthorized; launching from a protected workspace path failed at spawn.
Two slow-confirmation iPhone attempts failed before the final framing fix.
A separate synthetic run isolates a firmware deadline failure: with
`--confirmation-delay 13`, the physical-confirmation gate passes, ownership is
registered, then the guest logs **Disconnecting unauthenticated client after
timeout** before session authentication. The default one-second control still
passes all 22 checks. At `50aaf9b1`, `Event::Paired` clears the pairing-active
flag, reducing the allowance from 120 to 12 seconds while the deadline remains
anchored to the original connection. This is separate from the FFat fix;
neither proxy timing nor a firmware fix is claimed to resolve it here.

The physical 1.75-inch board separately reconnected to the same iPhone. Its
authenticated app status reports `08c05ea6ff46`, ordinary target
`WAVESHARE_AMOLED_175`, and storage **sdmmc**. The operator confirmed matching
35 m guidance on its real screen. This is an existing-image BLE/navigation
smoke test, not physical qualification of PR #632 or FFat/radio memory margins.
No serial port was opened, board reset requested, firmware flashed or map
replaced. Its firmware reports remote debugging unsupported. The diagnostics
broker has no fresh phone observation, so old retained data was not used as
evidence of this test.

Python compilation, native helper build/signature and final launch/shutdown
checks pass. An initial launcher-only smoke omitted the configure message and
timed out waiting for Bluetooth; the corrected configured smoke passes.
Both Clippy commands and the full 1,047-test workspace suite pass again on
stable 1.99.1. Rust and all goldens remain unchanged. Commands, hashes, scoped
outcomes and privacy omissions are in the
[iPhone follow-up receipt](../../docs/evidence/bicino-development/iphone-followup.json).

## Inputs and reproducibility

- Emulator Rust: `a09103c42edf786b40bc05f42c9f99024ea38f2e`;
  final scenario: `06cc54ed26ed65a242128e8759e7248b3295cf47`.
- Original firmware: `seichris/open-bike-computer` at `03ea4cb4`, SHA-256
  `883a0e7ba96cf9180d3773e5a6bdf5601f9464ed63d8aab982a798abbf076d76`.
- Fixed firmware: `seichris/open-bike-computer` at
  `50aaf9b134ae1de88f1769fcb7742cbea88c7f69`, ordinary 1.75-inch profile,
  version 0.3.4/build 104, fingerprint `6E0C0E72`, firmware SHA-256
  `5be95483ca387d6ac60ce5351cee90b1e818758033c6fa449dd8cfdcaab0eef5`.
  The running BOOT_META matches that source/profile. Its locked build passed;
  40 clean-source firmware host checks passed. See
  [firmware PR #632](https://github.com/seichris/open-bike-computer/pull/632).
- Import an existing build with `tools/bicino.py setup --firmware-dir ...`;
  run `run`, `start`, `screen`, `tools/bicino_ble.py probe`, and
  `tools/bicino_scenario.py` as documented in the README. The scenario needs the
  optional Python `cryptography` package and a fresh unclaimed guest.

The receipt records all four firmware input hashes, artifact hashes and lengths,
checks and limits: [navigation replay](../../docs/evidence/bicino-development/navigation-replay.json).
Firmware, raw logs and screenshots remain in ignored local storage. They are
not redistributed here. No device identity, owner credential, actual location
trace, process inventory or host identity is included in the curated receipts.

## Proven behavior

- A five-second bounded boot checks the correct target, readiness, advertising,
  no panic, bounded stop and a nonblank upright 466x466 screen. A 0.1-second
  negative capture fails readiness and returns exit 1.
- Brave shows the native WebSocket framebuffer and ongoing serial logs. A click
  reaches the real CST9217 decoder as a press/release. The browser Restart button
  produces a real guest POWERON reset. CLI screenshots read current RGB565 pixels.
- Start/status/input/screen/stop and immediate restart work. Duplicate starts,
  invalid BLE commands and startup timeouts fail explicitly; logs remain available.
- The ATT probe discovers the real guest GATT table, negotiates MTU 512 and
  receives INFO notifications on two links with guest teardown between them.
- The final scenario passes **22 checks** at MTU 185: unclaimed identity,
  unauthenticated GPS rejection, P-256/HKDF pairing, delayed confirmation rejection,
  explicit modeled BOOT confirmation, mutual HMAC session proof, AES-GCM writes
  and encrypted notification proofs on initial and reconnected sessions, replay
  rejection, a second GPS update, screen-button cycling, one boot and no panic.
  The pairing screenshot matches the independently computed comparison code.
- Actual firmware counters reach `authOK=2 nav=1 route=1 gps=2 settings=1`;
  rejectAuth records the unauthenticated write and replay. The screen shows
  **No microSD card**, a position marker and the 120 m navigation overlay.
  A modeled BOOT press changes `MAP_GUIDANCE` to `RIDESTATS`.

These are synthetic ATT-client and emulator checks. They do not prove physical
CoreBluetooth discovery, iPhone app compatibility, RF, loaded SD-map rendering,
GPS receiver fidelity, power, battery or physical touch behavior.

## Retained failures and corrections

1. The original image asserts `esp_task_stack_is_sane_cache_disabled()` on its
   first map render. Symbolization reaches `Maps::renderWorkerLoop ->
   prepareMapScene -> getMapBlocks -> readMapBlock -> open -> vfs_fat_open ->
   f_open -> ff_wl_read -> esp_partition_read`. The PSRAM-backed renderer enters
   the internal flash driver while FFat owns `/sdcard`. The firmware fix uses an
   internal worker stack for FFat and retains PSRAM for removable SD. Maps/storage
   code is unchanged between the original input and the fix's base, although BLE
   and other upstream code also changed; this is not byte-identical baseline firmware.
2. The initial register-RAM PMIC stub stored IRQ-clear writes as pending PWR
   events, causing false pairing confirmation and page changes. A delayed
   CONFIRM negative control exposed it. Status `0x49` now acknowledges W1C;
   clearing idle flags never creates events. The regression test also checks
   partial clearing and ordinary configuration writes. Earlier passing counter
   replays are superseded for button-gate qualification. The final delayed test
   and screenshot verification use the corrected stub.
3. An intermediate raw-ATT reset change discarded pre-boot commands. The full
   suite caught it. Reset now cancels an active raw session while preserving
   ordinary commands queued before NimBLE initialization; both contracts pass.
4. Writing a source-hash marker inside the macOS app after signing invalidated
   its resource envelope. The helper now signs/verifies a complete temporary
   bundle before atomic publication and keeps the marker outside. Fresh build,
   cached reuse and deliberately damaged-signature repair pass. No Bluetooth
   activation is part of these checks.
5. A live start exceeded 45 seconds under concurrent compiler load above 40.
   A larger-timeout boot completed. A timeout is retained as a failure and stops
   the owned process; it is not an ESP32 timing measurement.

External PMIC status `0x49` follows `waveshare_board/axp2101.cpp` and
`axp2101_register_policy.hpp` in `seichris/open-bike-computer` at `50aaf9b1`.
That build uses ESP-IDF 5.5.1. These are external I2C PMIC semantics, not an
Espressif SoC register block. No comparison with silicon is claimed.

## Required checks

Rust stable 1.99.1: both full Clippy commands passed with warnings denied;
`ESP32SIM_ROM_DIR=.local/roms cargo +stable test --release --workspace --
--include-ignored --skip external_` passed **1,047 tests, zero failures**.
The S3 library passed 146 tests, including the new board/PMIC regressions.

All remaining CI commands passed locally: workspace build, virtual-quantum
native checks, production WASM build/sections and nine demos, timing/BLE ABIs,
worker/input/browser checks, generated-block differential tests, profiling
features, retained receipt contracts and native AArch64 tests. Python compilation,
macOS helper build/signature, whitespace and evidence privacy checks pass.
No existing golden was regenerated. [Command receipt](../../docs/evidence/bicino-development/checks.json).

## Default-workload CPU comparison (EX200 follow-up)

Main `20117b08` versus the final Rust source above, identical immutable binaries
through each batch, one warmup plus five alternating pairs, user CPU seconds.
All instruction counts and console hashes match. macOS 26.7.1/arm64, 11 logical
CPUs, Rust 1.99.1. BLE is off in these controls.

| workload | main median | candidate median | delta |
| --- | ---: | ---: | ---: |
| S3 hello, 30 s | 0.416775 | 0.416923 | +0.04% |
| C3 hello, 30 s | 3.643065 | 3.648591 | +0.15% |
| C6 hello, 30 s | 2.655028 | 2.687982 | +1.24% |
| Pocket Tank, 8 s, loaded host | 14.512365 | 15.262660 | +5.17% |
| Pocket Tank, 8 s, follow-up | 12.319881 | 12.215665 | -0.85% |

The first batch's aggregate load varied from 13.37 to 92.21; Pocket Tank was
slower in all five pairs. This negative is retained. The follow-up materially
changes measurement quality: our live emulator and test jobs were stopped;
load was 4.33–4.96. The same binary hashes and work/output contract apply.
The slowdown did not persist. This is not a speedup or hardware timing claim.
[Loaded samples](../../docs/evidence/bicino-development/default-controls-loaded.json),
[follow-up samples](../../docs/evidence/bicino-development/pocket-control-followup.json).
Two earlier harness attempts used unsupported C3 board/PSRAM flags; a later
partial batch was invalidated by a concurrent rebuild. Those partial observations
are not timing clearance; final measurements use frozen binary copies.

## Remaining qualification

The CoreBluetooth helper remains experimental beyond the bounded iPhone run
above. Its subscription-based teardown and compound-write limits remain
explicit. Longer human confirmation fails in the supplied firmware; full map
transfer, broad iOS/central compatibility and radio coexistence are unqualified.

SDMMC is absent on S3. The runner explicitly substitutes Storage::initSD with
ESP_FAIL and runs real FFat. No SD map image or full background-map rendering is
claimed. Upstream #274 owns the shared SDMMC model and depends on #273; S3 wiring
must build on that work. Physical 1.75/2.06-inch and FFat/radio memory-pressure
acceptance remain separate firmware gates.
