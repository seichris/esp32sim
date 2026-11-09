//! An explicitly bound TCP listener relays opaque bytes to the DHCP station.
use super::*;
use std::net::{SocketAddrV4, TcpListener};

pub(super) struct Forward {
    listener: TcpListener,
    guest_port: u16,
    next_port: u16,
}

impl Nat {
    /// Bind one host endpoint. No wildcard or publicly routed address is accepted.
    pub fn listen(&mut self, bind: SocketAddrV4, guest_port: u16) -> io::Result<SocketAddrV4> {
        if self.forward.is_some() || guest_port == 0 || !(bind.ip().is_private() || bind.ip().is_loopback()) {
            return Err(io::Error::new(ErrorKind::InvalidInput, "one private IPv4 or loopback listener and a nonzero guest port are required"));
        }
        let listener = TcpListener::bind(bind)?;
        listener.set_nonblocking(true)?;
        let SocketAddr::V4(actual) = listener.local_addr()? else { unreachable!("IPv4 listener") };
        self.forward = Some(Forward { listener, guest_port, next_port: 49152 });
        Ok(actual)
    }

    pub(crate) fn forward_address(&self) -> Option<Ipv4Addr> {
        let addr = self.forward.as_ref()?.listener.local_addr().ok()?;
        match addr { SocketAddr::V4(addr) => Some(*addr.ip()), _ => None }
    }

    pub(crate) fn clear_forwarded_connections(&mut self) {
        if self.forward.is_some() { self.tcp.retain(|c| !c.inbound); }
    }

    pub(crate) fn accept_forwarded(&mut self, guest_mac: [u8; 6], guest_ip: [u8; 4], gateway: [u8; 4],
                                   now_us: u64) -> Vec<Vec<u8>> {
        let Some(forward) = &mut self.forward else { return Vec::new() };
        let mut frames = Vec::new();
        for _ in 0..4 {
            let evict = if self.tcp.len() >= MAX_FLOWS {
                let Some(index) = self.tcp.iter().position(|c| matches!(c.transport, Transport::TimeWait | Transport::Closed)) else { break };
                Some(index)
            } else { None };
            let Ok((socket, _)) = forward.listener.accept() else { break };
            if socket.set_nonblocking(true).is_err() || socket.set_nodelay(true).is_err() { continue; }
            if let Some(index) = evict { self.tcp.remove(index); }
            let peer_port = loop {
                let port = forward.next_port;
                forward.next_port = if port == u16::MAX { 49152 } else { port + 1 };
                if !self.tcp.iter().any(|c| c.guest_ip == guest_ip && c.guest_port == forward.guest_port
                    && c.dst_ip == gateway && c.dst_port == port) { break port; }
            };
            self.isn = self.isn.wrapping_add(0x10000);
            let mut flow = Tcp { guest_mac, guest_ip, guest_port: forward.guest_port, dst_ip: gateway, dst_port: peer_port,
                transport: Transport::Connected(socket), guest_write: GuestWrite::Open, host_closed: false,
                our_seq: self.isn, guest_seq: 0, guest_window: 0, to_host: VecDeque::new(), unacked: VecDeque::new(),
                last_activity_us: now_us, inbound: true };
            frames.push(flow.send(SYN, Vec::new(), now_us));
            self.tcp.push(flow);
        }
        frames
    }
}
