"""ERF (Encapsulated Resource File) reader/writer for Dragon Age: Origins and Dragon Age 2.

Supported: ERF V2.0, V2.2 (DAO) and V3.0 (DA2).
Layouts follow the Dragon Age Toolset Wiki (datoolset.net/wiki/ERF).

V3.0 notes
- Entry names live in a string table; an entry may have no name (nameOffset == -1),
  in which case only its FNV64 name hash identifies it.
- Hashes are FNV over the lowercased name. The wiki does not say FNV-1 or FNV-1a,
  so we compute both and detect which one the archive uses from its named entries.
- Encryption / compression are flagged in the header. Encrypted payloads are not
  decrypted (no passwords); compressed payloads are decompressed on a best-effort basis.
"""
from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

# --------------------------------------------------------------------------- hashing

FNV64_OFFSET, FNV64_PRIME = 0xCBF29CE484222325, 0x100000001B3
FNV32_OFFSET, FNV32_PRIME = 0x811C9DC5, 0x01000193
M64, M32 = (1 << 64) - 1, (1 << 32) - 1


def fnv1_64(data: bytes) -> int:
    h = FNV64_OFFSET
    for b in data:
        h = (h * FNV64_PRIME) & M64
        h ^= b
    return h


def fnv1a_64(data: bytes) -> int:
    h = FNV64_OFFSET
    for b in data:
        h ^= b
        h = (h * FNV64_PRIME) & M64
    return h


def fnv1_32(data: bytes) -> int:
    h = FNV32_OFFSET
    for b in data:
        h = (h * FNV32_PRIME) & M32
        h ^= b
    return h


def fnv1a_32(data: bytes) -> int:
    h = FNV32_OFFSET
    for b in data:
        h ^= b
        h = (h * FNV32_PRIME) & M32
    return h


HASH64 = {"fnv1": fnv1_64, "fnv1a": fnv1a_64}
HASH32 = {"fnv1": fnv1_32, "fnv1a": fnv1a_32}


def name_hash(name: str, variant: str = "fnv1") -> int:
    return HASH64[variant](name.lower().encode("ascii", "replace"))


def hkey_of(name: str) -> str:
    """Lookup key for a file name (base name only, case-insensitive)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return f"{name_hash(base, 'fnv1'):016x}"


def type_hash(ext: str, variant: str = "fnv1") -> int:
    return HASH32[variant](ext.lower().lstrip(".").encode("ascii", "replace"))


# --------------------------------------------------------------------------- model

ENCRYPTION = {0: "none", 1: "xor", 2: "blowfish", 3: "blowfish-da2"}
COMPRESSION = {0: "none", 1: "bioware-zlib", 2: "unknown-2", 3: "unknown-3", 7: "headerless-zlib"}


ARCHIVE_EXTS = {"erf", "rim"}   # DA2 also ships ERF-format archives named .rim (e.g. 2da.rim)


def is_archive(path) -> bool:
    return str(path).lower().rsplit(".", 1)[-1] in ARCHIVE_EXTS


def iter_archives(folder):
    """All .erf/.rim files under folder, sorted case-insensitively by path."""
    from pathlib import Path
    files = [p for p in Path(folder).rglob("*") if p.is_file() and is_archive(p)]
    return sorted(files, key=lambda p: tuple(x.upper() for x in p.parts))


class ErfError(Exception):
    pass


@dataclass
class ErfEntry:
    name: str | None          # None for unnamed V3.0 entries
    offset: int
    packed_size: int
    unpacked_size: int
    name_hash: int | None = None   # V3.0 only
    type_hash: int | None = None   # V3.0 only

    @property
    def key(self) -> str:
        """Stable resource key: lowercased base name, or '#<hash>' if unnamed."""
        if self.name:
            return self.name.replace("\\", "/").rsplit("/", 1)[-1].lower()
        return f"#{self.name_hash:016x}"

    @property
    def hkey(self) -> str:
        """Engine lookup key: the FNV-1 64 hash of the lowercased name, as hex. Hash-only
        entries already store it; for named ones it's computed, so both kinds compare."""
        if self.name_hash is not None:
            return f"{self.name_hash:016x}"
        return hkey_of(self.name or "")

    @property
    def basename(self) -> str | None:
        return self.name.replace("\\", "/").rsplit("/", 1)[-1].lower() if self.name else None

    @property
    def ext(self) -> str:
        if not self.name or "." not in self.name:
            return ""
        return self.name.rsplit(".", 1)[-1].lower()


