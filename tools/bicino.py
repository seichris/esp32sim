#!/usr/bin/env python3
"""Bicino 1.75 capture and live workflow. Standard library only; never builds/flashes firmware."""
import argparse
import datetime as dt
import hashlib
import json
import math
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
# open-bike-computer's locked builder records every image it flashes; the ones besides the
# bootloader, partition table and app (boot_app0.bin's OTA data) go in at their offsets.
FLASH_PLAN = "open-bike-flash-plan.json"
ROM_SHA = "c0ce0f338d1de1bdc6efbef1591779a2a42c1ab7d759d3c6ae8ae63a7dd34cfd"
STORAGE = {
    "no-card-substitution": "Storage::initSD is substituted with ESP_FAIL; real FFat fallback runs. No SD card or SD maps.",
    "empty-socket": "Real SDMMC driver against an empty modelled socket: commands time out, the firmware's SPI fallback fails, FFat runs. No SD maps.",
    "sd-card": "Real SDMMC driver and FatFs on a modelled card (copy-on-write image, untimed bus, one-bit slot 1). Map content is what the image holds; no map transfer, signature or durable-install path runs.",
}
LIMITATIONS = [
    STORAGE["no-card-substitution"],
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


def flash_extras(source):
    """Images of the build's flash plan other than FILES: name -> (path, offset)."""
    plan = source / FLASH_PLAN
    if not plan.is_file():
        return {}
    extras = {}
    for image in json.loads(plan.read_text())["images"]:
        path, offset = Path(image["path"]), image["offset"]
        if path.name in FILES:
            continue
        if not re.fullmatch(r"0x[0-9a-fA-F]+", offset) or not path.is_file():
            raise ValueError(f"{plan}: unusable image {path.name} at {offset}")
        extras[path.name] = (path, offset)
    return extras


def import_firmware(directory):
    source = directory.expanduser().resolve()
    missing = [name for name in FILES if not (source / name).is_file()]
    if missing:
        raise ValueError(f"Missing firmware files in {source}: {', '.join(missing)}")
    extras = flash_extras(source)
    paths = {name: source / name for name in FILES} | {name: path for name, (path, _) in extras.items()}
    hashes = {name: sha(path) for name, path in paths.items()}
    key = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16]
    destination = LOCAL / "inputs" / key
    destination.mkdir(parents=True, exist_ok=True)
    for name, path in paths.items():
        if not (destination / name).exists() or sha(destination / name) != hashes[name]:
            # APFS snapshots share immutable file extents instead of duplicating
            # a 50 MB ELF on every import. Publish only a complete verified file.
            pending = destination / (name + ".pending")
            cloned = sys.platform == "darwin" and subprocess.run(
                ["/bin/cp", "-c", str(path), str(pending)], capture_output=True).returncode == 0
            if not cloned:
                shutil.copyfile(path, pending)
            if sha(pending) != hashes[name]:
                raise ValueError(f"Firmware changed during snapshot: {name}")
            os.replace(pending, destination / name)
        if sha(destination / name) != hashes[name]:
            raise ValueError(f"Firmware changed during snapshot, or corrupt snapshot: {name}")
    manifest = {"directory": str(destination), "source_directory": str(source),
                "source_git_commit": git("rev-parse", "HEAD", directory=source), "sha256": hashes,
                "flash_at": {name: offset for name, (_, offset) in extras.items()}}
    if extras:
        manifest["flash_plan_sha256"] = sha(source / FLASH_PLAN)
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


def storage_for(args):
    """The storage the guest gets, and what is known about an SD image."""
    if getattr(args, "sd_image", None) and args.full_storage_probe:
        raise ValueError("--sd-image and --full-storage-probe exclude each other")
    if not getattr(args, "sd_image", None):
        return {"mode": "empty-socket" if args.full_storage_probe else "no-card-substitution"}
    image = args.sd_image.expanduser().resolve()
    info = {"mode": "sd-card", "image": str(image), "sha256": sha(image), "bytes": image.stat().st_size}
    described = image.with_name(image.name + ".json")
    if described.is_file():
        card = json.loads(described.read_text())
        if card.get("sha256") != info["sha256"]:
            raise ValueError(f"{image.name} does not match {described.name}")
        info.update(active_map=card.get("active_map"), files=card.get("files", []))
    return info


def sd_reads(log, files):
    """Read commands (CMD17/18, block-addressed) the host answered, per image file whose sectors
    they start in; `--debug sdmmc` logs each command."""
    runs = [(f["path"], lba, lba + count) for f in files for lba, count in f.get("lba_runs", [])]
    reads = {}
    for match in re.finditer(r"^\[sdmmc\] slot \d CMD1[78] arg (0x[0-9a-f]+) -> Ok", log, re.MULTILINE):
        lba = int(match.group(1), 16)
        for path, start, end in runs:
            if start <= lba < end:
                reads[path] = reads.get(path, 0) + 1
    return reads


def sd_checks(serial, storage):
    """Boot-time SD evidence from the firmware's own logs: the native mount and, for an image
    with an active map, the renderer probe and root switch to it."""
    checks = {"sd_mounted": bool(re.search(r"SDIO: attempt=\d+/\d+ phase=mount bus=SDMMC pins=1 mounted=1 card=1 root=1", serial))}
    active = storage.get("active_map")
    if active:
        checks["map_selected"] = f"MAP_TRANSFER: activeMapId={active['mapId']} root={active['root']}\n" in serial
    return checks


def prepare(args):
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
    return manifest, firmware, rom


def new_output():
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = LOCAL / "runs" / stamp
    output.mkdir(parents=True)
    return output


