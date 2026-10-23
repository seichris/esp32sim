#!/usr/bin/env python3
"""Bicino 1.75 emulator capture. Standard library only; never builds/flashes firmware."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local" / "bicino"
FILES = ("bootloader.bin", "partitions.bin", "firmware.bin", "firmware.elf")
ROM_SHA = "c0ce0f338d1de1bdc6efbef1591779a2a42c1ab7d759d3c6ae8ae63a7dd34cfd"
LIMITATIONS = [
    "Storage::initSD is substituted with ESP_FAIL; real FFat fallback runs. No SD maps.",
    "Virtual BLE controller; no real iPhone, RF, pairing or throughput validation.",
    "AXP2101, RTC, TCA9554 and codec initialization stubs; no power/reset fidelity.",
    "No GPS or IMU; single-contact touch only. Screen is the mounted Bicino orientation.",
    "Timing is emulator wall time, not physical ESP32 performance or battery consumption.",
]


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def git(*args, directory=ROOT):
    result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def save(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name, delete=False) as pending:
        pending.write(json.dumps(value, indent=2) + "\n")
    os.replace(pending.name, path)


def import_firmware(directory):
    source = directory.expanduser().resolve()
    missing = [name for name in FILES if not (source / name).is_file()]
    if missing:
        raise ValueError(f"Missing firmware files in {source}: {', '.join(missing)}")
    hashes = {name: sha(source / name) for name in FILES}
    key = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16]
    destination = LOCAL / "inputs" / key
    destination.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        if not (destination / name).exists() or sha(destination / name) != hashes[name]:
            # APFS snapshots share immutable file extents instead of duplicating
            # a 50 MB ELF on every import. Publish only a complete verified file.
            pending = destination / (name + ".pending")
            cloned = sys.platform == "darwin" and subprocess.run(
                ["/bin/cp", "-c", str(source / name), str(pending)], capture_output=True).returncode == 0
            if not cloned:
                shutil.copyfile(source / name, pending)
            if sha(pending) != hashes[name]:
                raise ValueError(f"Firmware changed during snapshot: {name}")
            os.replace(pending, destination / name)
        if sha(destination / name) != hashes[name]:
            raise ValueError(f"Firmware changed during snapshot, or corrupt snapshot: {name}")
    manifest = {"directory": str(destination), "source_directory": str(source),
                "source_git_commit": git("rev-parse", "HEAD", directory=source), "sha256": hashes}
    save(LOCAL / "config.json", manifest)
    return manifest


def build():
    with (LOCAL / "build.log").open("w") as log:
        result = subprocess.run(["cargo", "+stable", "build", "--release", "-p", "esp32sim", "--bin", "esp32sim"],
                                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise ValueError(f"Emulator build failed; see {LOCAL / 'build.log'}")


def screen_info(path):
    """Validate esp32sim's RGB8/filter-0 PNG and reject a blank capture."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Screenshot is not a PNG")
    offset, compressed, size = 8, bytearray(), None
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", payload)
            if (width, height, depth, color, interlace) != (466, 466, 8, 2, 0):
                raise ValueError("Expected a 466x466 RGB8 screen")
            size = (width, height)
        elif kind == b"IDAT":
            compressed.extend(payload)
        offset += length + 12
    if size is None:
        raise ValueError("PNG has no dimensions")
    raw = zlib.decompress(compressed)
    stride = size[0] * 3 + 1
    if len(raw) != stride * size[1] or any(raw[y * stride] for y in range(size[1])):
        raise ValueError("Unsupported PNG scanlines")
    colors = set()
    for y in range(size[1]):
        row = raw[y * stride + 1:(y + 1) * stride]
        colors.update(row[i:i + 3] for i in range(0, len(row), 3))
    return {"width": size[0], "height": size[1], "distinct_colors": len(colors), "sha256": sha(path)}


