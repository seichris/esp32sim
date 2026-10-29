# Bicino workflow verification — 2026-10-24, updated 2026-10-29

The initial October 24 checks below use the native emulator and a synthetic
client. The October 26–27 follow-up also uses a real USB-connected iPhone and
the Mac's CoreBluetooth radio. The October 29 update syncs upstream, adds a
microSD card behind the ESP32-S3 SD/MMC host and requalifies current firmware;
it uses only the emulator and the synthetic client. No board was flashed. A
separate existing-image physical-board check is scoped explicitly below.

## October 29: upstream sync, microSD maps and firmware `f37a084f`

Upstream `mikroverk/esp32sim` `main` is `2e36224d34decc2fc9d2bba5fa4c5fdf2171b0a6`
(through #293, including the shared SD/MMC host of #274). The fork's `main`
fast-forwarded from `20117b08` to it; the fork had no commits of its own there.
This branch merged it in `8f680ef9` (four additive conflicts: CLI help, board
docs, the BLE peer's state and the live page). The S3 host is the stacked
`claude/s3-sdmmc` branch (`b64b009b`, `a19a9366`), merged here. All runs below
use emulator `7ce4819f0836b4093edad46f698fae7e91227a80`, clean, Rust 1.99.1.

Firmware is `seichris/open-bike-computer` `main` at
`f37a084facc8a286ad506bd51bfc2e6adbd19957`, built from a clean checkout by its
locked builder (`python3 tools/build_firmware.py WAVESHARE_AMOLED_175`, ordinary
profile, upload-eligible, core cache miss, ESP-IDF 5.5.1). The running
`BOOT_META` reports target and profile `WAVESHARE_AMOLED_175`, version 0.3.4,
build 105, that Git SHA, `built=2026-10-29T10:30:05Z` and fingerprint `289B86E9`.
Firmware SHA-256 `ada0975cb17d1378b33861d67ddc1fef3abfca473668b49fe4463dfe53150787`,
ELF `352c456292fc1aff80d409b8b363363bde3736180f5deb25fecdbfb551f8fed2`,
bootloader `83a06f27…`, partition table `7dd5955a…` and `boot_app0.bin`
`f94c5d78…` all equal the builder's `FIRMWARE_BUILD_PROVENANCE`. Setup now
takes `boot_app0.bin` from the build's flash plan and writes it at `0xe000`, as
the locked uploader does; earlier runs left that OTA data erased. Not flashed;
cached `50aaf9b1` was not reused.

| Run | Storage | Result | Firmware-side evidence |
| --- | --- | --- | --- |
| 5 s boot | substitution | pass, 7/7 | ready at 1.73 s |
| 0.1 s capture | substitution | fails as intended, exit 1 | no `BOOT_META`, ready, advertising or screen |
| 8 s, `touch.txt` | substitution | pass, 8/8 | the guest read 9 contact packets |
| 10 s, empty socket | empty socket | pass, 7/7 | 30 SD commands, 27 unanswered; SPI fallback, FFat; ready at 4.00 s |
| 10 s, blank card | card | fails as intended, exit 1 | `sd_mounted` false: 72 commands, FatFs finds no file system |
| 8 s, Golden Gate card | card | pass, 10/10 | native mount on attempt 1; `activeMapId=golden-gate-labels-t3`; block and font sectors read; ready at 1.56 s |
| ATT probe | substitution | pass | MTU 512, three services, INFO on two links |
| replay | substitution | pass, 22/22 | `authOK=2 nav=1 route=1 gps=2 settings=1 rejectAuth=2`; `mapFound=0` |
| replay, confirmation after 13 s | substitution | fails, 6 checks then disconnect | retained firmware failure, below |
| replay with card at 37.8199, −122.4783 | card | pass, 26/26 | blocks `15_7` and `15_8` read after the GPS write; `mapFound=1 mapBlocks=2` |

The card image comes from `tools/bicino_sdcard.py` (two builds, identical
SHA-256 `c5c9fef2934599147630f96b237d3b218c1efe009eba0f5d2e226a830ac3b52d`;
macOS `fsck_msdos -n` on its partition reports no errors). It holds the frozen
`golden-gate-labels-t3` extraction fixture from open-bike-computer `f37a084f`
under `VECTMAP/.maps/` and a session-less `active-map.json`. No-card and
populated-SD results are kept apart by `storage.mode` in every result; the
scenario's `sd_maps` is true only for the card run. Screenshots, logs, firmware
and images stay in ignored `.local/`; the
[receipt](../../docs/evidence/bicino-development/sd-sync-2026-10-29.json) holds
commands, inputs, hashes, checks and outcomes.

Retained failures:

1. **Touch, an emulator regression, fixed.** The new `touch_contact_read` check
   failed for `touch.txt` and for a tap at 7.3 s on clean `3928b387`: zero
   contact reads. open-bike-computer `73d2f9ad` moved the CST9217 TP_INT to
   GPIO11, its vendor-schematic net, and the firmware now reads only after
   that edge; the board still raised GPIO21. `7ce4819f` raises GPIO11.
2. **Delayed confirmation, a firmware failure, fixed in
   [open-bike-computer #667](https://github.com/seichris/open-bike-computer/pull/667).**
   Confirming 13 modeled seconds after connecting logged `Ownership pairing
   confirmed`, `Device ownership registered` and then `Disconnecting
   unauthenticated client after timeout`, before the claim and authentication:
   the allowance was 120 s only while a pairing code was open, then 12 s from
   the original connection. #667 (`a8a0297e`) restarts the window when CONFIRM
   registers ownership. On the same replay revision (`cae769c2`, clean), with
   the new `--abandon` controls:

   | Replay | `f37a084f` | `a8a0297e` |
   | --- | --- | --- |
   | confirmation after 13 s | fails, dropped after registration | pass, 22/22 |
   | confirmation after 100 s | fails, dropped after registration | pass, 22/22 |
   | silent after `PAIRED` | dropped 9.28 s later: fails | dropped 12.02 s later: pass |
   | silent after `PAIRED`, 13 s confirmation | — | dropped 12.02 s later: pass |
   | silent after connecting | dropped 12.12 s later: pass | dropped 12.18 s later: pass |

   Two replay timing bugs surfaced on the way and are fixed: a 90-second wall
   limit on advancing modeled time, and a main-screen check that read the last
   5-second status line instead of waiting for one. Receipt:
   [deadline-fix-2026-10-29.json](../../docs/evidence/bicino-development/deadline-fix-2026-10-29.json).
3. The 0.1-second capture and the blank card fail by design.

The S3 bus is untimed and the card ideal; the selection is hand-written, not an
app-installed durable map, so no transfer, manifest receipt or signature path
runs. Wall times ran on a shared host under other sessions' load and are not
performance results. The CPU controls for the S3 change are in
[EX248](../../docs/experiments.md#ex248).

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
