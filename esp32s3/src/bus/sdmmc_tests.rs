//! The SD/MMC host on the S3: a card reached through the GPIO matrix, the IDMAC in internal
//! SRAM, interrupt source 30, the SYSTEM reset bit and a guest reboot.
use super::*;
use crate::periph::{SIG_SDHOST_CCLK_OUT, SRC_SDIO_HOST};
use esp_periph::sdmmc::{des0, idsts, int, reg, MemStore, SdCard, CMD_DATA, CMD_RESP, CMD_RESP_CRC, CMD_RESP_LONG, CMD_START, CTRL_INT_ENABLE, CTRL_USE_INTERNAL_DMA, BMOD_DE};
use esp_soc::board::{BoardModel, SdSocket};

const SD: u32 = 0x6002_8000;
/// A one-bit socket on CLK 2, CMD 1, D0 3, D3 41.
const SOCKET: SdSocket = SdSocket { clk: 2, cmd: 1, data: [Some(3), None, None, Some(41)] };
const DESC: u32 = DRAM_LOW + 0x1000;
const BUF: u32 = DRAM_LOW + 0x2000;

struct Socketed;
impl BoardModel for Socketed {
    fn name(&self) -> &'static str { "socketed" }
    fn sd_socket(&self) -> Option<SdSocket> { Some(SOCKET) }
}

/// A 1 MiB card whose block n starts with n.
fn image() -> Vec<u8> { let mut v = vec![0u8; 1 << 20]; for (n, b) in v.chunks_mut(512).enumerate() { b[0] = n as u8; b[511] = 0xa5; } v }

fn bus_with_card() -> SocBus {
    let mut bus = SocBus::new(1024, 1024, [0; 6]);
    bus.board = Box::new(Socketed);
    esp_soc::SocBus::attach_sd_card(&mut bus, Box::new(MemStore(image())), Default::default()).unwrap();
    bus
}

/// The CLK pad as sdmmc_host_init_slot leaves it: a GPIO (IO_MUX MCU_SEL 1) driving SDHOST_CCLK_OUT of `slot`.
fn route_clock(bus: &mut SocBus, slot: usize) {
    bus.write32(0x6000_9004 + 4 * SOCKET.clk as u32, 1 << 12).unwrap();
    bus.write32(0x6000_4554 + 4 * SOCKET.clk as u32, SIG_SDHOST_CCLK_OUT[slot]).unwrap();
}

/// A command on `slot`; returns RINTSTS after it, then clears it.
fn cmd(bus: &mut SocBus, slot: u32, index: u32, arg: u32, flags: u32) -> u32 {
    bus.write32(SD + reg::CMDARG, arg).unwrap();
    bus.write32(SD + reg::CMD, CMD_START | slot << 16 | flags | index).unwrap();
    let r = bus.read32(SD + reg::RINTSTS).unwrap();
    bus.write32(SD + reg::RINTSTS, r).unwrap();
    r
}

/// CMD0 to CMD7, as sdmmc_card_init does for an SDHC card in one-bit mode.
fn select_card(bus: &mut SocBus, slot: u32) {
    let short = CMD_RESP | CMD_RESP_CRC;
    cmd(bus, slot, 0, 0, 0);
    assert_eq!(cmd(bus, slot, 8, 0x1aa, short), int::CMD_DONE, "CMD8 answered");
    let ready = (0..8).any(|_| {
        cmd(bus, slot, 55, 0, short);
        cmd(bus, slot, 41, 0x40ff_8000, CMD_RESP);
        bus.read32(SD + reg::RESP0).unwrap() & 1 << 31 != 0
    });
    assert!(ready, "ACMD41 power-up done");
    cmd(bus, slot, 2, 0, CMD_RESP | CMD_RESP_LONG);
    cmd(bus, slot, 3, 0, short);
    let rca = bus.read32(SD + reg::RESP0).unwrap() >> 16;
    assert_eq!(cmd(bus, slot, 7, rca << 16, short), int::CMD_DONE);
}

