//! Waveshare AMOLED 1.75 prototype: CO5300 pixels and CST9217 single-contact input.
use esp_soc::board::{BoardEdge, BoardModel, VirtualCycle};
use esp_soc::devices::co5300::Co5300;
use crate::i2c::{I2cDevice, Reg8Device, Tca9554, TouchState};
use std::sync::{Arc, Mutex};

/// Register format and geometry follow seichris/open-bike-computer at 03ea4cb4:
/// esp32/lib/waveshare_board/{display,cst9217_touch_frame}.hpp and include/hal.hpp.
/// Power, RTC and codec are initialization stubs; SDMMC and GPS are absent.
pub struct WaveshareAmoled175 {
    panel: Co5300,
    touch: Arc<Mutex<TouchState>>,
    cycle: VirtualCycle,
    pending: Vec<BoardEdge>,
    edges: Vec<BoardEdge>,
}

impl WaveshareAmoled175 {
    pub fn new() -> Self { Self { panel: Co5300::new(466, 466, 6), touch: Default::default(), cycle: 0, pending: Vec::new(), edges: Vec::new() } }
}
impl Default for WaveshareAmoled175 { fn default() -> Self { Self::new() } }

impl BoardModel for WaveshareAmoled175 {
    fn name(&self) -> &'static str { "waveshare-amoled175" }
    fn spi_transfer(&mut self, host: u8, tx: &[u8], rx_len: usize) -> Vec<u8> {
        if host == 2 { self.panel.transaction(tx); }
        vec![0xff; rx_len]
    }
    fn i2c_devices(&mut self) -> Vec<(u8, u8, Box<dyn I2cDevice>)> {
        vec![
            (0, 0x5a, Box::new(Cst9217::new(self.touch.clone()))),
            (0, 0x20, Box::new(Tca9554::register_ram_stub())),
            (0, 0x34, Box::new(Axp2101InitStub::new())),
            (0, 0x51, Box::new(Reg8Device::new("pcf85063a-initialization-stub", &[]))),
            (0, 0x18, Box::new(Reg8Device::new("es8311-initialization-stub", &[]))),
        ]
    }
    fn display(&self) -> Option<(u32, u32, Vec<u16>, u64)> {
        // Bicino mounts the panel a quarter turn from its native GRAM axes.
        // Firmware rotates each flush clockwise; present the mounted view upright.
        let mut mounted = vec![0; 466 * 466];
        for y in 0..466 { for x in 0..466 { mounted[y * 466 + x] = self.panel.frame[x * 466 + 465 - y]; } }
        Some((466, 466, mounted, self.panel.pixels_written))
    }
    fn gram(&self) -> Option<(Vec<u16>, usize, usize)> { Some((self.panel.frame.clone(), 466, 466)) }
    fn display_version(&self) -> u64 { self.panel.pixels_written }
    fn display_frames(&self) -> u64 { self.panel.frames }
    fn touch(&mut self, x: u16, y: u16, down: bool) {
        // Mounted coordinates -> native panel -> half-turned sensor axes.
        let mut t = self.touch.lock().expect("CST9217 touch mutex poisoned");
        t.update(y.min(465), 465 - x.min(465), down);
        let edge = BoardEdge { cycle: self.cycle.saturating_add(1 + u64::from(!self.pending.is_empty())), pin: 21, level: !down };
        if self.pending.len() < 2 { self.pending.push(edge); } else { self.pending[1] = edge; }
    }
    fn touch_at(&mut self, cycle: VirtualCycle, x: u16, y: u16, down: bool) { self.advance_to(cycle); self.touch(x, y, down); }
    fn next_deadline(&self) -> Option<VirtualCycle> { self.pending.first().map(|edge| edge.cycle) }
    fn advance_to(&mut self, cycle: VirtualCycle) {
        self.cycle = cycle;
        while self.pending.first().is_some_and(|edge| edge.cycle <= cycle) { self.edges.push(self.pending.remove(0)); }
    }
    fn take_edges(&mut self) -> Vec<BoardEdge> { std::mem::take(&mut self.edges) }
    fn input_levels(&self) -> Vec<(u8, bool)> {
        vec![(0, true), (21, !self.touch.lock().expect("CST9217 touch mutex poisoned").down)]
    }
    fn report(&self) -> String { format!("AMOLED175: frames={} pixels={} size=466x466 power=stub sd=absent", self.panel.frames, self.panel.pixels_written) }
}

/// Initialization-only PMIC, with the PWR IRQ acknowledgement the guest actually uses.
/// seichris/open-bike-computer at 50aaf9b1, waveshare_board/axp2101{.cpp,_register_policy.hpp}:
/// status 0x49 is W1C. Ordinary register RAM turns a clear into a phantom button press,
/// which can falsely confirm ownership. No PWR events, battery or rail behavior are modeled.
struct Axp2101InitStub { ram: Reg8Device, ptr: u8, first: bool }
impl Axp2101InitStub {
    fn new() -> Self { Self { ram: Reg8Device::new("axp2101-initialization-stub", &[(0x03, 0x4a)]), ptr: 0, first: true } }
}
impl I2cDevice for Axp2101InitStub {
    fn start(&mut self, read: bool) -> bool { if !read { self.first = true; } self.ram.start(read) }
    fn write(&mut self, byte: u8) -> bool {
        let value = if !self.first && self.ptr == 0x49 { self.ram.regs[0x49] & !byte } else { byte };
        let ok = self.ram.write(value);
        if self.first { self.ptr = byte; self.first = false; } else { self.ptr = self.ptr.wrapping_add(1); }
        ok
    }
    fn read(&mut self) -> u8 { self.ptr = self.ptr.wrapping_add(1); self.ram.read() }
}

