"""On-demand native Bicino sessions. No third-party Python packages or firmware builds."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time
import zlib

import bicino as capture

LOCAL = capture.LOCAL
SESSION = LOCAL / "session.json"


class WebSocket:
    """Small synchronous loopback client for esp32sim's unfragmented WebSocket ABI."""
    def __init__(self, port, timeout=5):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout)
        self.stream = self.socket.makefile("rb")
        key = base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall((f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                             f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n").encode())
        status = self.stream.readline(4096)
        headers = {}
        for _ in range(64):
            line = self.stream.readline(4096)
            if line == b"\r\n":
                break
            name, _, value = line.decode().partition(":")
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if b" 101 " not in status or headers.get("sec-websocket-accept") != expected:
            self.close()
            raise ValueError("Emulator WebSocket handshake failed")

    def close(self):
        self.stream.close()
        self.socket.close()

    def read(self, size):
        data = self.stream.read(size)
        if len(data) != size:
            raise EOFError("Emulator disconnected")
        return data

    def receive(self):
        while True:
            first, second = self.read(2)
            if not first & 0x80 or second & 0x80:
                raise ValueError("Unsupported WebSocket frame")
            size = second & 127
            if size == 126:
                size = struct.unpack(">H", self.read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self.read(8))[0]
            if size > 8 * 1024 * 1024:
                raise ValueError("Oversized WebSocket frame")
            data = self.read(size)
            opcode = first & 15
            if opcode == 8:
                raise EOFError("Emulator closed the session")
            if opcode == 9:
                self.send_frame(10, data)
            elif opcode in (1, 2):
                return opcode, data

    def send_frame(self, opcode, data):
        size = len(data)
        head = bytes([0x80 | opcode])
        if size < 126:
            head += bytes([0x80 | size])
        elif size < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            raise ValueError("Input too large")
        mask = os.urandom(4)
        self.socket.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, message):
        self.send_frame(1, json.dumps(message).encode())


def write_png(path, message):
    if len(message) < 5 or message[0] != 1:
        raise ValueError("Expected a display frame")
    width, height = struct.unpack_from("<HH", message, 1)
    if (width, height) != (466, 466) or len(message) != 5 + width * height * 2:
        raise ValueError("Expected the mounted 466x466 RGB565 frame")
    raw = bytearray()
    pixels = memoryview(message)[5:]
    for y in range(height):
        raw.append(0)
        for x in range(width):
            color = struct.unpack_from("<H", pixels, (y * width + x) * 2)[0]
            r, g, b = color >> 11, (color >> 5) & 63, color & 31
            raw.extend((r * 255 // 31, g * 255 // 63, b * 255 // 31))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def session():
    return json.loads(SESSION.read_text())


def is_running(state):
    # Check process identity as well as PID: an old session must never signal a reused PID.
    process = subprocess.run(["ps", "-p", str(state["pid"]), "-o", "command="], capture_output=True, text=True)
    return process.returncode == 0 and f"--session {state['id']}" in process.stdout and "bicino_live.py" in process.stdout


def status(state):
    output = Path(state["output"])
    serial = (output / "serial.log").read_text(errors="replace") if (output / "serial.log").exists() else ""
    boot = serial[serial.rfind("BOOT_META "):] if "BOOT_META " in serial else ""
    report = dict(state, running=is_running(state), boot_ready=bool(capture.re.search(r"BOOT_STAGE .*event=ready .*name=ready", boot)),
                  correct_target=bool(capture.re.search(r"BOOT_META .*target=WAVESHARE_AMOLED_175(?: |$)", boot)),
                  ble_initialized="BLE: Server started, advertising" in boot,
                  guest_panic=bool(capture.re.search(r"assert failed:|Guru Meditation|panic'ed|abort\(\) was called|event=safe_mode", serial)))
    manifest = output / "manifest.json"
    report["limitations"] = json.loads(manifest.read_text())["limitations"] if manifest.exists() else capture.LIMITATIONS
    if (output / "live-result.json").exists():
        report["result"] = json.loads((output / "live-result.json").read_text())
    return report


def worker(args):
    # The inherited advisory lock survives start() exiting and is released on worker death.
    lock = os.fdopen(args.lock_fd, "w")
    output = Path(args.output)
    command = json.loads((output / "command.json").read_text())
    state = {"schema": 1, "id": args.session, "pid": os.getpid(), "output": str(output),
             "port": args.port, "url": f"http://127.0.0.1:{args.port}/", "started_at": time.time()}
    stopping = False
    child = None
    def stop(_sig, _frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with (output / "serial.log").open("wb") as serial, (output / "emulator.log").open("wb") as emulator:
            child = subprocess.Popen(command, cwd=capture.ROOT, stdout=serial, stderr=emulator)
            state["emulator_pid"] = child.pid
            capture.save(SESSION, state)
            link = LOCAL / (".live-" + args.session)
            link.symlink_to(output.relative_to(LOCAL), target_is_directory=True)
            os.replace(link, LOCAL / "live")
            while child.poll() is None:
                if stopping:
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                else:
                    time.sleep(0.1)
            capture.save(output / "live-result.json", {"returncode": child.returncode, "stopped_by_user": stopping,
                                                       "ended_at": time.time(), "passed": False,
                                                       "note": "Live session termination is not a passing capture test; use run for bounded checks."})
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        lock.close()


def start(args):
    lock = (LOCAL / "live.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError("A Bicino live session is already running; use status or stop") from None
    # Fail before launching if another application owns the requested port.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", args.port))
    manifest, firmware, rom = capture.prepare(args)
    output = capture.new_output()
    command = capture.command_for(args, firmware, rom, output)
    pos = command.index("--max-seconds")
    del command[pos:pos + 2]
    command.remove("--no-reboot")  # Enable the browser reset button; logs still retain guest faults.
    command += ["--web", str(args.port), "--web-dir", str(capture.ROOT / "web")]
    capture.save(output / "command.json", command)
    (output / "command.txt").write_text(capture.shlex.join(command) + "\n")
    limitations = list(capture.LIMITATIONS)
    if args.full_storage_probe:
        limitations[0] = "SDMMC is unmodeled; real driver timeouts/retries run without the SD failure substitution."
    capture.save(output / "manifest.json", {"firmware": manifest, "emulator_commit": capture.git("rev-parse", "HEAD"),
                 "emulator_dirty": bool(capture.git("status", "--porcelain")),
                 "emulator_binary_sha256": capture.sha(capture.ROOT / "target/release/esp32sim"),
                 "sd_failure_substitution": not args.full_storage_probe, "limitations": limitations})
    worker_command = [sys.executable, str(Path(__file__).resolve()), "--session", output.name,
                      "--output", str(output), "--port", str(args.port), "--lock-fd", str(lock.fileno())]
    with (output / "session.log").open("wb") as log:
        process = subprocess.Popen(worker_command, cwd=capture.ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, pass_fds=(lock.fileno(),))
    lock.close()
    try:
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError(f"Live session exited; see {output / 'session.log'}")
            if SESSION.exists():
                state = session()
                if state["id"] == output.name:
                    report = status(state)
                    if not report["running"]:
                        raise ValueError(f"Emulator exited; see {output / 'emulator.log'}")
                    if report["guest_panic"]:
                        os.kill(state["pid"], signal.SIGTERM)
                        raise ValueError(f"Guest panic; see {output / 'serial.log'}")
                    if report["boot_ready"] and report["correct_target"] and report["ble_initialized"]:
                        try:
                            with contextlib.closing(WebSocket(args.port)) as ws:
                                for _ in range(100):
                                    kind, data = ws.receive()
                                    if kind == 2 and data[:1] == b"\1":
                                        write_png(output / "screen.png", data)
                                        break
                            report["screen"] = capture.screen_info(output / "screen.png")
                            if report["screen"]["distinct_colors"] < 8:
                                time.sleep(0.1)
                                continue
                        except (OSError, EOFError):
                            time.sleep(0.1)
                            continue
                        print(json.dumps(report, indent=2), flush=True)
                        if args.action == "live":
                            try:
                                process.wait()
                            except KeyboardInterrupt:
                                os.kill(state["pid"], signal.SIGTERM)
                                process.wait(timeout=5)
                        return 0
            time.sleep(0.1)
        # A failed start never leaves a forgotten emulator behind.
        raise ValueError(f"Live boot timed out; see {output}")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=6)
        raise


def stop_session(state):
    if is_running(state):
        os.kill(state["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 6
        while is_running(state) and time.monotonic() < deadline:
            time.sleep(0.1)
        if is_running(state):
            raise ValueError("Session did not stop; inspect session.log")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "live", "restart", "stop", "status", "screen", "input"])
    parser.add_argument("message", nargs="?", help="input: JSON web ABI message (touch, btn, serial, ble)")
    parser.add_argument("--port", type=int, default=17500)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--firmware-dir", type=Path)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--script", type=Path, help="Native replay script; guest-time deterministic inputs")
    parser.add_argument("--full-storage-probe", action="store_true")
    parser.add_argument("--emulator-arg", action="append", default=[])
    parser.add_argument("--output", type=Path, help="screen: PNG output path")
    args = parser.parse_args()
    args.seconds = 5  # command builder's bounded default, removed for a live session
    LOCAL.mkdir(parents=True, exist_ok=True)
    try:
        if not 1 <= args.port <= 65535 or not 0 < args.timeout < float("inf"):
            raise ValueError("port must be 1..65535 and timeout must be finite and positive")
        if args.action == "restart" and SESSION.exists():
            stop_session(session())
        if args.action in ("start", "live", "restart"):
            return start(args)
        state = session()
        report = status(state)
        if args.action == "status":
            print(json.dumps(report, indent=2))
            return 0 if report["running"] and not report["guest_panic"] else 1
        if args.action == "stop":
            stop_session(state)
            print(json.dumps(status(state), indent=2))
            return 0
        if not report["running"]:
            raise ValueError("No live session; run tools/bicino.py start")
        with contextlib.closing(WebSocket(state["port"])) as ws:
            if args.action == "input":
                message = json.loads(args.message or "null")
                if not isinstance(message, dict) or message.get("t") not in {"touch", "btn", "serial", "key", "ble"}:
                    raise ValueError("Expected a touch, btn, serial, key or ble JSON message")
                ws.send(message)
                acknowledged = message["t"] != "ble"
                # Wait for a fresh stat: confirms the emulator has advanced, not semantic acceptance.
                while True:
                    kind, data = ws.receive()
                    if kind == 1 and json.loads(data).get("t") == "ble-command":
                        if not json.loads(data)["ok"]:
                            raise ValueError(json.loads(data)["error"])
                        acknowledged = True
                    if kind == 1 and acknowledged and json.loads(data).get("t") == "stat":
                        record = {"guest_seconds_observed": json.loads(data)["time"], "message": message}
                        with (Path(state["output"]) / "inputs.jsonl").open("a") as log:
                            log.write(json.dumps(record) + "\n")
                        print(json.dumps(record))
                        break
            else:
                path = args.output or Path(state["output"]) / "screen.png"
                while True:
                    kind, data = ws.receive()
                    if kind == 2 and data[:1] == b"\1":
                        write_png(path, data)
                        print(json.dumps({"path": str(path.resolve()), "screen": capture.screen_info(path)}))
                        break
        return 0
    except (OSError, EOFError, ValueError, subprocess.SubprocessError) as error:
        print(f"bicino: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if "--session" in sys.argv:
        internal = argparse.ArgumentParser()
        internal.add_argument("--session", required=True)
        internal.add_argument("--output", required=True)
        internal.add_argument("--port", type=int, required=True)
        internal.add_argument("--lock-fd", type=int, required=True)
        worker(internal.parse_args())
    else:
        sys.exit(main())
