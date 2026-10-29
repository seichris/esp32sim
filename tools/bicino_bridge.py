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
import shutil
import signal
import subprocess
import sys
import tempfile
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
    marker = bicino.LOCAL / "bridge-source.sha256"
    if binary.is_file() and marker.exists() and marker.read_text().strip() == digest and (contents / "Info.plist").is_file() and (contents / "Info.plist").read_bytes() == plist:
        verified = subprocess.run(["codesign", "--verify", "--strict", str(app)], capture_output=True)
        if verified.returncode == 0:
            return binary
    # A completed bundle is signed before publication. Keep the cache marker outside:
    # writing a new Contents file after signing invalidates the resource envelope.
    with tempfile.TemporaryDirectory(dir=bicino.LOCAL, prefix=".bridge-build-") as temporary:
        staged = Path(temporary) / "BicinoBridge.app"
        staged_contents = staged / "Contents"
        staged_binary = staged_contents / "MacOS/BicinoBridge"
        staged_binary.parent.mkdir(parents=True)
        (staged_contents / "Info.plist").write_bytes(plist)
        subprocess.run(["swiftc", "-O", str(source), "-o", str(staged_binary)], check=True)
        for command in (["codesign", "--force", "--sign", "-", str(staged)],
                        ["codesign", "--verify", "--strict", str(staged)]):
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise ValueError(result.stderr.strip() or "macOS helper signature failed")
        previous = Path(temporary) / "previous.app"
        if app.exists():
            os.replace(app, previous)
        try:
            os.replace(staged, app)
        except OSError:
            if previous.exists():
                os.replace(previous, app)
            raise
    marker.write_text(digest + "\n")
    return binary


@contextlib.contextmanager
def native_helper(binary, output, log):
    # LaunchServices gives CoreBluetooth the helper's own app identity. A direct
    # child inherits the terminal/agent's Bluetooth permission; launchd also
    # cannot start an app or open its streams inside protected Documents paths.
    source = binary.parents[2]
    cache = Path.home() / "Library/Caches/com.bicino.esp32sim.bridge" / (bicino.sha(binary) + "-" + bicino.sha(source / "Contents/Info.plist")[:16])
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    app = cache / source.name
    if not app.exists():
        with tempfile.TemporaryDirectory(dir=cache, prefix=".publish-") as staging:
            staged = Path(staging) / source.name
            shutil.copytree(source, staged)
            subprocess.run(["codesign", "--verify", "--strict", str(staged)], check=True, capture_output=True)
            os.replace(staged, app)
    if bicino.sha(app / "Contents/MacOS/BicinoBridge") != bicino.sha(binary) or (app / "Contents/Info.plist").read_bytes() != (source / "Contents/Info.plist").read_bytes():
        raise ValueError("Cached native helper does not match the built bundle")
    subprocess.run(["codesign", "--verify", "--strict", str(app)], check=True, capture_output=True)
    # Private FIFOs retain the existing JSON protocol without a TCP listener or
    # an installed background service. Both launchd and the helper can open
    # these paths without granting the app access to the firmware workspace.
    with tempfile.TemporaryDirectory(prefix="bicino-ble-") as temporary:
        incoming, outgoing, native_log = (Path(temporary) / name for name in ("input", "output", "stderr"))
        for path in (incoming, outgoing):
            os.mkfifo(path, 0o600)
        with os.fdopen(os.open(incoming, os.O_RDWR), "w", buffering=1) as writer, os.fdopen(os.open(outgoing, os.O_RDWR), "rb", buffering=0) as reader:
            process = subprocess.Popen(["open", "-W", "-n", "-a", str(app), "--stdin", str(incoming), "--stdout", str(outgoing), "--stderr", str(native_log)], stdout=subprocess.DEVNULL, stderr=log)
            process.stdin, process.stdout, process.native_pid = writer, reader, None
            try:
                yield process
            finally:
                with contextlib.suppress(OSError, ValueError):
                    writer.write('{"t":"shutdown"}\n'); writer.flush()
                writer.close()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    if process.native_pid is not None:
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(process.native_pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                if native_log.exists():
                    shutil.copyfile(native_log, output / "bridge-native.log")


def native_messages(selector, pending):
    # TextIO.readline() can prefetch several events, leaving later lines in a
    # user-space buffer after the descriptor stops being readable. Drain every
    # complete JSON line from each unbuffered read, including coalesced writes.
    for key, _ in selector.select(0.05):
        chunk = os.read(key.fd, 65536)
        if not chunk:
            raise EOFError("Native helper transport closed")
        pending.extend(chunk)
        if len(pending) > 131072:
            raise ValueError("Native helper message exceeds transport limit")
        while (end := pending.find(b"\n")) >= 0:
            line = bytes(pending[:end]); del pending[:end + 1]
            yield json.loads(line)


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
            with (output / "bridge.log").open("a", buffering=1) as log, native_helper(binary, output, log) as process:
                started = time.monotonic()
                def record(message):
                    log.write(json.dumps({**message, "host_elapsed_s": round(time.monotonic() - started, 6)}) + "\n")
                def send(message):
                    process.stdin.write(json.dumps(message) + "\n")
                    process.stdin.flush()
                def notify(handle, value):
                    send({"t": "notify", "handle": handle, "value": value.hex()})
                    record({"event": "notification", "handle": handle, "bytes": len(value)})
                guest.notification = notify
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                try:
                    send({"t": "configure", "service": service, "name": "BikeComputer SIM"})
                    print(f"Bridge starting; approve macOS Bluetooth access if prompted. Log: {output / 'bridge.log'}", flush=True)
                    subscribed = set()
                    native_pending = bytearray()
                    while process.poll() is None:
                        for message in native_messages(selector, native_pending):
                            kind = message.get("t")
                            record({k: v for k, v in message.items() if k not in {"value", "central", "pid"}})
                            if kind == "started":
                                pid = message.get("pid")
                                if not isinstance(pid, int) or pid < 2:
                                    raise ValueError("Native helper returned an invalid process identity")
                                process.native_pid = pid
                                continue
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
                                record({"event": "request-failed", "message": str(error)})
                                if kind in {"subscribe", "unsubscribe"}:
                                    raise
                            if kind in {"read", "write"}:
                                record({"event": "reply", "id": reply["id"], "error": reply["error"]})
                                send(reply)
                        if guest.connected:
                            try:
                                unexpected = guest.packet(time.monotonic() + 0.01)
                                raise ValueError(f"Unexpected ATT response {unexpected.hex()}")
                            except TimeoutError:
                                pass
                    raise ValueError("CoreBluetooth helper exited unexpectedly; see bridge.log")
                finally:
                    selector.close()


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