struct Cst9217 {
    ptr: u16, phase: u8, packet: [u8; 15], touch: Arc<Mutex<TouchState>>,
}
impl Cst9217 {
    fn new(touch: Arc<Mutex<TouchState>>) -> Self { Self { ptr: 0, phase: 0, packet: [0; 15], touch } }
}
impl I2cDevice for Cst9217 {
    fn start(&mut self, read: bool) -> bool {
        if !read { self.phase = 0; }
        else {
            let mut t = self.touch.lock().expect("CST9217 touch mutex poisoned");
            if t.release_pending && t.seen { t.down = false; t.release_pending = false; }
            if t.down { t.seen = true; }
            self.packet = [0; 15];
            self.packet[0] = if t.down { 6 } else { 0 };
            self.packet[1] = (t.x >> 4) as u8;
            self.packet[2] = (t.y >> 4) as u8;
            self.packet[3] = ((t.x & 15) << 4 | (t.y & 15)) as u8;
            self.packet[5] = u8::from(t.down);
            self.packet[6] = 0xab;
        }
        true
    }
    fn write(&mut self, byte: u8) -> bool {
        match self.phase {
            0 => { self.ptr = u16::from(byte) << 8; self.phase = 1; }
            1 => { self.ptr |= u16::from(byte); self.phase = 2; }
            _ => { self.ptr = self.ptr.wrapping_add(1); }
        }
        true
    }
    fn read(&mut self) -> u8 {
        let value = self.ptr.checked_sub(0xd000).and_then(|i| self.packet.get(usize::from(i))).copied().unwrap_or(0);
        self.ptr = self.ptr.wrapping_add(1);
        value
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pmic_irq_acknowledgements_never_create_power_button_events() {
        let mut device = Axp2101InitStub::new();
        let write = |d: &mut Axp2101InitStub, reg, value| { d.start(false); d.write(reg); d.write(value); };
        let read = |d: &mut Axp2101InitStub, reg| { d.start(false); d.write(reg); d.start(true); d.read() };
        for _ in 0..3 { write(&mut device, 0x49, 0x0b); assert_eq!(read(&mut device, 0x49), 0); }
        device.ram.regs[0x49] = 0x0b;
        write(&mut device, 0x49, 8); assert_eq!(read(&mut device, 0x49), 3);
        write(&mut device, 0x49, 3); assert_eq!(read(&mut device, 0x49), 0);
        write(&mut device, 0x41, 0x0b); assert_eq!(read(&mut device, 0x41), 0x0b);
        assert_eq!(read(&mut device, 3), 0x4a);
    }

    #[test]
    fn full_native_panel_window_preserves_all_four_corners() {
        let mut board = WaveshareAmoled175::new();
        board.spi_transfer(2, &[2, 0, 0x2a, 0, 0, 6, 1, 0xd7], 0);
        board.spi_transfer(2, &[2, 0, 0x2b, 0, 0, 0, 1, 0xd1], 0);
        let mut pixels = vec![0; 466 * 466 * 2];
        for (i, color) in [(0, 0xf800u16), (465, 0x07e0), (466 * 465, 0x001f), (466 * 466 - 1, 0xffff)] {
            pixels[2 * i..2 * i + 2].copy_from_slice(&color.to_be_bytes());
        }
        board.spi_transfer(2, &[0x32, 0, 0x2c, 0], 0);
        board.spi_transfer(2, &pixels, 0);
        let (w, h, frame, count) = board.display().unwrap();
        assert_eq!((w, h, count), (466, 466, 466 * 466));
        assert_eq!([frame[0], frame[465], frame[466 * 465], frame[466 * 466 - 1]], [0x07e0, 0xffff, 0xf800, 0x001f]);
        let (raw, _, _) = board.gram().unwrap();
        assert_eq!([raw[0], raw[465], raw[466 * 465], raw[466 * 466 - 1]], [0xf800, 0x07e0, 0x001f, 0xffff]);
    }

    #[test]
    fn touch_packet_and_irq_match_the_firmware_decoder() {
        let mut board = WaveshareAmoled175::new();
        let mut devices = board.i2c_devices();
        let (_, _, device) = devices.iter_mut().find(|(_, a, _)| *a == 0x5a).unwrap();
        board.touch(135, 415, true);
        assert!(board.input_levels().contains(&(21, false)));
        board.advance_to(1);
        assert_eq!(board.take_edges(), [BoardEdge { cycle: 1, pin: 21, level: false }]);
        device.start(false); device.write(0xd0); device.write(0); device.start(true);
        let packet: Vec<_> = (0..15).map(|_| device.read()).collect();
        assert_eq!(packet[0], 6);
        assert_eq!(packet[5..7], [1, 0xab]);
        assert_eq!(u16::from(packet[1]) << 4 | u16::from(packet[3] >> 4), 415);
        assert_eq!(u16::from(packet[2]) << 4 | u16::from(packet[3] & 15), 330);
        board.touch(135, 415, false);
        assert!(board.input_levels().contains(&(21, true)));
        board.advance_to(2);
        assert_eq!(board.take_edges(), [BoardEdge { cycle: 2, pin: 21, level: true }]);
        device.start(false); device.write(0xd0); device.write(0); device.start(true);
        let packet: Vec<_> = (0..15).map(|_| device.read()).collect();
        assert_eq!(packet[5..7], [0, 0xab]);
    }
}