/// One descriptor for a 512-byte read into BUF, and the IDMAC on.
fn arm_read(bus: &mut SocBus, buf: u32) {
    bus.write32(DESC, des0::OWN | des0::FS | des0::LD | des0::CH).unwrap();
    bus.write32(DESC + 4, 512).unwrap();
    bus.write32(DESC + 8, buf).unwrap();
    bus.write32(DESC + 12, 0).unwrap();
    bus.write32(SD + reg::CTRL, CTRL_USE_INTERNAL_DMA | CTRL_INT_ENABLE).unwrap();
    bus.write32(SD + reg::BMOD, BMOD_DE).unwrap();
    bus.write32(SD + reg::DBADDR, DESC).unwrap();
    bus.write32(SD + reg::BLKSIZ, 512).unwrap();
    bus.write32(SD + reg::BYTCNT, 512).unwrap();
}

#[test]
fn a_card_answers_only_on_the_slot_that_drives_its_clock() {
    let mut bus = bus_with_card();
    assert_eq!(cmd(&mut bus, 1, 8, 0x1aa, CMD_RESP | CMD_RESP_CRC), int::RTO | int::CMD_DONE, "CLK not routed: no card");
    route_clock(&mut bus, 1);
    assert_eq!(cmd(&mut bus, 0, 8, 0x1aa, CMD_RESP | CMD_RESP_CRC), int::RTO | int::CMD_DONE, "slot 0 does not reach it");
    assert_eq!(cmd(&mut bus, 1, 8, 0x1aa, CMD_RESP | CMD_RESP_CRC), int::CMD_DONE);
    assert_eq!(bus.read32(SD + reg::RESP0).unwrap(), 0x1aa, "R7 echoes the check pattern");
    route_clock(&mut bus, 0);
    assert_eq!(cmd(&mut bus, 0, 8, 0x1aa, CMD_RESP | CMD_RESP_CRC), int::CMD_DONE, "the same card on slot 0");
    // A pad left on IO_MUX function 0 is not driven by the matrix.
    bus.write32(0x6000_9004 + 4 * SOCKET.clk as u32, 0).unwrap();
    assert_eq!(cmd(&mut bus, 0, 8, 0x1aa, CMD_RESP | CMD_RESP_CRC), int::RTO | int::CMD_DONE);
    assert_eq!(bus.periph.sdmmc.stats.timeouts, 3);
}

#[test]
fn the_idmac_reads_a_block_into_internal_sram_and_moves_the_code_versions() {
    let mut bus = bus_with_card();
    route_clock(&mut bus, 1);
    select_card(&mut bus, 1);
    arm_read(&mut bus, BUF);
    let off = (BUF - DRAM_LOW) as usize + 0x8000;
    let page = bus.ver_base[SRC_SRAM as usize] as usize + (off >> VPAGE_SHIFT);
    let before = bus.page_ver[page];
    assert_eq!(cmd(&mut bus, 1, 17, 5, CMD_RESP | CMD_RESP_CRC | CMD_DATA), int::CMD_DONE | int::DATA_OVER);
    assert_eq!((bus.read8(BUF).unwrap(), bus.read8(BUF + 511).unwrap()), (5, 0xa5), "block 5");
    assert_eq!(bus.read32(DESC).unwrap() & des0::OWN, 0, "descriptor back to the CPU");
    assert_eq!(bus.read32(SD + reg::IDSTS).unwrap() & (idsts::RI | idsts::NIS), idsts::RI | idsts::NIS);
    assert_ne!(bus.page_ver[page], before, "decoded code over the buffer is invalidated");
    assert_eq!(bus.periph.sdmmc.stats.bytes_read, 512);
}

#[test]
fn a_buffer_outside_internal_sram_is_a_fatal_bus_error() {
    for buf in [DBUS_LOW, IRAM_LOW, DRAM_HIGH - 256] {
        let mut bus = bus_with_card();
        route_clock(&mut bus, 1);
        select_card(&mut bus, 1);
        arm_read(&mut bus, buf);
        cmd(&mut bus, 1, 17, 0, CMD_RESP | CMD_RESP_CRC | CMD_DATA);
        let st = bus.read32(SD + reg::IDSTS).unwrap();
        assert_eq!(st & (idsts::FBE | idsts::AIS), idsts::FBE | idsts::AIS, "buffer {buf:#x}");
    }
}

