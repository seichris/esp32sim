#!/usr/bin/env python3
"""Replay ownership-v2 and synthetic navigation against a fresh Bicino emulator.

Requires the optional Python `cryptography` package. Never targets a physical board,
never imports an owner's credential, and never skips the firmware's pairing checks.
The only simulated confirmation is an explicit GPIO0 press after the pairing UI.
"""
import argparse
import contextlib
import hashlib
import hmac
import json
import re
from pathlib import Path
import struct
import sys
import time

import bicino as capture
from bicino_ble import Guest
from bicino_live import write_png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirmation-delay", type=float, default=1,
                        help="Modeled seconds before confirming the code (0 to 100; default 1)")
    parser.add_argument("--abandon", choices=["connected", "registered"],
                        help="Negative control: send nothing more after connecting (or after PAIRED) and require "
                             "the guest to drop the unauthenticated link about 12 modeled seconds later")
    parser.add_argument("--position", default="0,0",
                        help="LAT,LON in degrees for the route and GPS (default 0,0); put it inside an SD map to render one")
    args = parser.parse_args()
    if not 0 <= args.confirmation_delay <= 100:
        parser.error("--confirmation-delay must be between 0 and 100 modeled seconds")
    try:
        lat, lon = (round(float(v) * 1_000_000) for v in args.position.split(","))
    except ValueError:
        parser.error("--position expects LAT,LON")
    if not (-90_000_000 <= lat <= 90_000_000 - 100 and -180_000_000 <= lon <= 180_000_000 - 100):
        parser.error("--position is outside WGS-84 bounds")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    checks = {}
    completed = False
    storage = {"mode": "no-card-substitution"}
    abandon_result = {}
    with contextlib.closing(Guest()) as guest:
        output = Path(guest.state["output"])
        if (output / "manifest.json").exists():
            storage = json.loads((output / "manifest.json").read_text()).get("storage", storage)
        def check(name, condition):
            checks[name] = bool(condition)
            print(name, "PASS" if condition else "FAIL", flush=True)
            if not condition:
                raise ValueError(name)
        def snapshot(name):
            from bicino_live import WebSocket
            with contextlib.closing(WebSocket(guest.state["port"])) as ws:
                while True:
                    kind, data = ws.receive()
                    if kind == 2 and data[:1] == b"\1":
                        write_png(output / (name + ".png"), data)
                        return
        def advance_guest(seconds):
            target = None
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                kind, data = guest.ws.receive()
                if kind == 1:
                    message = json.loads(data)
                    if message.get("t") == "stat":
                        if target is None:
                            target = message["time"] + seconds
                        elif message["time"] >= target:
                            return
            raise TimeoutError("Guest clock did not advance")
        def guest_time():
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                kind, data = guest.ws.receive()
                if kind == 1 and (message := json.loads(data)).get("t") == "stat":
                    return message["time"]
            raise TimeoutError("Guest clock did not advance")
        def abandoned(since):
            """Send nothing; the modeled seconds from `since` until the guest drops the link."""
            offset = len((output / "serial.log").read_text(errors="replace"))
            now = since
            while now - since < 30:
                now = guest_time()
                if "BLE: Disconnecting unauthenticated client after timeout" in (output / "serial.log").read_text(errors="replace")[offset:]:
                    guest.wait_line(lambda line: " disconnected" in line)
                    guest.connected = False
                    return now - since
            return None
        def wait_serial(pattern, timeout=90):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                serial = (output / "serial.log").read_text(errors="replace")
                if re.search(pattern, serial):
                    return True
                guest.ws.receive()
            return False
        def emulator_log():
            return (output / "emulator.log").read_text(errors="replace")
        try:
            if storage["mode"] == "sd-card":
                boot = (output / "serial.log").read_text(errors="replace")
                for name, ok in capture.sd_checks(boot, storage).items():
                    check(name, ok)
            guest.connect(185)
            connected_at = guest_time()
            check("mtu_185", guest.mtu == 185)
            service = next(s for s in guest.discover() if s["uuid"] == "9d7b3f30-3f6a-4d1c-9f6d-1fbf0e8b1800")
            chars = {c["uuid"]: c for c in service["characteristics"]}
            auth = chars["9d7b3f30-3f6a-4d1c-9f6d-1fbf0e8b1002"]
            nav, gps, route, settings = (chars[u] for u in ("2a6e", "2a72", "2a6f", "2a73"))
            guest.subscribe(auth); guest.subscribe(nav)
            def auth_send(value):
                guest.write(auth["handle"], value.encode())
                return guest.wait_notification(auth["handle"], timeout=30).decode()
            info = auth_send("INFO").split("|")
            check("fresh_unclaimed_guest", info[:2] == ["DEVICE", "2"] and info[3] == "0")
            if args.abandon == "connected":
                dropped = abandoned(connected_at)
                abandon_result["seconds_to_drop"] = dropped
                check("abandoned_link_dropped_after_window", dropped is not None and 11 <= dropped <= 14)
                completed = True
                return 0
            device = info[2]
            before = (output / "serial.log").read_text(errors="replace")
            guest.write(gps["handle"], struct.pack("<iiH", 0, 0, 90))
            time.sleep(0.2)
            rejected = (output / "serial.log").read_text(errors="replace")[len(before):]
            check("unauthenticated_gps_rejected", "BLE: Rejected GPS characteristic:" in rejected)
            owner = "aa" * 16  # Public synthetic test identity; no real owner credential.
            private = ec.derive_private_key(17, ec.SECP256R1())
            public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            pairing = auth_send(f"PAIR|{owner}|{public.hex()}").split("|")
            check("pairing_reply", pairing[:2] == ["PAIRING", device])
            peer_public = bytes.fromhex(pairing[2])
            transcript = hashlib.sha256(b"BikeComputer ownership v2" + bytes.fromhex(device + owner) + public + peer_public).digest()
            shared = private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_public))
            key = HKDF(algorithm=hashes.SHA256(), length=32, salt=transcript, info=b"BikeComputer owner key v2").derive(shared)
            def mac(value):
                return hmac.new(key, value, hashlib.sha256).hexdigest()
            comparison = int.from_bytes(hmac.new(key, b"compare|" + transcript, hashlib.sha256).digest()[:4], "big") % 1000000
            print(f"Synthetic comparison code: {comparison:06d}", flush=True)
            confirm = f"CONFIRM|{owner}|{mac(b'claim|' + transcript)}|{b'Sim test'.hex()}"
            advance_guest(args.confirmation_delay)
            snapshot("pairing-code")
            check("confirmation_required", auth_send(confirm) == "ERROR|physical_confirmation_required")
            # Real UI/render and button handling run; only the input source is simulated.
            advance_guest(0.2)
            snapshot("pairing")
            guest.ws.send({"t": "btn", "pin": 0, "v": 1})
            advance_guest(0.15)
            guest.ws.send({"t": "btn", "pin": 0, "v": 0})
            check("button_confirmation", guest.wait_notification(auth["handle"], 10).decode() == "PAIR_READY|" + device)
            check("paired", auth_send(confirm).startswith("PAIRED|" + device + "|"))
            if args.abandon == "registered":
                # Registration restarts the unauthenticated window: a registered client that never
                # authenticates keeps the link for the full window, then loses it.
                dropped = abandoned(guest_time())
                abandon_result["seconds_to_drop"] = dropped
                check("registered_link_dropped_after_window", dropped is not None and 11 <= dropped <= 14)
                completed = True
                return 0
            def authenticate(client_nonce):
                server = auth_send(f"OWNER|{owner}|{client_nonce}").split("|")
                check("server_proof_" + client_nonce[:2], server[:3] == ["SERVER2", device, client_nonce] and
                      hmac.compare_digest(server[4], mac(f"server2|{device}|{owner}|{client_nonce}|{server[3]}".encode())))
                server_nonce = server[3]
                proof = mac(f"client2|{device}|{owner}|{client_nonce}|{server_nonce}".encode())
                check("authenticated_" + client_nonce[:2], auth_send(f"PROOF|{owner}|{client_nonce}|{server_nonce}|{proof}") == f"OK2|{device}|{client_nonce}|{server_nonce}")
                return tuple(hmac.new(key, f"session2-{direction}|{device}|{client_nonce}|{server_nonce}".encode(), hashlib.sha256).digest() for direction in ("write", "notify"))
            write_key, notify_key = authenticate("11" * 16)
            seq = {}
            def protected(channel, plaintext):
                seq[channel] = seq.get(channel, 0) + 1
                serial = struct.pack(">I", seq[channel])
                return b"S2" + serial + AESGCM(write_key).encrypt(bytes([channel]) + bytes(7) + serial, plaintext, b"write2|" + bytes([channel]) + serial)
            def encrypted_info(label):
                guest.write(auth["handle"], protected(1, b"INFO"))
                frame = guest.wait_notification(auth["handle"])
                check("encrypted_notification_" + label, frame[:2] == b"R2" and len(frame) >= 22)
                serial = frame[2:6]
                plaintext = AESGCM(notify_key).decrypt(b"\x01" + bytes(7) + serial, frame[6:], b"notify2|\x01" + serial)
                check("notification_proof_" + label, plaintext.startswith(f"DEVICE|2|{device}|1|".encode()))
            encrypted_info("initial")
            before = (output / "serial.log").read_text(errors="replace")
            sd_offset = len(emulator_log())
            guest.write(nav["handle"], protected(2, b"1|120|Turn right at the test junction"))
            guest.write(route["handle"], protected(3, struct.pack("<iihhhh", lat, lon, 100, 0, 0, 100)))
            gps_frame = protected(4, struct.pack("<iiH", lat, lon, 90))
            guest.write(gps["handle"], gps_frame)
            guest.write(settings["handle"], protected(5, struct.pack("<Bi", 11, 0)))
            time.sleep(0.4)
            guest.write(gps["handle"], gps_frame)  # Replay must not become a second GPS update.
            time.sleep(0.2)
            delta = (output / "serial.log").read_text(errors="replace")[len(before):]
            (output / "scenario-serial.log").write_text(delta)
            check("protected_writes_applied", wait_serial(r"BLE Debug: [^\n]*auth=1 [^\n]*nav=1 route=1 gps=1 settings=1"))
            if storage.get("active_map"):
                # The render worker reads the blocks around the new position from the card.
                deadline = time.monotonic() + 90
                reads = {}
                while time.monotonic() < deadline:
                    reads = capture.sd_reads(emulator_log()[sd_offset:], storage.get("files", []))
                    if any(path.endswith(".fmb") for path in reads):
                        break
                    guest.ws.receive()
                storage["reads_after_gps"] = reads
                check("map_blocks_read_after_gps", any(path.endswith(".fmb") for path in reads))
                # The firmware's own status line: the renderer holds blocks of the active map.
                deadline, found = time.monotonic() + 90, None
                while time.monotonic() < deadline and not found:
                    found = re.search(r"SYS: [^\n]*mapFound=1 mapBlocks=([1-9]\d*)", (output / "serial.log").read_text(errors="replace")[len(before):])
                    if not found:
                        guest.ws.receive()
                check("map_found_reported", found)
            snapshot("map-sd" if storage.get("active_map") else "map-no-data")
            check("protected_replay_rejected", "Rejected GPS" in delta or "rejected authenticated GPS" in delta or "invalid authenticated GPS" in delta)
            serial = (output / "serial.log").read_text(errors="replace")
            tiles = re.findall(r"SYS: [^\n]*screen=main tile=(\w+)", serial)
            check("main_screen_visible", bool(tiles))
            previous_tile = tiles[-1]
            offset = len(serial)
            guest.ws.send({"t": "btn", "pin": 0, "v": 1}); advance_guest(0.15)
            guest.ws.send({"t": "btn", "pin": 0, "v": 0})
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                changed = re.findall(r"SYS: [^\n]*screen=main tile=(\w+)", (output / "serial.log").read_text(errors="replace")[offset:])
                if changed:
                    break
                guest.ws.receive()
            else:
                check("screen_button_cycle", False)
            check("screen_button_cycle", changed[-1] != previous_tile)
            print(f"Modeled BOOT screen transition: {previous_tile} -> {changed[-1]}", flush=True)
            snapshot("page-after-button")
            guest.disconnect()
            guest.connect(185); guest.subscribe(auth); guest.subscribe(nav)
            write_key, notify_key = authenticate("22" * 16); seq.clear()
            encrypted_info("reconnect")
            guest.write(gps["handle"], protected(4, struct.pack("<iiH", lat + 100, lon + 100, 180)))
            time.sleep(0.3)
            check("reconnect_protected_gps_applied", wait_serial(r"BLE Debug: [^\n]*auth=1 [^\n]*authOK=2 [^\n]*nav=1 route=1 gps=2 settings=1"))
            snapshot("reconnected")
            serial = (output / "serial.log").read_text(errors="replace")
            check("no_guest_panic", not re.search(r"assert failed:|Guru Meditation|panic'ed|abort\(\) was called", serial))
            check("single_boot", serial.count("BOOT_META ") == 1)
            completed = True
        finally:
            passed = completed and bool(checks) and all(checks.values())
            notes = {"no-card-substitution": "SD failure substitution and FFat fallback remain.",
                     "empty-socket": "Empty modelled SD socket: the real driver finds no card; FFat fallback.",
                     "sd-card": "Modelled SD card through the real SDMMC driver; map from the image's active selection."}
            report = {"schema": 1, "passed": passed, "checks": checks,
                      "confirmation_delay_seconds": args.confirmation_delay, "position_microdegrees": [lat, lon],
                      "abandon": args.abandon, **({"abandon_seconds_to_drop": abandon_result.get("seconds_to_drop")} if args.abandon else {}),
                      "physical_iphone": False, "physical_board": False,
                      "storage": {k: v for k, v in storage.items() if k != "files"},
                      "sd_maps": passed and storage["mode"] == "sd-card" and bool(storage.get("active_map")),
                      "note": "Synthetic ATT client; real guest ownership handlers. " + notes[storage["mode"]]}
            (output / "scenario-result.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, EOFError, ValueError, TimeoutError, StopIteration, ImportError) as error:
        print(f"bicino scenario: {error}", file=sys.stderr)
        sys.exit(1)
