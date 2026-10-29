"""Opt-in ATT client for the real guest NimBLE host, using the native live session.

No credentials, application protocol, or authentication bypass lives in this transport.
Raw guest ATT observations stay in the ignored emulator log.
"""
import argparse
from collections import deque
import contextlib
import json
from pathlib import Path
import re
import struct
import time
import uuid

from bicino_live import WebSocket, is_running, session


def word(data, offset=0):
    return struct.unpack_from("<H", data, offset)[0]


def uuid_text(data):
    return str(uuid.UUID(bytes=bytes(reversed(data)))) if len(data) == 16 else f"{word(data):04x}"


class AttError(ValueError):
    def __init__(self, data):
        self.code = data[4] if len(data) == 5 else 0x0e
        super().__init__(f"Guest ATT error: {data.hex()}")


class Guest:
    def __init__(self, state=None, notification=None):
        self.state = state or session()
        if not is_running(self.state):
            raise ValueError("Start a live session first: tools/bicino.py start")
        self.ws = WebSocket(self.state["port"])
        self.log = (Path(self.state["output"]) / "emulator.log").open()
        self.log.seek(0, 2)
        self.notifications = deque()
        self.notification = notification
        self.connected = False
        self.mtu = 23
        self.lines = deque()
        self.partial = ""

    def close(self):
        try:
            if self.connected:
                self.disconnect()
        finally:
            self.log.close()
            self.ws.close()

    def command(self, value):
        self.ws.send({"t": "ble", "command": value})
        # Drain the web connection too: frame delivery must never accumulate indefinitely.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            kind, data = self.ws.receive()
            if kind == 1:
                message = json.loads(data)
                if message.get("t") == "ble-command":
                    if not message["ok"]:
                        raise ValueError(message["error"])
                    return
        raise TimeoutError("No BLE command acknowledgement; rebuild the emulator")

    def next_line(self, deadline):
        while time.monotonic() < deadline:
            if self.lines:
                return self.lines.popleft()
            text = self.log.read()
            if text:
                self.partial += text
                complete = self.partial.split("\n")
                self.partial = complete.pop()
                self.lines.extend(complete)
                continue
            if not is_running(self.state):
                raise EOFError("Live emulator stopped")
            time.sleep(0.01)
        raise TimeoutError("Guest BLE response timed out; see emulator.log and serial.log")

    def wait_line(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            line = self.next_line(deadline)
            if predicate(line):
                return line
            if " disconnected" in line:
                self.connected = False

    def connect(self, mtu=517):
        if self.connected:
            return
        self.command("connect")
        self.wait_line(lambda line: " connected handle=" in line)
        self.connected = True
        requested = min(517, max(23, mtu))
        response = self.request(bytes([2]) + struct.pack("<H", requested))  # ATT MTU 517, guest chooses its own maximum
        if len(response) != 3 or response[0] != 3:
            raise ValueError("Invalid guest MTU response")
        self.mtu = min(requested, max(23, word(response, 1)))

    def disconnect(self):
        if not self.connected:
            return
        try:
            self.command("disconnect")
            self.wait_line(lambda line: " disconnected" in line)
        finally:
            self.connected = False
            self.notifications.clear()

    def packet(self, deadline):
        while True:
            line = self.next_line(deadline)
            if " disconnected" in line:
                self.connected = False
                raise EOFError("Guest disconnected; begin a fresh authenticated session")
            match = re.search(r" att-rx ([0-9a-f]+)$", line)
            if match:
                data = bytes.fromhex(match[1])
                if data[0] in (0x1b, 0x1d):
                    if len(data) < 3:
                        raise ValueError("Truncated ATT notification")
                    event = (word(data, 1), data[3:])
                    if self.notification:
                        self.notification(*event)
                    else:
                        self.notifications.append(event)
                    if data[0] == 0x1d:
                        self.command("att 1e")
                    continue
                return data

    def request(self, data, timeout=10):
        if len(data) > self.mtu and data[0] != 2:
            raise ValueError(f"ATT request exceeds negotiated MTU {self.mtu}")
        self.command("att " + data.hex())
        response = self.packet(time.monotonic() + timeout)
        if response[0] == 1:
            raise AttError(response)
        expected = data[0] + 1
        if response[0] != expected:
            raise ValueError(f"ATT response {response[0]:02x}, expected {expected:02x}")
        return response

    def read(self, handle, offset=0):
        pdu = bytes([0x0c]) + struct.pack("<HH", handle, offset) if offset else bytes([0x0a]) + struct.pack("<H", handle)
        return self.request(pdu)[1:]

    def write(self, handle, value, acknowledged=True):
        if not acknowledged:
            if len(value) > self.mtu - 3:
                raise ValueError("Unacknowledged write exceeds guest MTU; cannot split an application frame")
            self.command("att " + (b"\x52" + struct.pack("<H", handle) + value).hex())
        elif len(value) <= self.mtu - 3:
            self.request(b"\x12" + struct.pack("<H", handle) + value)
        else:
            if len(value) > 512:
                raise ValueError("Attribute values are limited to 512 bytes")
            try:
                for offset in range(0, len(value), self.mtu - 5):
                    pdu = b"\x16" + struct.pack("<HH", handle, offset) + value[offset:offset + self.mtu - 5]
                    if self.request(pdu)[1:] != pdu[1:]:
                        raise ValueError("Guest prepare-write echo differs")
                self.request(b"\x18\x01")
            except Exception:
                with contextlib.suppress(Exception):
                    self.request(b"\x18\x00")
                raise

    def subscribe(self, characteristic, enabled=True):
        ccc = characteristic.get("ccc")
        if not ccc:
            raise ValueError("Guest characteristic has no CCC descriptor")
        self.write(ccc, struct.pack("<H", (1 if characteristic["properties"] & 0x10 else 2) if enabled else 0))

    def wait_notification(self, handle, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            while self.notifications:
                actual, data = self.notifications.popleft()
                if actual == handle:
                    return data
            # packet() queues notifications and normally waits for a response; use a short
            # deadline to return control when the only event was a notification.
            try:
                response = self.packet(min(deadline, time.monotonic() + 0.1))
                raise ValueError(f"Unsolicited ATT response {response.hex()}")
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise

    def discover(self):
        services = []
        start = 1
        while start <= 0xffff:
            try:
                data = self.request(b"\x10" + struct.pack("<HHH", start, 0xffff, 0x2800))
            except AttError as error:
                if error.code == 0x0a:
                    break
                raise
            size = data[1]
            if size not in (6, 20) or (len(data) - 2) % size:
                raise ValueError("Invalid guest service discovery")
            for pos in range(2, len(data), size):
                entry = data[pos:pos + size]
                services.append({"start": word(entry), "end": word(entry, 2), "uuid": uuid_text(entry[4:]), "characteristics": []})
            if not services or services[-1]["end"] < start:
                raise ValueError("Non-progressing service discovery")
            start = services[-1]["end"] + 1
        for service in services:
            start = service["start"]
            chars = service["characteristics"]
            while start <= service["end"]:
                try:
                    data = self.request(b"\x08" + struct.pack("<HHH", start, service["end"], 0x2803))
                except AttError as error:
                    if error.code == 0x0a:
                        break
                    raise
                size = data[1]
                if size not in (7, 21) or (len(data) - 2) % size:
                    raise ValueError("Invalid guest characteristic discovery")
                for pos in range(2, len(data), size):
                    entry = data[pos:pos + size]
                    chars.append({"declaration": word(entry), "properties": entry[2], "handle": word(entry, 3), "uuid": uuid_text(entry[5:])})
                if not chars or chars[-1]["declaration"] < start:
                    raise ValueError("Non-progressing characteristic discovery")
                start = chars[-1]["declaration"] + 1
            for index, char in enumerate(chars):
                start = char["handle"] + 1
                end = chars[index + 1]["declaration"] - 1 if index + 1 < len(chars) else service["end"]
                while start <= end:
                    try:
                        data = self.request(b"\x04" + struct.pack("<HH", start, end))
                    except AttError as error:
                        if error.code == 0x0a:
                            break
                        raise
                    size = 4 if data[1] == 1 else 18 if data[1] == 2 else 0
                    if not size or (len(data) - 2) % size or len(data) == 2:
                        raise ValueError("Invalid guest descriptor discovery")
                    last = 0
                    for pos in range(2, len(data), size):
                        entry = data[pos:pos + size]
                        last = word(entry)
                        if uuid_text(entry[2:]) == "2902":
                            char["ccc"] = last
                    if last < start:
                        raise ValueError("Non-progressing descriptor discovery")
                    start = last + 1
        return services


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["discover", "probe"])
    args = parser.parse_args()
    with contextlib.closing(Guest()) as guest:
        guest.connect()
        services = guest.discover()
        if args.action == "discover":
            print(json.dumps({"mtu": guest.mtu, "services": services}, indent=2))
        else:
            service = next(s for s in services if s["uuid"] == "9d7b3f30-3f6a-4d1c-9f6d-1fbf0e8b1800")
            auth = next(c for c in service["characteristics"] if c["uuid"] == "9d7b3f30-3f6a-4d1c-9f6d-1fbf0e8b1002")
            guest.subscribe(auth)
            guest.write(auth["handle"], b"INFO")
            info = guest.wait_notification(auth["handle"])
            assert info.startswith(b"DEVICE|2|"), info
            guest.disconnect()
            time.sleep(0.2)
            guest.connect()
            guest.subscribe(auth)
            guest.write(auth["handle"], b"INFO")
            assert guest.wait_notification(auth["handle"]) == info
            print(json.dumps({"passed": True, "mtu": guest.mtu, "services": len(services),
                              "info_notification": True, "reconnect_notification": True,
                              "physical_iphone": False, "authenticated": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, EOFError, ValueError, TimeoutError, StopIteration) as error:
        raise SystemExit(f"bicino BLE: {error}") from error
