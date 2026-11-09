use super::*;
use std::net::TcpListener;

mod counts;

fn flow() -> Tcp {
    Tcp { guest_mac: [2; 6], guest_ip: [10, 0, 2, 15], guest_port: 1234,
        dst_ip: [127, 0, 0, 1], dst_port: 80, transport: Transport::Closed,
        guest_write: GuestWrite::Open, host_closed: false,
        our_seq: 100, guest_seq: 200, guest_window: WINDOW,
        to_host: VecDeque::new(), unacked: VecDeque::new(), last_activity_us: 0, inbound: false }
}

fn segment(seq: u32, ack: u32, flags: u8, data: &[u8]) -> Vec<u8> {
    let mut seg = vec![0; 20];
    seg[..2].copy_from_slice(&1234u16.to_be_bytes());
    seg[2..4].copy_from_slice(&80u16.to_be_bytes());
    seg[4..8].copy_from_slice(&seq.to_be_bytes());
    seg[8..12].copy_from_slice(&ack.to_be_bytes());
    seg[12] = 0x50; seg[13] = flags;
    seg[14..16].copy_from_slice(&WINDOW.to_be_bytes());
    seg.extend_from_slice(data);
    seg
}

fn socket_pair() -> (TcpStream, TcpStream) {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let a = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
    let b = listener.accept().unwrap().0;
    a.set_nonblocking(true).unwrap();
    b.set_read_timeout(Some(std::time::Duration::from_secs(1))).unwrap();
    (a, b)
}

// The relay is nonblocking; wait for kernel loopback delivery without advancing emulated time.
fn poll_ready(nat: &mut Nat, now_us: u64) -> Vec<Vec<u8>> {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
    loop {
        let frames = nat.poll(now_us);
        if !frames.is_empty() { return frames; }
        assert!(std::time::Instant::now() < deadline, "loopback delivery timed out");
        std::thread::yield_now();
    }
}

#[test]
fn host_listener_relays_only_after_the_guest_completes_the_handshake() {
    let mac = [2; 6];
    let guest = [10, 0, 2, 15];
    let gateway = [10, 0, 2, 2];
    let mut nat = Nat::new(false);
    assert!(nat.listen("0.0.0.0:0".parse().unwrap(), 8080).is_err());
    let bind = nat.listen("127.0.0.1:0".parse().unwrap(), 8080).unwrap();
    let mut host = TcpStream::connect(bind).unwrap();
    host.set_read_timeout(Some(std::time::Duration::from_secs(1))).unwrap();
    host.write_all(b"upload").unwrap();
    let frames = nat.accept_forwarded(mac, guest, gateway, 0);
    assert_eq!(frames.len(), 1);
    let syn = &frames[0][34..];
    assert_eq!(syn[13], SYN);
    assert_eq!(u16::from_be_bytes(syn[2..4].try_into().unwrap()), 8080);
    let peer_port = u16::from_be_bytes(syn[..2].try_into().unwrap());
    let ack = u32::from_be_bytes(syn[4..8].try_into().unwrap()).wrapping_add(1);
    let guest_segment = |seq, ack, flags, data: &[u8]| {
        let mut bytes = segment(seq, ack, flags, data);
        bytes[..2].copy_from_slice(&8080u16.to_be_bytes());
        bytes[2..4].copy_from_slice(&peer_port.to_be_bytes());
        bytes
    };
    assert!(nat.tcp_in(&mac, &guest, &gateway, &guest_segment(1, ack, ACK, b""), 1).is_empty());
    assert!(nat.poll(1).is_empty(), "an ACK alone must not release host data");
    let reply = nat.tcp_in(&mac, &guest, &gateway, &guest_segment(100, ack, SYN | ACK, b""), 2);
    assert_eq!(reply[0][47], ACK);
    let duplicate = nat.tcp_in(&mac, &guest, &gateway, &guest_segment(100, ack, SYN | ACK, b""), 3);
    assert_eq!(reply, duplicate, "retry a lost final handshake ACK");
    let data = poll_ready(&mut nat, 4);
    assert_eq!(&data[0][54..], b"upload");
    nat.tcp_in(&mac, &guest, &gateway, &guest_segment(101, ack.wrapping_add(6), ACK, b"stored"), 5);
    nat.poll(5);
    let mut received = [0; 6];
    host.read_exact(&mut received).unwrap();
    assert_eq!(&received, b"stored");
    nat.clear_forwarded_connections();
    assert_eq!(nat.tcp_flow_counts().active, 0);
}

