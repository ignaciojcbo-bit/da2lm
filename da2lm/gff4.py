"""GFF V4.0 / V4.1 reader and writer (Dragon Age: Origins / Dragon Age 2).

Layout (little-endian on PC), after xoreos' gff4file.cpp and the DA Toolset Wiki:

  header   "GFF " "V4.x" platform type typeVersion structCount
           [V4.1: stringCount stringOffset] dataOffset
  structs  structCount x { label[4], fieldCount, fieldOffset(abs), structSize }
  fields   per struct: fieldCount x { label u32, type u16, flags u16, offset u32 }
  data     the top-level struct (template 0) lives at dataOffset

Field flags: 0x8000 list, 0x4000 struct (type = struct template index), 0x2000 reference.
Lists and references store a u32 offset relative to dataOffset (0xFFFFFFFF = null);
a list block is u32 count + elements. Strings (type 14) are a u32 offset to
{u32 length-in-chars, UTF-16LE chars}; in V4.1 files with a shared string table the
u32 is instead an index into that table (UTF-8, NUL-terminated).

Scope: everything GDA and most simple GFFs need. Generics and lists/references of
non-struct types raise Gff4Unsupported rather than guessing.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

NULL = 0xFFFFFFFF
F_LIST, F_STRUCT, F_REF = 0x8000, 0x4000, 0x2000
T_STRING, T_TLK, T_GENERIC = 14, 17, 0xFFFF

# type id -> struct format (scalars and fixed-size compounds)
SCALAR = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "Q", 7: "q", 8: "f", 9: "d",
          10: "3f", 12: "4f", 13: "4f", 15: "4f", 16: "16f", 17: "2I", 18: "i"}


class Gff4Error(Exception):
    pass


class Gff4Unsupported(Gff4Error):
    pass


@dataclass
class FieldDef:
    label: int
    type: int
    flags: int
    offset: int

    @property
    def is_list(self): return bool(self.flags & F_LIST)
    @property
    def is_struct(self): return bool(self.flags & F_STRUCT)
    @property
    def is_ref(self): return bool(self.flags & F_REF)


@dataclass
class StructDef:
    label: bytes          # 4 raw bytes, e.g. b"gtop"
    size: int
    fields: list[FieldDef]


@dataclass
class Struct:
    sdef: int                                   # template index
    values: dict[int, object] = field(default_factory=dict)

    def __eq__(self, other):
        return isinstance(other, Struct) and self.sdef == other.sdef and self.values == other.values


@dataclass
class Gff4:
    version: str          # "V4.0" | "V4.1"
    platform: bytes
    ftype: bytes          # e.g. b"G2DA"
    fversion: bytes       # e.g. b"V0.2"
    structs: list[StructDef]
    top: Struct
    shared_strings: bool = False


# --------------------------------------------------------------------------- reading

def _u32(buf, pos):
    return struct.unpack_from("<I", buf, pos)[0]


def parse(buf: bytes) -> Gff4:
    if buf[:4] != b"GFF ":
        raise Gff4Error("not a GFF file")
    version = buf[4:8].decode("ascii", "replace")
    if version not in ("V4.0", "V4.1"):
        raise Gff4Error(f"unsupported GFF version {version}")
    platform, ftype, fversion = buf[8:12], buf[12:16], buf[16:20]
    if platform.strip() not in (b"PC",):
        raise Gff4Unsupported(f"platform {platform!r} (only PC / little-endian)")
    struct_count = _u32(buf, 20)
    pos = 24
    string_count, string_offset = 0, NULL
    if version == "V4.1":
        string_count, string_offset = _u32(buf, 24), _u32(buf, 28)
        pos = 32
    data_off = _u32(buf, pos)
    pos += 4

    structs = []
    for i in range(struct_count):
        p = pos + i * 16
        label = buf[p:p + 4]
        fcount, foff, size = struct.unpack_from("<3I", buf, p + 4)
        fields = []
        if foff != NULL:
            for j in range(fcount):
                lab, typ, flg, off = struct.unpack_from("<IHHI", buf, foff + j * 12)
                fields.append(FieldDef(lab, typ, flg, off))
        structs.append(StructDef(label, size, fields))

    shared = None
    if string_count or string_offset != NULL:
        shared, p = [], string_offset
        for _ in range(string_count):
            end = buf.index(b"\x00", p)
            shared.append(buf[p:end].decode("utf-8", "replace"))
            p = end + 1

    r = _Reader(buf, data_off, structs, shared)
    top = r.struct(0, data_off)
    return Gff4(version, platform, ftype, fversion, structs, top, shared is not None)


class _Reader:
    def __init__(self, buf, data_off, structs, shared):
        self.buf, self.data_off, self.structs, self.shared = buf, data_off, structs, shared

    def struct(self, idx: int, abs_off: int) -> Struct:
        sd = self.structs[idx]
        s = Struct(idx)
        for fd in sd.fields:
            s.values[fd.label] = self.field(fd, abs_off + fd.offset)
        return s

    def rel(self, pos):
        off = _u32(self.buf, pos)
        return None if off == NULL else self.data_off + off

    def field(self, fd: FieldDef, pos: int):
        if fd.type == T_GENERIC and not fd.is_struct:
            raise Gff4Unsupported("generic fields")
        if fd.is_struct:
            idx, size = fd.type, self.structs[fd.type].size
            if fd.is_list:
                base = self.rel(pos)
                if base is None:
                    return None
                n = _u32(self.buf, base)
                if fd.is_ref:
                    return [None if (o := self.rel(base + 4 + 4 * i)) is None else self.struct(idx, o)
                            for i in range(n)]
                return [self.struct(idx, base + 4 + i * size) for i in range(n)]
            if fd.is_ref:
                o = self.rel(pos)
                return None if o is None else self.struct(idx, o)
            return self.struct(idx, pos)
        if fd.is_list:
            raise Gff4Unsupported(f"list of type {fd.type}")
        if fd.type == T_STRING:
            if self.shared is not None:
                i = _u32(self.buf, pos)
                return None if i == NULL else self.shared[i]
            o = self.rel(pos)
            if o is None:
                return None
            n = _u32(self.buf, o)
            return self.buf[o + 4:o + 4 + 2 * n].decode("utf-16-le", "replace")
        if fd.is_ref:
            raise Gff4Unsupported(f"reference to type {fd.type}")
        fmt = SCALAR.get(fd.type)
        if fmt is None:
            raise Gff4Unsupported(f"field type {fd.type}")
        v = struct.unpack_from("<" + fmt, self.buf, pos)
        return v[0] if len(v) == 1 else v


# --------------------------------------------------------------------------- writing

def serialize(g: Gff4) -> bytes:
    header_size = 36 if g.version == "V4.1" else 28
    struct_area = header_size + 16 * len(g.structs)
    field_offsets, p = [], struct_area
    for sd in g.structs:
        field_offsets.append(p if sd.fields else NULL)
        p += 12 * len(sd.fields)
    data_off = (p + 3) // 4 * 4

    w = _Writer(g.structs, g.shared_strings)
    top = w.alloc(g.structs[0].size)
    w.write_struct(g.top, top)
    data = bytes(w.data)

    out = bytearray(b"GFF " + g.version.encode() + g.platform + g.ftype + g.fversion)
    out += struct.pack("<I", len(g.structs))
    string_blob = b""
    if g.version == "V4.1":
        if g.shared_strings:
            string_blob = b"".join(s.encode("utf-8") + b"\x00" for s in w.shared)
            out += struct.pack("<II", len(w.shared), data_off + len(data))
        else:
            out += struct.pack("<II", 0, NULL)
    out += struct.pack("<I", data_off)
    for sd, foff in zip(g.structs, field_offsets):
        out += sd.label + struct.pack("<3I", len(sd.fields), foff, sd.size)
    for sd in g.structs:
        for fd in sd.fields:
            out += struct.pack("<IHHI", fd.label, fd.type, fd.flags, fd.offset)
    out += b"\x00" * (data_off - len(out))
    return bytes(out + data + string_blob)


class _Writer:
    def __init__(self, structs, shared_strings):
        self.structs = structs
        self.data = bytearray()
        self.shared_mode = shared_strings
        self.shared: list[str] = []
        self.shared_idx: dict[str, int] = {}
        self.str_off: dict[str, int] = {}

    def alloc(self, n: int) -> int:
        pad = (-len(self.data)) % 4
        self.data += b"\x00" * pad
        off = len(self.data)
        self.data += b"\x00" * n
        return off

    def put(self, pos, fmt, *v):
        struct.pack_into("<" + fmt, self.data, pos, *v)

    def write_struct(self, s: Struct, off: int):
        sd = self.structs[s.sdef]
        for fd in sd.fields:
            self.write_field(fd, off + fd.offset, s.values.get(fd.label))

    def write_field(self, fd: FieldDef, pos: int, v):
        if fd.is_struct:
            size = self.structs[fd.type].size
            if fd.is_list:
                if v is None:
                    return self.put(pos, "I", NULL)
                elem = 4 if fd.is_ref else size
                block = self.alloc(4 + elem * len(v))
                self.put(pos, "I", block)
                self.put(block, "I", len(v))
                for i, s in enumerate(v):
                    if fd.is_ref:
                        if s is None:
                            self.put(block + 4 + 4 * i, "I", NULL)
                        else:
                            so = self.alloc(size)
                            self.put(block + 4 + 4 * i, "I", so)
                            self.write_struct(s, so)
                    else:
                        self.write_struct(s, block + 4 + i * size)
                return
            if fd.is_ref:
                if v is None:
                    return self.put(pos, "I", NULL)
                so = self.alloc(size)
                self.put(pos, "I", so)
                return self.write_struct(v, so)
            return self.write_struct(v, pos)
        if fd.type == T_STRING:
            if v is None:
                return self.put(pos, "I", NULL)
            if self.shared_mode:
                if v not in self.shared_idx:
                    self.shared_idx[v] = len(self.shared)
                    self.shared.append(v)
                return self.put(pos, "I", self.shared_idx[v])
            if v not in self.str_off:
                enc = v.encode("utf-16-le")
                so = self.alloc(4 + len(enc))
                self.put(so, "I", len(enc) // 2)
                self.data[so + 4:so + 4 + len(enc)] = enc
                self.str_off[v] = so
            return self.put(pos, "I", self.str_off[v])
        fmt = SCALAR.get(fd.type)
        if fmt is None or fd.is_list or fd.is_ref:
            raise Gff4Unsupported(f"writing field type {fd.type} flags {fd.flags:#x}")
        try:
            if isinstance(v, tuple):
                self.put(pos, fmt, *v)
            else:
                self.put(pos, fmt, 0 if v is None else v)
        except struct.error as e:
            raise Gff4Error(f"value {v!r} doesn't fit field {fd.label} (type {fd.type}): {e}") from e
