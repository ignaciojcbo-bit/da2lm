"""Minimal reader for GFF V3.2 files (.uti items, .utc creatures, .are areas...), enough to compare versions."""
import struct
def parse(d):
    h=struct.unpack('<4s4s12I',d[:56])
    so,sc,fo,fc,lo,lc,fdo,fdc,fio,fic,lio,lic=h[2:]
    structs=[struct.unpack('<3I',d[so+12*i:so+12*i+12]) for i in range(sc)]
    fields=[struct.unpack('<3I',d[fo+12*i:fo+12*i+12]) for i in range(fc)]
    labels=[d[lo+16*i:lo+16*i+16].rstrip(b'\0').decode('latin1') for i in range(lc)]
    def fdata(off,n): return d[fdo+off:fdo+off+n]
    def val(fi):
        t,li,dv=fields[fi]; lab=labels[li]
        if t in (0,1,2,3,4,5): v=dv & ((1<<[8,8,16,16,32,32][t])-1)
        elif t==8: v=struct.unpack('<f',struct.pack('<I',dv))[0]
        elif t in (6,7): v=struct.unpack('<'+('Q' if t==6 else 'q'),fdata(dv,8))[0]
        elif t==9: v=struct.unpack('<d',fdata(dv,8))[0]
        elif t==10: n=struct.unpack('<I',fdata(dv,4))[0]; v=fdata(dv+4,n).decode('latin1')
        elif t==11: n=fdata(dv,1)[0]; v=fdata(dv+1,n).decode('latin1')
        elif t==12:
            tot,sref,cnt=struct.unpack('<3I',fdata(dv,12)); v=('strref',sref)
        elif t==13: n=struct.unpack('<I',fdata(dv,4))[0]; v=fdata(dv+4,n).hex()[:40]
        elif t==14: v=st(dv)
        elif t==15:
            n=struct.unpack('<I',d[lio+dv:lio+dv+4])[0]
            v=[st(struct.unpack('<I',d[lio+dv+4+4*k:lio+dv+8+4*k])[0]) for k in range(n)]
        else: v=('type',t)
        return lab,v
    def st(si):
        typ,di,n=structs[si]
        if n==1: idx=[di]
        else: idx=list(struct.unpack(f'<{n}I',d[fio+di:fio+di+4*n])) if n else []
        return dict(val(i) for i in idx)
    return st(0)


def item_view(data: bytes):
    """(base fields, {property id: power}, {var: value}) of a .uti/.utc, for comparing versions."""
    g = parse(data)
    props = {p.get("PropertyID"): p.get("PropertyPower") for p in g.get("PropertiesList", []) or []}
    vars_ = {v.get("Name"): v.get("Value") for v in g.get("VarTable", []) or []}
    base = {k: v for k, v in g.items() if k not in ("PropertiesList", "VarTable") and not isinstance(v, (list, dict))}
    return base, props, vars_


def contains(a, b) -> bool:
    """True if version a keeps everything version b has (same base fields, all of b's properties)."""
    (ba, pa, _), (bb, pb, _) = a, b
    return ba == bb and all(k in pa and (pa[k] == v or (pa[k] or 0) >= (v or 0)) for k, v in pb.items())