#[test]
fn syn_data_and_fin_share_retransmission_and_cumulative_ack() {
    let mut c = flow();
    for (flags, data) in [(SYN | ACK, Vec::new()), (PSH | ACK, b"hello".to_vec()), (FIN | ACK, Vec::new())] {
        let seq = c.our_seq;
        let frame = c.send(flags, data.clone(), 10);
        assert!(c.retransmit(RETRANSMIT_US).is_none());
        assert_eq!(c.retransmit(RETRANSMIT_US + 10).unwrap(), frame);
        assert_eq!(u32::from_be_bytes(frame[38..42].try_into().unwrap()), seq);
        assert_eq!(transport_checksum(&c.dst_ip, &c.guest_ip, 6, &frame[34..]), 0);
        c.acknowledge(c.our_seq);
        assert!(c.unacked.is_empty());
    }
    assert_eq!(c.our_seq, 107);
}

#[test]
fn partial_and_wrapping_ack_preserve_unsent_sequence_space() {
    let mut c = flow();
    c.our_seq = u32::MAX - 2;
    c.send(PSH | ACK, b"abcdef".to_vec(), 0);
    c.send(FIN | ACK, Vec::new(), 0);
    c.acknowledge(1000); // ACK beyond what we sent is ignored
    assert_eq!(c.in_flight(), 7);
    c.acknowledge(0);
    assert_eq!(c.unacked[0].data, b"def");
    assert_eq!(c.unacked[0].seq, 0);
    c.acknowledge(4);
    assert!(c.unacked.is_empty());
}

#[test]
fn guest_fin_is_in_order_and_idempotent_including_payload() {
    let mut c = flow();
    c.accept(201, b"", true);
    assert_eq!(c.guest_seq, 200);
    c.accept(200, b"abc", true);
    assert_eq!(c.guest_seq, 204);
    c.accept(200, b"abc", true);
    c.accept(203, b"", true);
    assert_eq!(c.guest_seq, 204);
    assert_eq!(c.to_host.make_contiguous(), b"abc");
    assert!(c.guest_write == GuestWrite::Draining);
}

#[test]
fn full_guest_buffer_does_not_ack_unstored_bytes_or_fin() {
    let mut c = flow();
    c.accept(200, &vec![7; WINDOW as usize], false);
    let next = c.guest_seq;
    c.accept(next, &[8], true);
    assert_eq!(c.guest_seq, next);
    assert_eq!(c.to_host.len(), WINDOW as usize);
    assert!(c.guest_write == GuestWrite::Open);
    let ack = c.segment(ACK, &[], c.our_seq);
    assert_eq!(&ack[48..50], &[0, 0]); // advertised window
}

struct ShortWriter { bytes: Vec<u8>, allowance: usize }
impl Write for ShortWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.allowance == 0 { return Err(ErrorKind::WouldBlock.into()); }
        let n = bytes.len().min(self.allowance).min(2);
        self.bytes.extend_from_slice(&bytes[..n]); self.allowance -= n;
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> { Ok(()) }
}

#[test]
fn short_write_and_would_block_preserve_exact_remaining_bytes() {
    let mut writer = ShortWriter { bytes: Vec::new(), allowance: 3 };
    let mut queue = VecDeque::from(b"abcdefgh".to_vec());
    assert_eq!(flush_pending(&mut writer, &mut queue).unwrap(), 3);
    assert_eq!(queue.make_contiguous(), b"defgh");
    assert_eq!(flush_pending(&mut writer, &mut queue).unwrap(), 0);
    writer.allowance = 100;
    assert_eq!(flush_pending(&mut writer, &mut queue).unwrap(), 5);
    assert!(queue.is_empty());
    assert_eq!(writer.bytes, b"abcdefgh");
}