def command_for(args, firmware, rom, output, manifest, storage):
    command = [str(ROOT / "target/release/esp32sim"), "--board", "waveshare-amoled175", "--boot", "rom",
               "--rom", str(rom), "--flash-mb", "16", "--psram-mb", "8",
               "--bootloader", str(firmware / "bootloader.bin"), "--ptable", str(firmware / "partitions.bin"),
               "--app", str(firmware / "firmware.bin"), "--elf", str(firmware / "firmware.elf"), "--ble",
               "--console", "usb", "--max-seconds", str(args.seconds), "--no-reboot", "--no-dump",
               "--tft-png", str(output / "screen.png"), "--gram-png", str(output / "native-gram.png")]
    for name, offset in manifest.get("flash_at", {}).items():
        command += ["--flash-at", f"{offset}={firmware / name}"]
    if storage["mode"] == "no-card-substitution":
        command += ["--stub", "_ZN7Storage6initSDEv=0xffffffff"]
    elif storage["mode"] == "sd-card":
        # Copy-on-write: the guest's writes stay in memory and the image is never changed.
        command += ["--sd", storage["image"], "--debug", "sdmmc"]
    if args.script:
        shutil.copyfile(args.script, output / "actions.txt")
        command += ["--script", str(output / "actions.txt")]
    command += args.emulator_arg
    save(output / "command.json", command)
    (output / "command.txt").write_text(shlex.join(command) + "\n")
    return command


def run(args):
    storage = storage_for(args)
    manifest, firmware, rom = prepare(args)
    output = new_output()
    command = command_for(args, firmware, rom, output, manifest, storage)
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
        "no_guest_panic": not re.search(r"assert failed:|Guru Meditation|panic'ed|abort\(\) was called|Stack smashing|event=safe_mode", serial),
        "bounded_stop": "stop: Halted" in diagnostics,
    }
    screen = None
    try:
        screen = screen_info(output / "screen.png")
        checks["nonblank_screen"] = screen["distinct_colors"] >= 8
    except (OSError, ValueError, struct.error, zlib.error) as error:
        checks["nonblank_screen"] = False
        screen = {"error": str(error)}
    if storage["mode"] == "sd-card":
        checks.update(sd_checks(serial, storage))
        storage["reads"] = sd_reads(diagnostics, storage.get("files", []))
        if storage.get("active_map"):
            checks["map_block_read"] = any(path.endswith(".fmb") for path in storage["reads"])
    passed = all(checks.values())
    metadata = re.search(r"^BOOT_META .*$", serial, re.MULTILINE)
    limitations = [STORAGE[storage["mode"]]] + LIMITATIONS[1:]
    report = {"schema": 1, "passed": passed, "checks": checks, "wall_seconds": round(elapsed, 3),
              "emulated_seconds_requested": args.seconds, "timed_out": timed_out, "returncode": returncode,
              "firmware": manifest, "boot_metadata": metadata.group(0) if metadata else None,
              "emulator_commit": git("rev-parse", "HEAD"), "emulator_dirty": bool(git("status", "--porcelain")),
              "emulator_binary_sha256": sha(ROOT / "target/release/esp32sim"), "screen": screen,
              "limitations": limitations, "storage": {k: v for k, v in storage.items() if k != "files"},
              "sd_failure_substitution": storage["mode"] == "no-card-substitution"}
    save(output / "result.json", report)
    lines = [f"# Bicino 1.75 capture: {'PASS' if passed else 'FAIL'}", "",
             f"Wall time: {elapsed:.2f}s. Requested emulated time: {args.seconds:g}s.", "",
             "![Mounted screen](screen.png)", "", "Checks:"]
    lines += [f"- {name}: {'PASS' if value else 'FAIL'}" for name, value in checks.items()]
    lines += ["", "Files: `serial.log`, `emulator.log`, `result.json`, `command.txt`, `native-gram.png`.", "", "Limitations:"]
    lines += [f"- {item}" for item in limitations]
    (output / "README.md").write_text("\n".join(lines) + "\n")
    link = LOCAL / f".latest-{output.name}"
    link.symlink_to(output.relative_to(LOCAL), target_is_directory=True)
    os.replace(link, LOCAL / "latest")
    print(f"{'PASS' if passed else 'FAIL'} in {elapsed:.2f}s: {output / 'README.md'}")
    print(f"Screen: {output / 'screen.png'}\nResult: {output / 'result.json'}")
    return 0 if passed else 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {"start", "live", "restart", "stop", "status", "screen", "input"}:
        from bicino_live import main as live_main
        return live_main()
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Live commands: start, live, restart, status, screen, input, stop. "
        "Use COMMAND --help for options. See examples/bicino175/README.md for BLE and navigation replay."))
    sub = parser.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("setup", help="Once: snapshot an existing 1.75 build, cache ROMs and build emulator")
    setup.add_argument("--firmware-dir", type=Path, required=True)
    capture = sub.add_parser("run", help="Boot cached firmware; capture screen and diagnostics")
    capture.add_argument("--firmware-dir", type=Path, help="Snapshot a different existing build first")
    capture.add_argument("--seconds", type=float, default=5)
    capture.add_argument("--timeout", type=float, default=45, help="Wall timeout in seconds")
    capture.add_argument("--script", type=Path)
    capture.add_argument("--no-build", action="store_true", help="Reuse binary without checking emulator source changes")
    capture.add_argument("--full-storage-probe", action="store_true", help="No substitution and no card: the real SDMMC driver meets an empty socket")
    capture.add_argument("--sd-image", type=Path, help="A FAT image in the microSD socket (tools/bicino_sdcard.py builds one); no substitution")
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
        if not all(math.isfinite(value) and value > 0 for value in (args.seconds, args.timeout)):
            raise ValueError("seconds and timeout must be finite and positive")
        return run(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"bicino: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
