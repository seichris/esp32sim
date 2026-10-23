# Bicino development workflow verification

The emulator uses an unchanged private build of `seichris/open-bike-computer`;
its source revision and firmware SHA-256 are documented in the README. Firmware
and raw local captures are not redistributed. This is emulator evidence, not
physical-device qualification.

## Working paths

- The original five-second capture reaches the real firmware's ready checkpoint
  and upright 466x466 welcome screen. A short 0.1-second run correctly fails.
- Brave renders the native WebSocket framebuffer and console. Clicking the panel
  produces a press/release in the real firmware's CST9217 decoder. CLI screenshot
  capture reads the same RGB565 stream without browser dependencies.
- Session start/status/input/screen/stop and immediate restart were exercised.
  Duplicate ownership and failed startup are bounded; logs remain local.
- The opt-in ATT client discovers the guest GATT table, negotiates MTU 512 and
  receives its INFO notification on two links separated by real guest teardown.
- At MTU 185 the synthetic client completes P-256 pairing and mutual ownership-v2
  authentication. CONFIRM before the modeled BOOT press returns
  `ERROR|physical_confirmation_required`; the button then produces PAIR_READY.
  Plaintext unauthenticated GPS is rejected. The guest decodes a protected turn
  instruction and a three-point route.
- The macOS CoreBluetooth helper compiles and signs. It has **not been run with
  the physical iPhone**, which remained owned by another task at the user's request.

## Retained failure

The original snapshot crashes on the first map render after route delivery:
`spi_flash_disable_interrupts_caches_and_other_cpu` asserts
`esp_task_stack_is_sane_cache_disabled()`. The symbolized call chain is
`Maps::renderWorkerLoop -> prepareMapScene -> getMapBlocks -> readMapBlock ->
open -> vfs_fat_open -> f_open -> ff_wl_read -> esp_partition_read`.

The render worker uses a PSRAM stack while FFat owns `/sdcard`. Even opening a
missing block enters the flash driver. The candidate firmware fix in
`seichris/open-bike-computer` allocates that worker internally only on FFat;
removable-SD rendering retains PSRAM. The original failure is retained locally,
and the replay result is explicitly false. Fix validation is recorded below
once its locked build and replay complete.

A live start also exceeded its 45-second deadline during concurrent release
compilation; a bounded boot with a larger timeout completed. Aggregate host load
was above 40. These observations are not firmware timing or simulator speed claims.

## Checks

The check receipt is completed after final validation. No existing golden is
regenerated. SDMMC remains unmodeled on S3; upstream #274 owns the shared model.
Storage::initSD is explicitly substituted with ESP_FAIL and real FFat runs.
GPS input is a synthetic authenticated protocol packet, not a modeled GPS receiver.
No SD-map, RF, power, battery, physical touch, or physical iPhone claim is made.

## Reproduce

Follow the README for snapshot import, then use `run`, `start`, `screen`,
`tools/bicino_ble.py probe` and `tools/bicino_scenario.py`. The original snapshot
must fail the map scenario instead of being counted as a navigation pass.
The macOS helper requires a separate physical acceptance run for scan visibility,
code comparison/claim, protected writes, notification delivery and reconnect.
Its CoreBluetooth subscription-based teardown and compound-write limits remain
explicit until tested on that phone.
