# EX248: the SD/MMC host on the ESP32-S3 (`claude/s3-sdmmc`)

The DesignWare SD/MMC host and SD card of [EX243](../../experiments.md#ex243) (upstream #274,
`esp-periph/src/sdmmc.rs`, its `SdmmcLayout::Esp32`) wired into the ESP32-S3: an optional
`device_set!` entry for block 0x28 on interrupt source 30, an arm in `pre_access` for that block
(card time, CDETECT, WRTPRT), one address compare after each S3 peripheral read and write (the
STATUS read's busy-clear interrupt, a command or IDMAC resume), the SYSTEM_PERIP_RST_EN1 reset
bit, the card kept across a guest reboot, and a default `BoardModel::sd_socket` at the end of the
shared board trait. Nothing else on the C3, C6, classic ESP32 or P4 changes.

## Register facts

From the headers the 1.75-inch Bicino firmware compiles against (Arduino-ESP32 3.3's libraries,
`framework-arduinoespressif32-libs` 5.5.0+sha.8410210c9a, ESP-IDF 5.5.1), cross-checked against
ESP-IDF v4.4.8 (Arduino-ESP32 2.x); both agree:

| value | IDF v5.5.1 | IDF v4.4.8 |
| --- | --- | --- |
| DR_REG_SDMMC_BASE 0x60028000 | `register/soc/reg_base.h:43` | `soc.h:78` |
| ETS_SDIO_HOST_INTR_SOURCE 30 | `include/soc/interrupts.h:48` | `periph_defs.h:88` |
| SDHOST_CCLK_OUT_1/2 172/173, CARD_DETECT_N_1/2 194/195, CARD_WRITE_PRT_1/2 196/197 | `include/soc/gpio_sig_map.h:312-361` | `gpio_sig_map.h:320-369` |
| SYSTEM_SDIO_HOST_CLK_EN / SDIO_HOST_RST, bit 7 of PERIP_CLK_EN1 / PERIP_RST_EN1 | `register/soc/system_reg.h:318-323, 580-585` | `system_reg.h:328, 590` |
| GPIO_MATRIX_CONST_ONE_INPUT 0x38, CONST_ZERO_INPUT 0x3C | `include/soc/gpio_pins.h:13-14` | `gpio_pins.h:22-23` |
| SOC_SDMMC_USE_GPIO_MATRIX, no SOC_SDMMC_PSRAM_DMA_CAPABLE | `include/soc/soc_caps.h:571-581` | — |

The host's register block follows `register/soc/sdmmc_struct.h` (CLOCK at 0x800, HCON fields as
the shared model's constant) and `hal/esp32s3/include/hal/sdmmc_ll.h`. The model's VERID and
other reset values are the P4's; they were not compared with S3 silicon. Nothing here was
compared with silicon.

## Correctness checks

`esp32s3/src/bus/sdmmc_tests.rs`, ten tests: a card answers only on the slot whose
SDHOST_CCLK_OUT drives its CLK pad (IO_MUX function 1), the IDMAC reads a block into internal
SRAM and moves the code-page versions, buffers in the cache windows, the IRAM alias or past the
end of DRAM raise FBE, the host interrupt is source 30 (raised by the command itself, cleared by
W1C), CDETECT/WRTPRT follow the matrix inputs, PERIP_RST_EN1 resets the host and keeps the card, a
guest reboot keeps the card and its counts, `--sd` needs a socket and an untimed bus, and without
a card the registers answer and no source is raised. Removing any one of the post-command source
refresh, the STATUS-read hook, the reset hook or the code-page bookkeeping fails exactly one test.

Firmware: seichris/open-bike-computer `main` at `f37a084f` (WAVESHARE_AMOLED_175, build 105)
mounts a FAT32 card through the real ESP-IDF SDMMC driver on the 1.75-inch board's socket (slot
1, one-bit, CLK 2, CMD 1, D0 3, 20 MHz), selects the card's map at boot and renders map blocks
from it; a blank card fails the FatFs mount and an empty socket times out, as on hardware. That
board and its runner are in the stacked Bicino pull request (seichris/esp32sim#1).

## CPU cost

Method as [p4-cpu/README.md](../p4-cpu/README.md): `main` at `2e36224d` extracted with `git archive`
into a sibling directory whose path has the same length as the branch's, both built with
`cargo build --release --bins` (fat LTO, one codegen unit) by rustc 1.99.1 (58cf8f9c3
2026-10-13), the binaries copied before measuring, and
[p4-cpu/cpu_pairs.py](../p4-cpu/cpu_pairs.py) run from the base tree's root: one warmup pair,
then seven pairs alternating base-first and branch-first, user CPU from `wait4` (10 ms
resolution). Branch binaries are from `a19a9366` (the code is `b64b009b`'s). Binary SHA-256:

| binary | main `2e36224d` | branch `a19a9366` |
| --- | --- | --- |
| `esp32sim` | `a2b03506cc36fbb0372bcae69d702c8657dabb36b9fa8647cd409261380ea2a7` | `1c178860cbb9190fbce946b0a72b8634103915fd8319842f57c19717a7030c2a` |
| `esp32sim-c3` | `789812aa4023e540cdf7d41f652847f89799fa036d67b89669e34344b67baa16` | `423d752834ddec0ef31c8774fd0d73ac0a497d77893e89efbac5b2a8337b1904` |
| `esp32sim-c6` | `c23087233c69e26a642e5c805cb73c00f27e625cff32b35263af0e93e9efe5c8` | `43b4fb00f9282bf4fe9bc7758ac740727a2519869f9800ff4b8e41b552e85e09` |

The C3 and C6 binaries differ only through the shared board trait's new default method. Every
run of a workload retired the same instructions and printed the same console on both builds.

Machine: Apple M3 Pro (11 cores) on macOS 26.7.1, shared with other sessions' builds and an
unrelated long-running emulator process for the whole measurement; the 1-minute load average
moved between 10 and 65 and is in the tables. No load gate. Flag, as in the other receipts: the
branch slower in at least 6 of 7 pairs, or ranges that do not overlap.

### run 1, started 2026-10-29T19:21:08+08:00 — [cpu-run1.json](cpu-run1.json)

| workload | main median (range) | branch median (range) | change | branch slower | instructions | console SHA-256 | 1-min load before / after |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| s3-hello | 0.36 (0.36-0.38) | 0.38 (0.37-0.38) | +5.56% | 5/7 | 11,990,305 + 6,798,164 | `04d4912461cb…` | 28.3 / 26.5 |
| c3-hello | 4.11 (4.02-4.26) | 4.09 (3.98-4.32) | -0.49% | 3/7 | 4,800,000,000 | `3b9d8d3eb053…` | 26.5 / 14.4 |
| c6-hello | 2.81 (2.74-2.89) | 2.70 (2.68-2.80) | -3.91% | 1/7 | 4,800,000,000 | `bd69fbb9ac5c…` | 14.4 / 10.7 |
| pocket-tank | 11.85 (11.21-13.66) | 13.09 (11.35-13.93) | +10.46% | 6/7 | 1,115,772,031 + 946,151,715 | `1d1d57f7c150…` | 10.7 / 18.9 |

**Flag: `pocket-tank` +10.46%, branch slower in 6 of 7 pairs.** Its samples spread from 11.21 to
13.93 s on both builds while the load fell from 36 to 10 (per-pair changes +1.2%, +4.7%, +13.2%,
+2.0%, −0.4%, +13.7%, +4.0%).

### run 2, started 2026-10-29T19:27:43+08:00 — [cpu-run2.json](cpu-run2.json)

| workload | main median (range) | branch median (range) | change | branch slower | instructions | console SHA-256 | 1-min load before / after |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| s3-hello | 0.38 (0.37-0.45) | 0.39 (0.38-0.42) | +2.63% | 4/7 | 11,990,305 + 6,798,164 | `04d4912461cb…` | 14.1 / 12.8 |
| pocket-tank | 10.82 (10.45-17.60) | 11.66 (10.65-16.30) | +7.76% | 4/7 | 1,115,772,031 + 946,151,715 | `1d1d57f7c150…` | 12.8 / 64.9 |

Not flagged. The first four Pocket Tank pairs moved +0.6%, −4.2%, +3.1% and +9.7%; a load
spike to 65 then reached pairs 5–7 (+52.2%, −5.5%, −16.4%).

### run 3, started 2026-10-29T19:50:09+08:00 — [cpu-run3.json](cpu-run3.json)

| workload | main median (range) | branch median (range) | change | branch slower | instructions | console SHA-256 | 1-min load before / after |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| s3-hello | 0.39 (0.37-0.42) | 0.39 (0.39-0.44) | +0.00% | 5/7 | 11,990,305 + 6,798,164 | `04d4912461cb…` | 9.9 / 9.7 |
| c3-hello | 4.40 (4.22-5.07) | 4.55 (4.22-5.27) | +3.41% | 4/7 | 4,800,000,000 | `3b9d8d3eb053…` | 9.7 / 20.4 |
| c6-hello | 2.97 (2.89-3.43) | 2.93 (2.76-3.41) | -1.35% | 1/7 | 4,800,000,000 | `bd69fbb9ac5c…` | 20.4 / 14.8 |
| pocket-tank | 13.14 (11.89-17.60) | 13.29 (10.85-16.82) | +1.14% | 2/7 | 1,115,772,031 + 946,151,715 | `1d1d57f7c150…` | 14.8 / 18.7 |

Nothing flagged. Run 1's Pocket Tank flag did not reproduce (4/7, then 2/7). It is retained, not
attributed: the branch's only per-access work outside SDMMC accesses is one address compare
after each S3 peripheral read and write; the `Peripherals` struct grows by about 0.4 KiB, which
moves later fields of the bus. These loads are too high and too variable for a sub-percent
claim; a quiet-host remeasurement is the next step before upstreaming, and no speedup or
equality is claimed.
