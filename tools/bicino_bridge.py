#!/usr/bin/env python3
"""macOS BLE peripheral proxy for a live Bicino guest. Opt-in; never builds firmware.

The proxy advertises a distinct BikeComputer SIM name and only the guest's application
service. It forwards ATT operations and notifications, without interpreting or replacing
ownership-v2 authentication. Do not run while another task uses the physical iPhone.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import plistlib
import selectors
import subprocess
import sys
import time

import bicino
from bicino_ble import AttError, Guest

SERVICE = "9d7b3f30-3f6a-4d1c-9f6d-1fbf0e8b1800"


def build():
    if sys.platform != "darwin":
        raise ValueError("The physical peripheral helper requires macOS and CoreBluetooth")
    source = bicino.ROOT / "tools/bicino/Peripheral.swift"
    app = bicino.LOCAL / "BicinoBridge.app"
    contents = app / "Contents"
    binary = contents / "MacOS/BicinoBridge"
    binary.parent.mkdir(parents=True, exist_ok=True)
    info = {"CFBundleIdentifier": "com.bicino.esp32sim.bridge", "CFBundleName": "Bicino Emulator Bridge",
            "CFBundleExecutable": "BicinoBridge", "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
            "NSBluetoothAlwaysUsageDescription": "Expose an emulated Bicino firmware to the development iPhone over Bluetooth.",
            "NSBluetoothPeripheralUsageDescription": "Expose the emulated Bicino firmware for development."}
    plist = plistlib.dumps(info)
    digest = bicino.sha(source)
    marker = contents / "source.sha256"
    if binary.is_file() and marker.exists() and marker.read_text().strip() == digest and (contents / "Info.plist").read_bytes() == plist:
        return binary
    (contents / "Info.plist").write_bytes(plist)
    subprocess.run(["swiftc", "-O", str(source), "-o", str(binary)], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True, capture_output=True)
    marker.write_text(digest + "\n")
    return binary


def run(binary):
    with (bicino.LOCAL / "ble.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another Bicino BLE bridge owns the guest peer") from None
        with contextlib.closing(Guest()) as guest:
            guest.connect()
            services = guest.discover()
            service = next(s for s in services if s["uuid"] == SERVICE)
            guest.disconnect()
            chars = {c["handle"]: c for c in service["characteristics"]}
            output = Path(guest.state["output"])
            bicino.save(output / "bridge-gatt.json", {"mtu": guest.mtu, "service": service})
            with (output / "bridge.log").open("a", buffering=1) as log:
                process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1)
                def send(message):
                    process.stdin.write(json.dumps(message) + "\n")
                    process.stdin.flush()
                def notify(handle, value):
                    send({"t": "notify", "handle": handle, "value": value.hex()})
                    log.write(json.dumps({"event": "notification", "handle": handle, "bytes": len(value)}) + "\n")
                guest.notification = notify
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                try:
                    send({"t": "configure", "service": service, "name": "BikeComputer SIM"})
                    print(f"Bridge starting; approve macOS Bluetooth access if prompted. Log: {output / 'bridge.log'}", flush=True)
                    subscribed = set()
                    while process.poll() is None:
                        for key, _ in selector.select(0.05):
                            line = key.fileobj.readline()
                            if not line:
                                break
                            message = json.loads(line)
                            kind = message.get("t")
                            log.write(json.dumps({k: v for k, v in message.items() if k not in {"value", "central"}}) + "\n")
                            if kind in {"bluetooth", "advertising", "fatal"}:
                                print(json.dumps(message), flush=True)
                            if kind == "fatal":
                                raise ValueError(message.get("message", "CoreBluetooth failed"))
                            if kind == "disconnect":
                                guest.disconnect(); subscribed.clear()
                                send({"t": "disconnected"})
                                continue
                            if kind not in {"read", "write", "subscribe", "unsubscribe"}:
                                continue
                            handle = message.get("handle")
                            reply = {"t": "reply", "id": message.get("id"), "error": 0}
                            try:
                                if handle not in chars:
                                    raise ValueError("Unknown guest attribute")
                                if kind == "unsubscribe":
                                    if guest.connected and handle in subscribed:
                                        guest.subscribe(chars[handle], False)
                                    subscribed.discard(handle)
                                    continue
                                guest.connect(message.get("mtu", 185))
                                if kind == "subscribe":
                                    guest.subscribe(chars[handle]); subscribed.add(handle)
                                    continue
                                if kind == "read":
                                    reply["value"] = guest.read(handle, message.get("offset", 0)).hex()
                                else:
                                    guest.write(handle, bytes.fromhex(message.get("value", "")), acknowledged=bool(chars[handle]["properties"] & 8))
                            except AttError as error:
                                reply["error"] = error.code
                            except (OSError, EOFError, ValueError, TimeoutError) as error:
                                reply["error"] = 0x0e
                                log.write(json.dumps({"event": "request-failed", "message": str(error)}) + "\n")
                                if kind in {"subscribe", "unsubscribe"}:
                                    raise
                            if kind in {"read", "write"}:
                                send(reply)
                        if guest.connected:
                            try:
                                unexpected = guest.packet(time.monotonic() + 0.01)
                                raise ValueError(f"Unexpected ATT response {unexpected.hex()}")
                            except TimeoutError:
                                pass
                    if process.wait() != 0:
                        raise ValueError("CoreBluetooth helper exited unsuccessfully; see bridge.log")
                finally:
                    selector.close()
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                    process.stdin.close(); process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true", help="Compile/sign the macOS helper without using Bluetooth")
    args = parser.parse_args()
    try:
        binary = build()
        if args.build_only:
            print(binary)
        else:
            run(binary)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, EOFError, ValueError, TimeoutError, StopIteration, subprocess.SubprocessError) as error:
        print(f"bicino bridge: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