@dataclass
class Erf:
    path: Path | None
    version: str
    flags: int = 0
    module_id: int = 0
    password_digest: bytes = b""
    entries: list[ErfEntry] = field(default_factory=list)
    hash_variant: str | None = None   # detected for V3.0
    hash_mismatches: int = 0

    @property
    def encryption(self) -> str:
        return ENCRYPTION.get((self.flags >> 4) & 0xF, f"invalid-{(self.flags >> 4) & 0xF}")

    @property
    def compression(self) -> str:
        return COMPRESSION.get((self.flags >> 29) & 0x7, f"invalid-{(self.flags >> 29) & 0x7}")

    # ---------------------------------------------------------------- reading data

    def read_raw(self, entry: ErfEntry, fh: BinaryIO | None = None) -> bytes:
        close = fh is None
        fh = fh or open(self.path, "rb")
        try:
            fh.seek(entry.offset)
            data = fh.read(entry.packed_size)
        finally:
            if close:
                fh.close()
        if len(data) != entry.packed_size:
            raise ErfError(f"truncated entry {entry.key}")
        return data

    def read(self, entry: ErfEntry, fh: BinaryIO | None = None) -> bytes | None:
        """Return unpacked bytes, or None if they can't be recovered (encrypted / unknown codec)."""
        raw = self.read_raw(entry, fh)
        if self.encryption != "none":
            return None
        if self.compression == "none" or entry.packed_size == entry.unpacked_size:
            return raw
        return _decompress(raw, entry.unpacked_size)


def _decompress(raw: bytes, expected: int) -> bytes | None:
    """Best effort: the exact 'Bioware zlib' framing isn't documented, so try the
    likely framings and accept the one that yields the expected size."""
    attempts = [(0, zlib.MAX_WBITS), (0, -zlib.MAX_WBITS)]
    for skip in (1, 2, 4, 8):
        attempts += [(skip, zlib.MAX_WBITS), (skip, -zlib.MAX_WBITS)]
    for skip, wbits in attempts:
        try:
            out = zlib.decompressobj(wbits).decompress(raw[skip:])
        except zlib.error:
            continue
        if len(out) == expected:
            return out
    return None


# --------------------------------------------------------------------------- parsing

def _magic(fh: BinaryIO) -> str:
    raw = fh.read(16)
    if len(raw) < 16:
        raise ErfError("file too small to be an ERF")
    try:
        return raw.decode("utf-16-le")
    except UnicodeDecodeError as e:
        raise ErfError("bad ERF magic") from e


def _utf16_name(raw: bytes) -> str:
    return raw.decode("utf-16-le", "replace").split("\x00", 1)[0]


def read_erf(path: str | Path) -> Erf:
    path = Path(path)
    with open(path, "rb") as fh:
        return parse_erf(fh, path)


