#!/usr/bin/env python3
"""Replay ownership-v2 and synthetic navigation against a fresh Bicino emulator.

Requires the optional Python `cryptography` package. Never targets a physical board,
never imports an owner's credential, and never skips the firmware's pairing checks.
The only simulated confirmation is an explicit GPIO0 press after the pairing UI.
"""
import contextlib
import hashlib
import hmac
import json
import re
from pathlib import Path
import struct
import sys
import time

from bicino_ble import Guest
from bicino_live import write_png


def main():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    checks = {}
    completed = False
    with contextlib.closing(Guest()) as guest:
        output = Path(guest.state["output"])
        def check(name, condition):
            checks[name] = bool(condition)
            print(name, "PASS" if condition else "FAIL", flush=True)
            if not condition:
                raise ValueError(name)
        def snapshot(name):
            for _ in range(100):
                kind, data = guest.ws.receive()
                if kind == 2 and data[:1] == b"\1":
                    write_png(output / (name + ".png"), data)
                    return
            # A late join always contains the current frame, even if it has not changed.
            from bicino_live import WebSocket
            with contextlib.closing(WebSocket(guest.state["port"])) as ws:
                while True:
                    kind, data = ws.receive()
                    if kind == 2 and data[:1] == b"\1":
                        write_png(output / (name + ".png"), data)
                        return
        def wait_serial(pattern, timeout=90):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                serial = (output / "serial.log").read_text(errors="replace")
                if re.search(pattern, serial):
                    return True
                guest.ws.receive()
            return False
        try:
            guest.connect(185)
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
            confirm = f"CONFIRM|{owner}|{mac(b'claim|' + transcript)}|{b'Sim test'.hex()}"
            check("confirmation_required", auth_send(confirm) == "ERROR|physical_confirmation_required")
            # Real UI/render and button handling run; only the input source is simulated.
            time.sleep(1)
            snapshot("pairing")
            guest.ws.send({"t": "btn", "pin": 0, "v": 1})
            time.sleep(0.15)
            guest.ws.send({"t": "btn", "pin": 0, "v": 0})
            check("button_confirmation", guest.wait_notification(auth["handle"], 10).decode() == "PAIR_READY|" + device)
            check("paired", auth_send(confirm).startswith("PAIRED|" + device + "|"))
            def authenticate(client_nonce):
                server = auth_send(f"OWNER|{owner}|{client_nonce}").split("|")
                check("server_proof_" + client_nonce[:2], server[:3] == ["SERVER2", device, client_nonce] and
                      hmac.compare_digest(server[4], mac(f"server2|{device}|{owner}|{client_nonce}|{server[3]}".encode())))
                server_nonce = server[3]
                proof = mac(f"client2|{device}|{owner}|{client_nonce}|{server_nonce}".encode())
                check("authenticated_" + client_nonce[:2], auth_send(f"PROOF|{owner}|{client_nonce}|{server_nonce}|{proof}") == f"OK2|{device}|{client_nonce}|{server_nonce}")
                return hmac.new(key, f"session2-write|{device}|{client_nonce}|{server_nonce}".encode(), hashlib.sha256).digest()
            write_key = authenticate("11" * 16)
            seq = {}
            def protected(channel, plaintext):
                seq[channel] = seq.get(channel, 0) + 1
                serial = struct.pack(">I", seq[channel])
                return b"S2" + serial + AESGCM(write_key).encrypt(bytes([channel]) + bytes(7) + serial, plaintext, b"write2|" + bytes([channel]) + serial)
            before = (output / "serial.log").read_text(errors="replace")
            guest.write(nav["handle"], protected(2, b"1|120|Turn right at the test junction"))
            guest.write(route["handle"], protected(3, struct.pack("<iihhhh", 0, 0, 100, 0, 0, 100)))
            gps_frame = protected(4, struct.pack("<iiH", 0, 0, 90))
            guest.write(gps["handle"], gps_frame)
            guest.write(settings["handle"], protected(5, struct.pack("<Bi", 11, 1)))
            time.sleep(0.4)
            snapshot("navigation")
            guest.write(gps["handle"], gps_frame)  # Replay must not become a second GPS update.
            time.sleep(0.2)
            delta = (output / "serial.log").read_text(errors="replace")[len(before):]
            (output / "scenario-serial.log").write_text(delta)
            check("protected_writes_applied", wait_serial(r"BLE Debug: [^\n]*auth=1 [^\n]*nav=1 route=1 gps=1 settings=1"))
            check("protected_replay_rejected", "Rejected GPS" in delta or "rejected authenticated GPS" in delta or "invalid authenticated GPS" in delta)
            guest.disconnect()
            guest.connect(185); guest.subscribe(auth); guest.subscribe(nav)
            write_key = authenticate("22" * 16); seq.clear()
            guest.write(gps["handle"], protected(4, struct.pack("<iiH", 100, 100, 180)))
            time.sleep(0.3)
            snapshot("reconnected")
            check("reconnect_protected_gps_applied", wait_serial(r"BLE Debug: [^\n]*auth=1 [^\n]*authOK=2 [^\n]*nav=1 route=1 gps=2 settings=1"))
            completed = True
        finally:
            report = {"schema": 1, "passed": completed and bool(checks) and all(checks.values()), "checks": checks,
                      "physical_iphone": False, "physical_board": False,
                      "sd_maps": False, "note": "Synthetic ATT client; real guest ownership handlers. SD failure substitution and FFat fallback remain."}
            (output / "scenario-result.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, EOFError, ValueError, TimeoutError, StopIteration, ImportError) as error:
        print(f"bicino scenario: {error}", file=sys.stderr)
        sys.exit(1)