#[test]
fn the_host_interrupt_is_source_30() {
    let mut bus = bus_with_card();
    route_clock(&mut bus, 1);
    let line = |b: &SocBus| b.periph.source_status()[SRC_SDIO_HOST / 32] & 1 << (SRC_SDIO_HOST % 32) != 0;
    bus.write32(SD + reg::INTMASK, int::CMD_DONE).unwrap();
    bus.write32(SD + reg::CMDARG, 0x1aa).unwrap();
    bus.write32(SD + reg::CMD, CMD_START | 1 << 16 | CMD_RESP | CMD_RESP_CRC | 8).unwrap();
    assert!(!line(&bus), "CTRL.INT_ENABLE is off");
    bus.write32(SD + reg::CTRL, CTRL_INT_ENABLE).unwrap();
    assert!(line(&bus));
    bus.write32(SD + reg::RINTSTS, int::CMD_DONE).unwrap();
    assert!(!line(&bus), "cleared by W1C");
    // Raised by the command itself, not by a later register write.
    bus.write32(SD + reg::CMD, CMD_START | 1 << 16 | CMD_RESP | CMD_RESP_CRC | 8).unwrap();
    assert!(line(&bus));
}

#[test]
fn a_status_read_after_programming_raises_busy_clear() {
    let mut bus = SocBus::new(1024, 1024, [0; 6]);
    bus.board = Box::new(Socketed);
    let slow = esp_periph::sdmmc::SdCardConfig { prg_ns: 1_000, ..Default::default() };
    esp_soc::SocBus::attach_sd_card(&mut bus, Box::new(MemStore(image())), slow).unwrap();
    route_clock(&mut bus, 1);
    select_card(&mut bus, 1);
    for i in 0..512 { bus.write8(BUF + i, i as u8).unwrap(); }
    arm_read(&mut bus, BUF);
    bus.write32(SD + reg::CARDTHRCTL, esp_periph::sdmmc::CARDTHRCTL_BUSY_CLR_INT_EN).unwrap();
    bus.write32(SD + reg::INTMASK, int::BCI).unwrap();
    assert_eq!(cmd(&mut bus, 1, 24, 9, CMD_RESP | CMD_RESP_CRC | CMD_DATA | esp_periph::sdmmc::CMD_WRITE), int::CMD_DONE | int::DATA_OVER);
    assert_ne!(bus.read32(SD + reg::STATUS).unwrap() & 1 << 9, 0, "DAT0 busy while the card programs");
    let line = |b: &SocBus| b.periph.source_status()[0] & 1 << SRC_SDIO_HOST != 0;
    assert!(!line(&bus));
    bus.periph.tick(1_000);                                   // 4.2 us at 240 MHz
    assert_eq!(bus.read32(SD + reg::STATUS).unwrap() & 1 << 9, 0);
    assert!(line(&bus), "BCI raised by the STATUS read");
    assert!(bus.irq_dirty);
    // And the block reads back.
    bus.write32(SD + reg::RINTSTS, int::ALL).unwrap();
    arm_read(&mut bus, BUF + 0x400);
    cmd(&mut bus, 1, 17, 9, CMD_RESP | CMD_RESP_CRC | CMD_DATA);
    assert_eq!((bus.read8(BUF + 0x400 + 7).unwrap(), bus.read8(BUF + 0x400 + 511).unwrap()), (7, 255));
}

#[test]
fn card_detect_and_write_protect_come_from_the_matrix() {
    let mut bus = bus_with_card();
    // Reset selection: constant low on every input, as the model's matrix starts.
    assert_eq!(bus.read32(SD + reg::CDETECT).unwrap() & 3, 0, "present");
    // sdmmc_host_init_slot without a CD pin: GPIO_MATRIX_CONST_ZERO_INPUT; without WP:
    // GPIO_MATRIX_CONST_ONE_INPUT, inverted. Slot 0 here gets a CD pad held high instead.
    bus.write32(0x6000_4154 + 4 * 195, 0x80 | 0x3c).unwrap();
    bus.write32(0x6000_4154 + 4 * 197, 0x80 | 0x40 | 0x38).unwrap();
    bus.write32(0x6000_4154 + 4 * 194, 0x80 | 7).unwrap();
    esp_soc::SocBus::gpio_set_input(&mut bus, 7, true);
    assert_eq!(bus.read32(SD + reg::CDETECT).unwrap() & 3, 1, "slot 0 empty, slot 1 present");
    assert_eq!(bus.read32(SD + reg::WRTPRT).unwrap() & 2, 0, "slot 1 writable");
}

