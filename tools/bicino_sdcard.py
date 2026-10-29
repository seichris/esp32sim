#!/usr/bin/env python3
"""Build a reproducible FAT32 microSD image for `bicino.py --sd-image`, and read one back.

    bicino_sdcard.py build --out card.img [--size-mib 64] [--map DIR --map-id ID] [--tree SRC=/DEST] ...
    bicino_sdcard.py list card.img

The layout follows tests/sd_image.rs: an MBR with one FAT32 (LBA) partition from LBA 2048,
512-byte sectors, and here one-sector clusters. Every name gets a long-file-name entry
(IDF's FatFs runs with LFN enabled) and a numbered 8.3 alias; timestamps are fixed, so the same
inputs give the same bytes. Field offsets: Microsoft's FAT32 File System Specification 1.03
(fatgen103), "Boot Sector and BPB", "FAT32 FSInfo Sector Structure", "FAT Directory Structure",
"FAT Long Directory Entries". `--map` copies a renderer tree (a `VECTMAP/<mapId>/` directory)
to `VECTMAP/.maps/<mapId>` and writes a session-less `VECTMAP/active-map.json` selection (mapId
and root only: no session, receipts or signatures), which the firmware's boot path probes and
switches to; seichris/open-bike-computer's `MapTransferInstaller::safeActiveRoot` accepts only
`/VECTMAP` and `/VECTMAP/.maps/<id>` roots. A JSON manifest next to the image records each
file's SHA-256 and LBA runs. Standard library only; it never formats a real card.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

SECTOR = 512
PARTITION_LBA = 2048
RESERVED = 32
FAT_EOC = 0x0FFF_FFFF
DATE = (2026 - 1980) << 9 | 1 << 5 | 1      # 2026-01-01 00:00:00, every entry
VALID_83 = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'()-@^_`{}~")


class Node:
    def __init__(self, name, data=None):
        self.name, self.data, self.children, self.cluster, self.clusters = name, data, {}, 0, 0

    @property
    def is_dir(self):
        return self.data is None


def add_file(root, path, data):
    parts = [p for p in path.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise ValueError(f"bad image path: {path}")
    node = root
    for part in parts[:-1]:
        node = node.children.setdefault(part.upper(), Node(part))
        if not node.is_dir:
            raise ValueError(f"{path}: {part} is a file")
    key = parts[-1].upper()
    if key in node.children:
        raise ValueError(f"duplicate image path: {path}")
    node.children[key] = Node(parts[-1], data)


def short_names(names):
    """A unique numbered 8.3 alias per name (fatgen103, "Basis-Name Generation Algorithm")."""
    taken, out = set(), {}
    for name in names:
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            stem, ext = name, ""
        clean = lambda s: bytes(c if c in VALID_83 else ord("_") for c in s.upper().encode("ascii", "replace") if c not in b" .")
        base, ext = clean(stem) or b"_", clean(ext)[:3]
        for n in range(1, 1_000_000):
            tail = b"~%d" % n
            alias = (base[:8 - len(tail)] + tail).ljust(8) + ext.ljust(3)
            if alias not in taken:
                taken.add(alias)
                out[name] = alias
                break
    return out


def checksum(alias):
    s = 0
    for c in alias:
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


def lfn_entries(name, alias):
    units = [name.encode("utf-16-le")[i:i + 2] for i in range(0, len(name.encode("utf-16-le")), 2)]
    if len(units) > 255:
        raise ValueError(f"name too long: {name}")
    if len(units) % 13:
        units += [b"\0\0"] + [b"\xff\xff"] * (12 - len(units) % 13)
    count, out, sum_ = len(units) // 13, [], checksum(alias)
    for i in range(count, 0, -1):
        part = units[(i - 1) * 13:i * 13]
        out.append(bytes([i | (0x40 if i == count else 0)]) + b"".join(part[:5]) + bytes([0x0F, 0, sum_])
                   + b"".join(part[5:11]) + b"\0\0" + b"".join(part[11:]))
    return out


def short_entry(alias, directory, cluster, size):
    return alias + struct.pack("<BBBHHHHHHHI", 0x10 if directory else 0x20, 0, 0, 0, DATE, DATE,
                               cluster >> 16, 0, DATE, cluster & 0xFFFF, size)


def entries(node, parent_cluster, is_root):
    out = []
    if not is_root:
        out.append(short_entry(b".          ", True, node.cluster, 0))
        out.append(short_entry(b"..         ", True, parent_cluster, 0))
    children = sorted(node.children.values(), key=lambda c: c.name)
    aliases = short_names([c.name for c in children])
    for child in children:
        out += lfn_entries(child.name, aliases[child.name])
        out.append(short_entry(aliases[child.name], child.is_dir, child.cluster, 0 if child.is_dir else len(child.data)))
    return b"".join(out)


def build(files, out, size_mib=64, label="BICINO"):
    size = size_mib << 20
    total = size // SECTOR - PARTITION_LBA
    fat_sectors = -(-(total - RESERVED) // ((256 * 1 + 2) // 2))       # fatgen103, "FAT Type Determination"
    data_lba = RESERVED + 2 * fat_sectors
    clusters = total - data_lba
    if clusters < 65525:
        raise ValueError(f"{size_mib} MiB gives {clusters} clusters; FAT32 needs 65525")
    root = Node("")
    for path, data in files:
        add_file(root, path, data)
    # Allocate: directories need their children's clusters before they can be written.
    next_cluster = [2]
    def allocate(node, count):
        node.cluster, node.clusters = (next_cluster[0], count) if count else (0, 0)
        next_cluster[0] += count
    def walk(node, is_root):
        entry_bytes = (0 if is_root else 64) + sum(32 * (len(lfn_entries(c.name, b" " * 11)) + 1) for c in node.children.values())
        allocate(node, max(1, -(-(entry_bytes + 32) // SECTOR)))
        for child in sorted(node.children.values(), key=lambda c: c.name):
            if child.is_dir:
                walk(child, False)
            else:
                allocate(child, -(-len(child.data) // SECTOR))
    walk(root, True)
    used = next_cluster[0] - 2
    if used > clusters:
        raise ValueError(f"image too small: needs {used} clusters, has {clusters}")
    fat = bytearray(4 * next_cluster[0])
    struct.pack_into("<II", fat, 0, 0x0FFF_FFF8, FAT_EOC)
    blobs = []
    def emit(node, parent_cluster, is_root):
        data = entries(node, parent_cluster, is_root) if node.is_dir else node.data
        for i in range(node.clusters):
            struct.pack_into("<I", fat, 4 * (node.cluster + i), node.cluster + i + 1 if i + 1 < node.clusters else FAT_EOC)
        if node.clusters:
            blobs.append((node.cluster, data))
        if node.is_dir:
            for child in node.children.values():
                emit(child, 0 if is_root else node.cluster, False)
    emit(root, 0, True)
    mbr = bytearray(SECTOR)
    struct.pack_into("<I", mbr, 440, 0x5D1C_0B17)
    mbr[446:462] = bytes([0, 0xFE, 0xFF, 0xFF, 0x0C, 0xFE, 0xFF, 0xFF]) + struct.pack("<II", PARTITION_LBA, total)
    mbr[510:512] = b"\x55\xaa"
    vbr = bytearray(SECTOR)
    vbr[0:11] = b"\xeb\x58\x90ESP32SIM"
    struct.pack_into("<HBHBHHBHHHII", vbr, 11, SECTOR, 1, RESERVED, 2, 0, 0, 0xF8, 0, 63, 255, PARTITION_LBA, total)
    struct.pack_into("<IHHIHH", vbr, 36, fat_sectors, 0, 0, 2, 1, 6)
    vbr[64], vbr[66] = 0x80, 0x29
    struct.pack_into("<I", vbr, 67, 0x5D1C_0B17)
    vbr[71:82] = label.upper().encode("ascii")[:11].ljust(11)
    vbr[82:90] = b"FAT32   "
    vbr[510:512] = b"\x55\xaa"
    info = bytearray(SECTOR)
    struct.pack_into("<I", info, 0, 0x4161_5252)
    struct.pack_into("<III", info, 484, 0x6141_7272, clusters - used, next_cluster[0])
    info[510:512] = b"\x55\xaa"
    out = Path(out)
    with out.open("wb") as f:
        f.truncate(size)
        def put(lba, data):
            f.seek(lba * SECTOR)
            f.write(data)
        put(0, mbr)
        for base in (0, 6):
            put(PARTITION_LBA + base, vbr)
            put(PARTITION_LBA + base + 1, info)
        for copy in range(2):
            put(PARTITION_LBA + RESERVED + copy * fat_sectors, fat)
        for cluster, data in blobs:
            put(PARTITION_LBA + data_lba + cluster - 2, data)


def read(image):
    """Every file in the image: path -> (bytes, [[first LBA, sectors], ...]). An independent reader."""
    raw = Path(image).read_bytes()
    if raw[510:512] != b"\x55\xaa" or raw[450] != 0x0C:
        raise ValueError("no FAT32 (LBA) partition in the MBR")
    part = struct.unpack_from("<I", raw, 454)[0] * SECTOR
    bps, spc, reserved, nfats = struct.unpack_from("<HBHB", raw, part + 11)
    fat_sectors, root_cluster = struct.unpack_from("<I", raw, part + 36)[0], struct.unpack_from("<I", raw, part + 44)[0]
    if raw[part + 82:part + 90] != b"FAT32   " or bps != SECTOR:
        raise ValueError("not a 512-byte-sector FAT32 volume")
    fat_at, data_at = part + reserved * bps, part + (reserved + nfats * fat_sectors) * bps
    def chain(cluster):
        seen = []
        while 2 <= cluster < 0x0FFF_FFF8:
            if cluster in seen:
                raise ValueError("FAT loop")
            seen.append(cluster)
            cluster = struct.unpack_from("<I", raw, fat_at + 4 * cluster)[0] & 0x0FFF_FFFF
        return seen
    def content(cluster):
        return b"".join(raw[data_at + (c - 2) * spc * bps:data_at + (c - 1) * spc * bps] for c in chain(cluster))
    files = {}
    def walk(cluster, prefix):
        data, lfn = content(cluster), []
        for at in range(0, len(data), 32):
            e = data[at:at + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = []
                continue
            if e[11] == 0x0F:
                lfn.insert(0, e)
                continue
            alias, attr = e[:11], e[11]
            name = None
            if lfn and all(x[13] == checksum(alias) for x in lfn):
                units = b"".join(x[1:11] + x[14:26] + x[28:32] for x in lfn)
                name = units.decode("utf-16-le").split("\0")[0]
            lfn = []
            if alias[:1] == b"." or attr & 0x08:
                continue
            name = name or (alias[:8].decode().rstrip() + ("." + alias[8:].decode().rstrip() if alias[8:].strip() else ""))
            first = struct.unpack_from("<H", e, 20)[0] << 16 | struct.unpack_from("<H", e, 26)[0]
            path = prefix + "/" + name
            if attr & 0x10:
                walk(first, path)
            else:
                size, runs = struct.unpack_from("<I", e, 28)[0], []
                for c in (chain(first) if size else []):
                    lba = data_at // bps + (c - 2) * spc
                    if runs and runs[-1][0] + runs[-1][1] == lba:
                        runs[-1][1] += spc
                    else:
                        runs.append([lba, spc])
                files[path] = (content(first)[:size], runs)
    walk(root_cluster, "")
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build", help="Write an image and its JSON manifest")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--size-mib", type=int, default=64)
    b.add_argument("--map", type=Path, help="A VECTMAP/<mapId>/ renderer tree to install with a legacy active selection")
    b.add_argument("--map-id", help="mapId and directory name for --map")
    b.add_argument("--tree", action="append", default=[], help="SRC=/DEST: copy a host directory into the image")
    l = sub.add_parser("list", help="Read an image back")
    l.add_argument("image", type=Path)
    args = parser.parse_args()
    if args.action == "list":
        for path, (data, runs) in sorted(read(args.image).items()):
            print(f"{len(data):10d} {hashlib.sha256(data).hexdigest()} {path} lba={runs}")
        return 0
    files, sources = [], []
    def tree(src, dest):
        src = src.resolve()
        found = sorted(p for p in src.rglob("*") if p.is_file() and not p.name.startswith("."))
        if not found:
            raise ValueError(f"no files under {src}")
        for p in found:
            files.append((dest.rstrip("/") + "/" + p.relative_to(src).as_posix(), p.read_bytes()))
        sources.append({"destination": dest, "files": len(found),
                        "tree_sha256": hashlib.sha256(b"".join(hashlib.sha256(p.read_bytes()).digest() + p.relative_to(src).as_posix().encode() for p in found)).hexdigest()})
    if bool(args.map) != bool(args.map_id):
        parser.error("--map and --map-id go together")
    active = None
    if args.map:
        if not args.map_id.replace("-", "").replace("_", "").isalnum():
            parser.error("--map-id: letters, digits, '-' and '_' only")
        tree(args.map, f"/VECTMAP/.maps/{args.map_id}")
        active = {"mapId": args.map_id, "root": f"/VECTMAP/.maps/{args.map_id}"}
        files.append(("/VECTMAP/active-map.json", (json.dumps(active, separators=(",", ":")) + "\n").encode()))
    for spec in args.tree:
        src, sep, dest = spec.partition("=")
        if not sep or not dest.startswith("/"):
            parser.error("--tree expects SRC=/DEST")
        tree(Path(src), dest)
    if not files:
        parser.error("nothing to write: give --map or --tree")
    build(files, args.out, args.size_mib)
    back = read(args.out)
    expected = {path: data for path, data in files}
    if {p: d for p, (d, _) in back.items()} != expected:
        raise ValueError("read-back differs from the inputs")
    manifest = {"schema": 1, "image": args.out.name, "bytes": args.out.stat().st_size,
                "sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(), "partition_lba": PARTITION_LBA,
                "sector_bytes": SECTOR, "cluster_bytes": SECTOR, "active_map": active, "sources": sources,
                "files": [{"path": p, "bytes": len(d), "sha256": hashlib.sha256(d).hexdigest(), "lba_runs": runs}
                          for p, (d, runs) in sorted(back.items())]}
    manifest_path = args.out.with_name(args.out.name + ".json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"{args.out}: {len(back)} files, sha256 {manifest['sha256']}\n{manifest_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f"bicino_sdcard: {error}", file=sys.stderr)
        sys.exit(2)