#[test]
fn fin_waits_for_host_write_drain_and_remains_until_acknowledged() {
    let (socket, mut host) = socket_pair();
    let mut nat = Nat::new(false);
    let mut c = flow(); c.transport = Transport::Connected(socket);
    nat.tcp.push(c);
    let reply = nat.tcp_in(&[2; 6], &[10, 0, 2, 15], &[127, 0, 0, 1], &segment(200, 100, ACK | FIN, b"hello"), 0);
    assert_eq!(reply.len(), 1);
    nat.poll(1);
    let mut received = Vec::new(); host.read_to_end(&mut received).unwrap();
    assert_eq!(received, b"hello");
    host.shutdown(std::net::Shutdown::Write).unwrap();
    let sent = poll_ready(&mut nat, 2);
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0][47], ACK | FIN);
    assert_eq!(nat.poll(RETRANSMIT_US + 2), sent);
    assert_eq!(nat.tcp.len(), 1);
    nat.tcp_in(&[2; 6], &[10, 0, 2, 15], &[127, 0, 0, 1], &segment(206, 101, ACK, &[]), RETRANSMIT_US + 3);
    nat.poll(RETRANSMIT_US + 4);
    assert!(matches!(nat.tcp[0].transport, Transport::TimeWait));
    let reply = nat.tcp_in(&[2; 6], &[10, 0, 2, 15], &[127, 0, 0, 1], &segment(205, 101, ACK | FIN, &[]), RETRANSMIT_US + 5);
    assert_eq!(reply.len(), 1);
    assert_eq!(u32::from_be_bytes(reply[0][42..46].try_into().unwrap()), 206);
    nat.poll(RETRANSMIT_US + 5 + TIME_WAIT_US);
    assert!(nat.tcp.is_empty());
}

#[test]
fn dropped_syn_ack_is_resent_on_timer_and_duplicate_syn() {
    let (socket, _host) = socket_pair();
    let (tx, rx) = channel(); tx.send(Ok(socket)).unwrap();
    let mut c = flow(); c.transport = Transport::Connecting(rx);
    let mut nat = Nat::new(false); nat.tcp.push(c);
    let syn_ack = nat.poll(0);
    assert_eq!(syn_ack.len(), 1);
    assert_eq!(nat.poll(RETRANSMIT_US), syn_ack);
    assert_eq!(nat.tcp_in(&[2; 6], &[10, 0, 2, 15], &[127, 0, 0, 1], &segment(199, 0, SYN, &[]), RETRANSMIT_US + 1), syn_ack);
    nat.tcp_in(&[2; 6], &[10, 0, 2, 15], &[127, 0, 0, 1], &segment(200, 101, ACK, &[]), RETRANSMIT_US + 2);
    assert!(nat.poll(RETRANSMIT_US * 2).is_empty());
}

#[test]
fn host_reads_stop_at_the_unacknowledged_window() {
    let (socket, mut host) = socket_pair();
    host.write_all(&vec![7; WINDOW as usize * 2]).unwrap();
    let mut c = flow(); c.transport = Transport::Connected(socket);
    let mut nat = Nat::new(false); nat.tcp.push(c);
    let frames = poll_ready(&mut nat, 0);
    assert_eq!(frames.iter().map(|frame| frame.len() - 54).sum::<usize>(), WINDOW as usize);
    assert_eq!(nat.tcp[0].in_flight(), WINDOW as usize);
    assert!(nat.poll(1).is_empty());
    assert_eq!(nat.tcp[0].in_flight(), WINDOW as usize);
}

#[test]
fn zero_window_probe_recovers_a_lost_window_update() {
    let (socket, mut host) = socket_pair();
    host.write_all(b"hello").unwrap();
    let mut c = flow(); c.transport = Transport::Connected(socket); c.guest_window = 0;
    let mut nat = Nat::new(false); nat.tcp.push(c);
    let probe = poll_ready(&mut nat, 0);
    assert_eq!(probe.len(), 1);
    assert_eq!(&probe[0][54..], b"h");
    assert_eq!(nat.poll(RETRANSMIT_US), probe);
    // The repeated byte reaches the reopened window even though its window update was lost.
    nat.tcp_in(&[2; 6], &[10, 0, 2, 15], &[127, 0, 0, 1], &segment(200, 101, ACK, &[]), RETRANSMIT_US + 1);
    let rest = poll_ready(&mut nat, RETRANSMIT_US + 2);
    assert_eq!(rest.len(), 1);
    assert_eq!(&rest[0][54..], b"ello");
}

#[test]
fn connector_permits_are_bounded_until_the_worker_drops_them() {
    let counter = AtomicUsize::new(0);
    let mut permits: Vec<_> = (0..MAX_CONNECTS).map(|_| ConnectPermit::acquire(&counter).unwrap()).collect();
    assert!(ConnectPermit::acquire(&counter).is_none());
    permits.pop();
    assert!(ConnectPermit::acquire(&counter).is_some());
}