#[test]
fn perip_rst_en1_resets_the_host_and_keeps_the_card() {
    let mut bus = bus_with_card();
    route_clock(&mut bus, 1);
    bus.write32(SD + reg::INTMASK, int::CMD_DONE).unwrap();
    bus.write32(SD + reg::CTRL, CTRL_INT_ENABLE).unwrap();
    cmd(&mut bus, 1, 8, 0x1aa, CMD_RESP | CMD_RESP_CRC);
    bus.write32(PERIPH_BASE + 0xc0024, 1 << 7).unwrap();
    bus.write32(PERIPH_BASE + 0xc0024, 0).unwrap();
    assert_eq!((bus.read32(SD + reg::INTMASK).unwrap(), bus.read32(SD + reg::CTRL).unwrap()), (0, 0));
    assert_eq!(cmd(&mut bus, 1, 8, 0x1aa, CMD_RESP | CMD_RESP_CRC), int::CMD_DONE, "the card is still there");
}

#[test]
fn a_guest_reboot_keeps_the_card_and_its_counts() {
    let mut bus = bus_with_card();
    route_clock(&mut bus, 1);
    select_card(&mut bus, 1);
    esp_soc::SocBus::reboot(&mut bus, [0; 6]);
    assert_eq!((bus.periph.sdmmc.devices.len(), bus.periph.sd_sockets.len()), (1, 1));
    assert!(bus.periph.sdmmc.stats.commands > 0);
    assert_eq!(bus.read32(SD + reg::CMD).unwrap() & CMD_START, 0);
    assert_eq!(cmd(&mut bus, 1, 8, 0x1aa, CMD_RESP | CMD_RESP_CRC), int::RTO | int::CMD_DONE, "routes reset with the GPIO matrix");
    route_clock(&mut bus, 1);
    cmd(&mut bus, 1, 0, 0, 0);
    assert_eq!(cmd(&mut bus, 1, 8, 0x1aa, CMD_RESP | CMD_RESP_CRC), int::CMD_DONE, "the selected card answers again after CMD0");
}

#[test]
fn a_card_needs_a_socket_and_an_untimed_bus() {
    let mut bus = SocBus::new(1024, 1024, [0; 6]);
    let e = esp_soc::SocBus::attach_sd_card(&mut bus, Box::new(MemStore(image())), Default::default()).unwrap_err();
    assert!(e.contains("no SD card socket"), "{e}");
    bus.board = Box::new(Socketed);
    let timed = esp_periph::sdmmc::SdCardConfig { timed_bus: true, ..Default::default() };
    assert!(esp_soc::SocBus::attach_sd_card(&mut bus, Box::new(MemStore(image())), timed).is_err());
    assert!(bus.periph.sdmmc.devices.is_empty());
    let what = esp_soc::SocBus::attach_sd_card(&mut bus, Box::new(MemStore(image())), Default::default()).unwrap();
    assert!(what.ends_with("at CLK 2 CMD 1 D0-D3 3,-,-,41"), "{what}");
    // A second card in the same socket replaces the first.
    bus.periph.attach_sd(SOCKET, Box::new(SdCard::new(Box::new(MemStore(image()))).unwrap()));
    assert_eq!(bus.periph.sdmmc.devices.len(), 1);
}

#[test]
fn without_a_card_the_registers_answer_and_no_source_is_raised() {
    let mut bus = SocBus::new(1024, 1024, [0; 6]);
    bus.write32(SD + reg::CTRL, CTRL_INT_ENABLE).unwrap();
    bus.write32(SD + reg::INTMASK, int::ALL).unwrap();
    bus.write32(SD + reg::CMD, CMD_START | 1 << 21).unwrap();
    assert_eq!(bus.read32(SD + reg::CMD).unwrap() & CMD_START, 0, "a clock update is taken at once");
    assert_eq!(bus.periph.source_status()[0] & 1 << SRC_SDIO_HOST, 0);
    assert_eq!(cmd(&mut bus, 1, 8, 0x1aa, CMD_RESP | CMD_RESP_CRC), int::RTO | int::CMD_DONE);
}