def run(args):
    manifest = import_firmware(args.firmware_dir) if args.firmware_dir else json.loads((LOCAL / "config.json").read_text())
    firmware = Path(manifest["directory"])
    for name, digest in manifest["sha256"].items():
        if sha(firmware / name) != digest:
            raise ValueError(f"Firmware snapshot hash mismatch: {name}")
    rom = ROOT / ".local" / "roms" / "esp32s3_rev0_rom.elf"
    if not rom.is_file() or sha(rom) != ROM_SHA:
        raise ValueError("Missing or invalid ROM; run tools/bicino.py setup first")
    if not args.no_build:
        build()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = LOCAL / "runs" / stamp
    output.mkdir(parents=True)
    command = [str(ROOT / "target/release/esp32sim"), "--board", "waveshare-amoled175", "--boot", "rom",
               "--rom", str(rom), "--flash-mb", "16", "--psram-mb", "8",
               "--bootloader", str(firmware / "bootloader.bin"), "--ptable", str(firmware / "partitions.bin"),
               "--app", str(firmware / "firmware.bin"), "--elf", str(firmware / "firmware.elf"), "--ble",
               "--console", "usb", "--max-seconds", str(args.seconds), "--no-reboot", "--no-dump",
               "--tft-png", str(output / "screen.png"), "--gram-png", str(output / "native-gram.png")]
    if not args.full_storage_probe:
        command += ["--stub", "_ZN7Storage6initSDEv=0xffffffff"]
    if args.script:
        shutil.copyfile(args.script, output / "actions.txt")
        command += ["--script", str(output / "actions.txt")]
    command += args.emulator_arg
    save(output / "command.json", command)
    (output / "command.txt").write_text(shlex.join(command) + "\n")
    start = time.monotonic()
    timed_out = False
    print(f"Capturing {args.seconds:g} emulated seconds; logs: {output}", flush=True)
    with (output / "serial.log").open("wb") as serial, (output / "emulator.log").open("wb") as emulator:
        try:
            result = subprocess.run(command, cwd=ROOT, stdout=serial, stderr=emulator, timeout=args.timeout)
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            timed_out, returncode = True, 124
    elapsed = time.monotonic() - start
    serial = (output / "serial.log").read_text(errors="replace")
    diagnostics = (output / "emulator.log").read_text(errors="replace")
    checks = {
        "process_exited": returncode == 0 and not timed_out,
        "correct_target": bool(re.search(r"BOOT_META .*target=WAVESHARE_AMOLED_175(?: |$)", serial)),
        "boot_ready": bool(re.search(r"BOOT_STAGE .*event=ready .*name=ready", serial)),
        "ble_advertising": "BLE: Server started, advertising" in serial,
        "no_guest_panic": not re.search(r"Guru Meditation|panic'ed|abort\(\) was called|Stack smashing|event=safe_mode", serial),
        "bounded_stop": "stop: Halted" in diagnostics,
    }
    screen = None
    try:
        screen = screen_info(output / "screen.png")
        checks["nonblank_screen"] = screen["distinct_colors"] >= 8
    except (OSError, ValueError, struct.error, zlib.error) as error:
        checks["nonblank_screen"] = False
        screen = {"error": str(error)}
    passed = all(checks.values())
    metadata = re.search(r"^BOOT_META .*$", serial, re.MULTILINE)
    limitations = list(LIMITATIONS)
    if args.full_storage_probe:
        limitations[0] = "SDMMC is unmodeled; real driver timeouts/retries are exercised. No SD maps."
    report = {"schema": 1, "passed": passed, "checks": checks, "wall_seconds": round(elapsed, 3),
              "emulated_seconds_requested": args.seconds, "timed_out": timed_out, "returncode": returncode,
              "firmware": manifest, "boot_metadata": metadata.group(0) if metadata else None,
              "emulator_commit": git("rev-parse", "HEAD"), "emulator_dirty": bool(git("status", "--porcelain")),
              "emulator_binary_sha256": sha(ROOT / "target/release/esp32sim"), "screen": screen,
              "limitations": limitations, "sd_failure_substitution": not args.full_storage_probe}
    save(output / "result.json", report)
    lines = [f"# Bicino 1.75 capture: {'PASS' if passed else 'FAIL'}", "",
             f"Wall time: {elapsed:.2f}s. Requested emulated time: {args.seconds:g}s.", "",
             "![Mounted screen](screen.png)", "", "Checks:"]
    lines += [f"- {name}: {'PASS' if value else 'FAIL'}" for name, value in checks.items()]
    lines += ["", "Files: `serial.log`, `emulator.log`, `result.json`, `command.txt`, `native-gram.png`.", "", "Limitations:"]
    lines += [f"- {item}" for item in limitations]
    (output / "README.md").write_text("\n".join(lines) + "\n")
    link = LOCAL / f".latest-{stamp}"
    link.symlink_to(output.relative_to(LOCAL), target_is_directory=True)
    os.replace(link, LOCAL / "latest")
    print(f"{'PASS' if passed else 'FAIL'} in {elapsed:.2f}s: {output / 'README.md'}")
    print(f"Screen: {output / 'screen.png'}\nResult: {output / 'result.json'}")
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("setup", help="Once: snapshot an existing 1.75 build, cache ROMs and build emulator")
    setup.add_argument("--firmware-dir", type=Path, required=True)
    capture = sub.add_parser("run", help="Boot cached firmware; capture screen and diagnostics")
    capture.add_argument("--firmware-dir", type=Path, help="Snapshot a different existing build first")
    capture.add_argument("--seconds", type=float, default=5)
    capture.add_argument("--timeout", type=float, default=45, help="Wall timeout in seconds")
    capture.add_argument("--script", type=Path)
    capture.add_argument("--no-build", action="store_true", help="Reuse binary without checking emulator source changes")
    capture.add_argument("--full-storage-probe", action="store_true", help="Exercise unsupported SDMMC; expect timeout/delayed boot")
    capture.add_argument("--emulator-arg", action="append", default=[], help="Extra flag/value; repeat, e.g. --emulator-arg=--log-periph")
    args = parser.parse_args()
    LOCAL.mkdir(parents=True, exist_ok=True)
    try:
        if args.action == "setup":
            manifest = import_firmware(args.firmware_dir)
            subprocess.run([str(ROOT / "tools/fetch-rom-elfs.sh"), str(ROOT / ".local/roms")], cwd=ROOT, check=True)
            build()
            print(f"Ready: {manifest['directory']}\nRun: {ROOT / 'tools/bicino.py'} run")
            return 0
        if args.seconds <= 0 or args.timeout <= 0:
            raise ValueError("seconds and timeout must be positive")
        return run(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"bicino: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