def parse_erf(fh: BinaryIO, path: Path | None = None) -> Erf:
    magic = _magic(fh)
    if magic == "ERF V2.0":
        count, _year, _day, _unk = struct.unpack("<4I", fh.read(16))
        erf = Erf(path, "2.0")
        for _ in range(count):
            name_raw, off, size = struct.unpack("<64sII", fh.read(72))
            erf.entries.append(ErfEntry(_utf16_name(name_raw), off, size, size))
        return erf

    if magic == "ERF V2.2":
        count, _year, _day, _unk, flags, module_id = struct.unpack("<6I", fh.read(24))
        digest = fh.read(16)
        erf = Erf(path, "2.2", flags, module_id, digest)
        for _ in range(count):
            name_raw, off, psize, usize = struct.unpack("<64sIII", fh.read(76))
            erf.entries.append(ErfEntry(_utf16_name(name_raw), off, psize, usize))
        return erf

    if magic == "ERF V3.0":
        st_size, count, flags, module_id = struct.unpack("<4I", fh.read(16))
        digest = fh.read(16)
        strings = fh.read(st_size)
        if len(strings) != st_size:
            raise ErfError("truncated string table")
        erf = Erf(path, "3.0", flags, module_id, digest)
        for _ in range(count):
            raw = fh.read(28)
            if len(raw) != 28:
                raise ErfError("truncated table of contents")
            name_off, nhash, thash, off, psize, usize = struct.unpack("<iQIIII", raw)
            name = None
            if 0 <= name_off < st_size:
                end = strings.find(b"\x00", name_off)
                name = strings[name_off: end if end != -1 else None].decode("ascii", "replace")
            erf.entries.append(ErfEntry(name, off, psize, usize, nhash, thash))
        _detect_hash_variant(erf)
        return erf

    raise ErfError(f"unsupported ERF magic {magic!r}")


def _detect_hash_variant(erf: Erf) -> None:
    named = [e for e in erf.entries if e.name]
    if not named:
        return
    scores = {v: sum(name_hash(e.name, v) == e.name_hash for e in named) for v in HASH64}
    best = max(scores, key=scores.get)
    if scores[best]:
        erf.hash_variant = best
        erf.hash_mismatches = len(named) - scores[best]
    else:
        erf.hash_mismatches = len(named)


def iter_entries_with_data(erf: Erf) -> Iterator[tuple[ErfEntry, bytes | None, bytes]]:
    """Yield (entry, unpacked_or_None, raw) with a single open file handle."""
    with open(erf.path, "rb") as fh:
        for e in erf.entries:
            raw = erf.read_raw(e, fh)
            if erf.encryption != "none":
                yield e, None, raw
            elif erf.compression == "none" or e.packed_size == e.unpacked_size:
                yield e, raw, raw
            else:
                yield e, _decompress(raw, e.unpacked_size), raw


# --------------------------------------------------------------------------- writing (V3.0)

def write_erf_v3(path: str | Path, files: list[tuple[str, bytes]], *, alignment: int = 4,
                 variant: str = "fnv1", unnamed: set[str] | None = None) -> None:
    """Write an uncompressed, unencrypted ERF V3.0 (the layout DA2 override mods use).

    `unnamed` lists names to store without a string-table entry (hash only) — useful for tests.
    """
    unnamed = unnamed or set()
    strings = bytearray()
    name_offsets = []
    for name, _ in files:
        if name in unnamed:
            name_offsets.append(-1)
        else:
            name_offsets.append(len(strings))
            strings += name.encode("ascii") + b"\x00"
    header_size = 48 + len(strings) + 28 * len(files)

    def align(n: int) -> int:
        return (n + alignment - 1) // alignment * alignment

    offsets, cursor = [], align(header_size)
    for _, data in files:
        offsets.append(cursor)
        cursor = align(cursor + len(data))

    out = io.BytesIO()
    out.write("ERF V3.0".encode("utf-16-le"))
    out.write(struct.pack("<4I", len(strings), len(files), 0, 0))
    out.write(b"\x00" * 16)
    out.write(strings)
    for (name, data), noff, off in zip(files, name_offsets, offsets):
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        out.write(struct.pack("<iQIIII", noff, name_hash(name, variant), type_hash(ext, variant),
                              off, len(data), len(data)))
    for (_, data), off in zip(files, offsets):
        out.write(b"\x00" * (off - out.tell()))
        out.write(data)
    Path(path).write_bytes(out.getvalue())