#[test]
fn time_wait_is_evicted_only_after_a_connect_worker_is_admitted() {
    // Final-ACK retry records cannot monopolize live-flow slots during connection churn.
    // Inject admission results so concurrent real workers cannot affect this test.
    let mut nat = Nat::new(false);
    for port in 0..MAX_FLOWS {
        let mut c = flow(); c.guest_port = port as u16; c.transport = Transport::TimeWait;
        nat.tcp.push(c);
    }
    nat.tcp_in_with_connect(&[2; 6], &[10, 0, 2, 15], &[127, 0, 0, 1], &segment(199, 0, SYN, &[]), 0, |_| None);
    assert_eq!(nat.tcp.len(), MAX_FLOWS);
    assert!(nat.tcp.iter().all(|c| matches!(c.transport, Transport::TimeWait)));
    let (_tx, rx) = channel();
    nat.tcp_in_with_connect(&[2; 6], &[10, 0, 2, 15], &[127, 0, 0, 1], &segment(199, 0, SYN, &[]), 0, |_| Some(rx));
    assert_eq!(nat.tcp.len(), MAX_FLOWS);
    assert!(matches!(nat.tcp.last().unwrap().transport, Transport::Connecting(_)));
}

#[test]
fn udp_at_capacity_reuses_existing_flows_and_evicts_the_least_recent() {
    let receiver = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    receiver.set_read_timeout(Some(std::time::Duration::from_secs(1))).unwrap();
    let port = receiver.local_addr().unwrap().port();
    let mut nat = Nat::new(false);
    let mut bytes = [0; 16];
    for sport in 0..MAX_FLOWS as u16 {
        nat.udp_out(&[2; 6], &[10, 0, 2, 15], sport, &[127, 0, 0, 1], &[127, 0, 0, 1], port, b"one", sport as u64);
        assert_eq!(receiver.recv_from(&mut bytes).unwrap().0, 3);
    }
    nat.udp_out(&[2; 6], &[10, 0, 2, 15], 0, &[127, 0, 0, 1], &[127, 0, 0, 1], port, b"reuse", 100);
    assert_eq!(receiver.recv_from(&mut bytes).unwrap().0, 5);
    assert_eq!(nat.udp_flows, MAX_FLOWS as u64);
    nat.udp_out(&[2; 6], &[10, 0, 2, 15], 1234, &[127, 0, 0, 1], &[127, 0, 0, 1], port, b"new", 101);
    assert_eq!(receiver.recv_from(&mut bytes).unwrap().0, 3);
    assert_eq!(nat.udp.len(), MAX_FLOWS);
    assert_eq!(nat.udp_evicted, 1);
    assert!(nat.udp.iter().any(|f| f.guest_port == 0));
    assert!(!nat.udp.iter().any(|f| f.guest_port == 1));
    assert!(nat.udp.iter().any(|f| f.guest_port == 1234));
    assert_eq!(nat.udp_send_errors, 0);
}

#[test]
fn short_and_invalid_tcp_headers_are_rejected() {
    let mut nat = Nat::new(false);
    for len in 0..64 {
        for offset in 0..16 {
            let mut seg = vec![0; len];
            if len > 12 { seg[12] = offset << 4; }
            assert!(nat.tcp_in(&[2; 6], &[10, 0, 2, 15], &[127, 0, 0, 1], &seg, 0).is_empty());
        }
    }
    assert!(nat.tcp.is_empty());
}

#[test]
fn udp_ignores_other_senders_and_preserves_dns_reply_address() {
    let resolver = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    resolver.set_read_timeout(Some(std::time::Duration::from_secs(1))).unwrap();
    let intruder = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let mut nat = Nat::new(false);
    nat.udp_out(&[2; 6], &[10, 0, 2, 15], 1234, &[127, 0, 0, 1], &[10, 0, 2, 3], resolver.local_addr().unwrap().port(), b"query", 0);
    let mut bytes = [0; 32];
    let (_, from) = resolver.recv_from(&mut bytes).unwrap();
    intruder.send_to(b"forged", from).unwrap();
    resolver.send_to(b"answer", from).unwrap();
    let frames = poll_ready(&mut nat, 1);
    assert_eq!(frames.len(), 1);
    assert_eq!(&frames[0][26..30], &[10, 0, 2, 3]);
    assert_eq!(&frames[0][42..], b"answer");
}
