#!/usr/bin/env python3
"""ERX offline pre-linker for UFE3 (SLPS-25441) / PS2Recomp.

Turns every disc/ERX/*/*.ERX (59 modules) into a fully linked, fixed-address
image plus erx/out/manifest.json.  Python3 stdlib only (capstone used only for
optional deep checks; core checks are raw word scans).

Usage:  python3 erx/erx_prelink.py [--base 0x01C00000] [--end 0x01F00000]
                                   [--root <repo root>] [--out <dir>]
See erx/README.md for design notes, evidence, and open questions.
"""
import argparse, json, os, struct, sys
from collections import Counter, OrderedDict

R_MIPS_32, R_MIPS_26, R_MIPS_HI16, R_MIPS_LO16 = 2, 4, 5, 6
T_CUSTOM_HI, T_CUSTOM_SYM = 250, 251
BREAK_STUB_WORD = 0x0000004d          # break 0x0,0x1  (import stub marker)

# ---------------------------------------------------------------- ELF parsing

def read_file(path):
    with open(path, 'rb') as f:
        return f.read()


def elf_sections(d):
    assert d[:4] == b'\x7fELF' and d[4] == 1 and d[5] == 1, 'not ELF32 LSB'
    e_shoff = struct.unpack('<I', d[0x20:0x24])[0]
    e_shentsize, e_shnum, e_shstrndx = struct.unpack('<HHH', d[0x2e:0x34])
    sh = []
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        v = struct.unpack('<10I', d[o:o + 40])
        sh.append(dict(name=v[0], typ=v[1], flags=v[2], addr=v[3], off=v[4],
                       size=v[5], link=v[6], info=v[7], align=v[8], entsize=v[9]))
    stro = sh[e_shstrndx]['off']
    for s in sh:
        e = d.index(b'\0', stro + s['name'])
        s['nm'] = d[stro + s['name']:e].decode()
    return sh


def u32(buf, off):
    return struct.unpack('<I', buf[off:off + 4])[0]


def su16(v):
    return v - 0x10000 if v & 0x8000 else v

# ---------------------------------------------------------------- main ELF

class MainElf:
    """disc/SLPS_254.41: vaddr -> file offset = vaddr - 0x1FF000 (data/rodata)."""

    OFFS = 0x1FF000

    def __init__(self, path):
        self.d = read_file(path)
        self.sh = {s['nm']: s for s in elf_sections(self.d)}
        self.libs = self._parse_libs()          # name -> dict(ver, funcs)
        self.jal_targets = self._scan_jals()    # absolute vaddrs
        self.export_ptrs = set()
        for lib in self.libs.values():
            self.export_ptrs.update(lib['funcs'])

    def v2o(self, vaddr):
        return vaddr - self.OFFS

    def _str(self, vaddr):
        o = self.v2o(vaddr)
        return self.d[o:self.d.index(b'\0', o)].decode()

    def _parse_libs(self):
        s = self.sh['.erx.lib']
        buf, libs, off = self.d[s['off']:s['off'] + s['size']], OrderedDict(), 0
        while off + 0x20 <= len(buf):
            if buf[off:off + 6] != b'eRxLiB':
                off += 4
                continue
            name_ptr, ver = struct.unpack('<II', buf[off + 0x10:off + 0x18])
            name = self._str(name_ptr)
            o2, funcs = off + 0x20, []
            while o2 + 4 <= len(buf):
                w = u32(buf, o2)
                if w == 0xFFFFFFFF or buf[o2:o2 + 6] == b'eRxLiB':
                    break
                funcs.append(w)
                o2 += 4
            libs[name] = dict(ver=ver & 0xffff, ver_raw=ver, funcs=funcs, addr=s['addr'] + off)
            off = o2 + 4 if o2 < len(buf) and u32(buf, o2) == 0xFFFFFFFF else o2
        return libs

    def _scan_jals(self):
        s = self.sh['.text']
        out = set()
        for a in range(s['off'], s['off'] + s['size'] - 3, 4):
            w = u32(self.d, a)
            if w >> 26 == 3:                      # jal
                out.add((w & 0x3FFFFFF) << 2)
        return out

# ---------------------------------------------------------------- ERX module

class ErxModule:
    def __init__(self, path, name):
        self.path, self.name = path, name
        self.d = read_file(path)
        self.sh_list = elf_sections(self.d)
        self.sh = {s['nm']: s for s in self.sh_list}
        # deterministic layout: keep the ERX's own contiguous sh_addr layout
        self.img_size = max((s['addr'] + s['size'] for s in self.sh_list
                             if s['typ'] in (1, 8) and s['nm'] != ''), default=0)
        self.eemod = struct.unpack('<11I', self.d[self.sh['.eemod']['off']:
                                                  self.sh['.eemod']['off'] + 0x2c])
        self.reloc_counts = Counter()
        self.anomalies = []
        self.imports = []
        self.exports = []

    # ---- linking
    def link(self, base, main):
        img = bytearray(self.img_size)
        for s in self.sh_list:
            if s['typ'] == 1 and s['nm'] != '' and s['size']:
                img[s['addr']:s['addr'] + s['size']] = self.d[s['off']:s['off'] + s['size']]
        self._apply_relocs(img, base)
        self._parse_exports(img, base)
        self._parse_imports(img, base, main)
        self._patch_stubs(img, base, main)
        self._collect_funcs(img, base)
        return bytes(img)

    def _apply_relocs(self, img, base):
        for s in self.sh_list:
            if not s['nm'].startswith('.rel'):
                continue
            entries = [struct.unpack('<II', self.d[s['off'] + i * 8:s['off'] + i * 8 + 8])
                       for i in range(s['size'] // 8)]
            # Rel stream is grouped per symbol (not address-sorted).  One HI16
            # can feed several LO16 immediates (addiu and lw/sw offsets alike),
            # so the "current hi" persists until the next 5/250 entry.
            cur = None          # ('hi16', hoff, imm_h) | ('250', hoff, target)
            i = 0
            while i < len(entries):
                off, info = entries[i]
                typ = info & 0xff
                sym = info >> 8
                self.reloc_counts[typ] += 1
                if sym != 0:
                    self.anomalies.append(f'{s["nm"]}: sym!=0 at {off:#x}')
                if typ == R_MIPS_32:
                    val = u32(img, off)
                    img[off:off + 4] = struct.pack('<I', (val + base) & 0xFFFFFFFF)
                elif typ == R_MIPS_26:
                    w = u32(img, off)
                    tgt = (w & 0x3FFFFFF) << 2
                    if not (0 <= tgt < self.img_size):
                        self.anomalies.append(f'{s["nm"]}: R_MIPS_26 target {tgt:#x} '
                                              f'outside image at {off:#x}')
                    img[off:off + 4] = struct.pack('<I', (w & 0xFC000000) |
                                                   (((base + tgt) >> 2) & 0x3FFFFFF))
                elif typ == R_MIPS_HI16:
                    cur = ('hi16', off, u32(img, off) & 0xFFFF)
                elif typ == T_CUSTOM_HI:
                    # [250@lui][251@target-word][6@lo...] : pair forms (base + target)
                    if (i + 1 < len(entries) and
                            (entries[i + 1][1] & 0xff) == T_CUSTOM_SYM):
                        target = entries[i + 1][0]
                        self.reloc_counts[T_CUSTOM_SYM] += 1
                        cur = ('250', off, target)
                        i += 1
                    else:
                        self.anomalies.append(f'{s["nm"]}: 250 at {off:#x} without 251')
                        cur = ('hi16', off, u32(img, off) & 0xFFFF)
                elif typ == R_MIPS_LO16:
                    if cur is None:
                        self.anomalies.append(f'{s["nm"]}: LO16 at {off:#x} with no preceding HI')
                        i += 1
                        continue
                    self._patch_hl(img, base, cur, off)
                elif typ == T_CUSTOM_SYM:
                    self.anomalies.append(f'{s["nm"]}: stray 251 at {off:#x}')
                else:
                    self.anomalies.append(f'{s["nm"]}: unknown reloc type {typ} at {off:#x}')
                i += 1

    def _patch_hl(self, img, base, cur, loff):
        """Patch a HI16/LO16 (or 250/LO16) immediate so the pair forms (base + A)."""
        kind, hoff, hval = cur
        imm_l = u32(img, loff) & 0xFFFF
        if kind == '250':
            A = hval
        else:
            A = ((hval << 16) + su16(imm_l)) & 0xFFFFFFFF
            if not (0 <= A < self.img_size):
                self.anomalies.append(f'HI/LO pair value {A:#x} outside image '
                                      f'(hi@{hoff:#x} lo@{loff:#x})')
        final = (base + A) & 0xFFFFFFFF
        lo_word = u32(img, loff)
        new_lo = final & 0xFFFF
        if (lo_word >> 26) == 0x0D:                       # ori: unsigned lo
            new_hi = (final >> 16) & 0xFFFF
        else:                                             # addiu/load-offset
            new_hi = ((final - su16(new_lo)) >> 16) & 0xFFFF
        w = u32(img, hoff)
        img[hoff:hoff + 4] = struct.pack('<I', (w & 0xFFFF0000) | new_hi)
        img[loff:loff + 4] = struct.pack('<I', (lo_word & 0xFFFF0000) | new_lo)

    # ---- .erx.stub / .erx.lib
    def _gstr(self, vaddr):
        """module vaddr -> string in original file (vaddr + 0x100)."""
        o = vaddr + 0x100
        return self.d[o:self.d.index(b'\0', o)].decode()

    def _parse_imports(self, img, base, main):
        s = self.sh.get('.erx.stub')
        if not s:
            return
        for i in range(s['size'] // 0x20):
            e = self.d[s['off'] + i * 0x20:s['off'] + (i + 1) * 0x20]
            name_ptr, ver, a, b = struct.unpack('<IIII', e[0x10:0x20])
            libname = self._gstr(name_ptr)
            lib = main.libs.get(libname)
            for slot in range(a, b, 8):
                w1, w2 = struct.unpack('<II', self.d[0x100 + slot:0x108 + slot])
                if w1 == 0 and w2 == 0:
                    continue                   # trailing zero padding in stub range
                idx = w2 & 0xFFFF
                target = None
                if w1 != BREAK_STUB_WORD:
                    self.anomalies.append(f'stub slot {slot:#x} ({libname}#{idx}) not break 0,1')
                elif lib is None:
                    self.anomalies.append(f'import lib {libname} not in main .erx.lib')
                elif idx >= len(lib['funcs']):
                    self.anomalies.append(f'{libname}#{idx:#x} out of range '
                                          f'(lib has {len(lib["funcs"])})')
                else:
                    target = lib['funcs'][idx]
                self.imports.append(dict(slot=base + slot, slot_off=slot, library=libname,
                                         version=ver & 0xffff, index=idx,
                                         target=target,
                                         target_ok=(target in main.jal_targets or
                                                    target in main.export_ptrs)
                                                    if target is not None else False))

    def _parse_exports(self, img, base):
        s = self.sh.get('.erx.lib')
        if not s:
            return
        buf = bytes(img[s['addr']:s['addr'] + s['size']])
        off = 0
        while off + 0x20 <= len(buf):
            if buf[off:off + 6] != b'eRxLiB':
                off += 4
                continue
            name_ptr, ver = struct.unpack('<II', buf[off + 0x10:off + 0x18])
            name = self._gstr(name_ptr - base)     # name_ptr already relocated
            o2, funcs = off + 0x20, []
            while o2 + 4 <= len(buf):
                w = u32(buf, o2)
                if w == 0xFFFFFFFF or buf[o2:o2 + 6] == b'eRxLiB':
                    break
                funcs.append(w)
                o2 += 4
            self.exports.append(dict(library=name, version=ver & 0xffff, funcs=funcs))
            off = o2 + 4 if o2 < len(buf) and u32(buf, o2) == 0xFFFFFFFF else o2

    def _patch_stubs(self, img, base, main):
        """REPLACE each 8-byte break-stub with `j <target>; nop`."""
        for imp in self.imports:
            if imp['target'] is None:
                continue
            t = imp['target']
            off = imp['slot_off']
            img[off:off + 4] = struct.pack('<I', 0x08000000 | ((t >> 2) & 0x3FFFFFF))
            img[off + 4:off + 8] = b'\x00\x00\x00\x00'

    def _collect_funcs(self, img, base):
        funcs = set()
        s = self.sh['.text']
        buf = bytes(img[s['addr']:s['addr'] + s['size']])
        for a in range(0, len(buf) - 3, 4):
            w = u32(buf, a)
            if w >> 26 == 3:                       # jal
                tgt = (w & 0x3FFFFFF) << 2
                if base <= tgt < base + self.img_size:
                    funcs.add(tgt)
        for ex in self.exports:
            funcs.update(f for f in ex['funcs'])
        entry = self.eemod[0]
        if entry != 0xFFFFFFFF:
            funcs.add(base + entry)
        self.func_starts = sorted(funcs)

    # ---- verification
    def verify(self, img, base, main):
        problems = []
        s = self.sh['.text']
        buf = bytes(img[s['addr']:s['addr'] + s['size']])
        ext_targets = Counter()
        for a in range(0, len(buf) - 3, 4):
            w = u32(buf, a)
            op = w >> 26
            if op == 2 or op == 3:                 # j / jal
                tgt = (w & 0x3FFFFFF) << 2
                if not (base <= tgt < base + self.img_size):
                    if tgt in main.jal_targets or tgt in main.export_ptrs:
                        ext_targets[tgt] += 1
                    else:
                        problems.append(f'{"jal" if op == 3 else "j"} to unknown {tgt:#x} at '
                                        f'{base + a:#x}')
        # no remaining `break 0,1` import stubs
        for a in range(0, len(buf) - 7, 4):
            if u32(buf, a) == BREAK_STUB_WORD:
                w2 = u32(buf, a + 4)
                if w2 >> 26 == 9 and (w2 >> 16) & 0x1F == 0 and (w2 >> 11) & 0x1F == 0:
                    problems.append(f'unreplaced break-stub at {base + a:#x}')
        problems += self._verify_disasm(buf, base, main)
        return problems, ext_targets

    def _verify_disasm(self, buf, base, main):
        """Disassembly cross-check with capstone (if present): every decoded
        jal/j must land in-module or on a resolved main-ELF function."""
        try:
            from capstone import Cs, CS_ARCH_MIPS, CS_MODE_MIPS32, CS_MODE_LITTLE_ENDIAN
        except ImportError:
            return []
        md = Cs(CS_ARCH_MIPS, CS_MODE_MIPS32 + CS_MODE_LITTLE_ENDIAN)
        md.detail = False
        problems = []
        checked = 0
        off = 0
        while off < len(buf) - 3:          # resume past undecodable R5900 words
            got = 0
            for addr, size, mnemonic, op_str in md.disasm_lite(buf[off:], base + off):
                got += size
                checked += 1
                if mnemonic in ('j', 'jal'):
                    tgt = int(op_str, 16) & 0x0FFFFFFF
                    if not (base <= tgt < base + self.img_size) and \
                            tgt not in main.jal_targets and tgt not in main.export_ptrs:
                        problems.append(f'disasm {mnemonic} to unknown {tgt:#x} '
                                        f'at {addr:#x}')
                if mnemonic == 'break' and u32(buf, addr - base) == BREAK_STUB_WORD:
                    problems.append(f'disasm break 0,1 stub at {addr:#x}')
            off += got + 4
        self.disasm_checked = checked
        return problems

# ---------------------------------------------------------------- driver

def module_sort_key(path):
    name = os.path.splitext(os.path.basename(path))[0]
    if name.startswith('PL_'):
        return (0, int(name[3:]), name)
    if name.startswith('MISN_'):
        return (2, int(name[5:]), name)
    return (1, 0, name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', type=lambda x: int(x, 0), default=0x01C00000)
    ap.add_argument('--end', type=lambda x: int(x, 0), default=0x01F00000)
    ap.add_argument('--root', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    root, outdir = args.root, args.out or os.path.join(args.root, 'erx', 'out')
    os.makedirs(outdir, exist_ok=True)

    mainelf = MainElf(os.path.join(root, 'disc', 'SLPS_254.41'))
    print(f'main ELF: {len(mainelf.libs)} .erx.lib libraries, '
          f'{len(mainelf.jal_targets)} jal targets')

    paths = []
    for sub in ('PLAYER00', 'PLAYER20', 'MISN'):
        d = os.path.join(root, 'disc', 'ERX', sub)
        paths += [os.path.join(d, f) for f in os.listdir(d) if f.upper().endswith('.ERX')]
    paths.sort(key=module_sort_key)

    base, manifest, rows = args.base, [], []
    total_relocs = Counter()
    for p in paths:
        name = os.path.splitext(os.path.basename(p))[0]
        mod = ErxModule(p, name)
        base = (base + 0xFF) & ~0xFF                  # module start aligned 0x100
        img = mod.link(base, mainelf)
        problems, ext_targets = mod.verify(img, base, mainelf)
        bad_imports = [i for i in mod.imports if not i['target_ok']]
        verified = not problems and not mod.anomalies and not bad_imports

        with open(os.path.join(outdir, f'{name}.bin'), 'wb') as f:
            f.write(img)

        entry = None
        if mod.eemod[0] != 0xFFFFFFFF:
            entry = base + mod.eemod[0]
        layout = [dict(name=s['nm'], addr=base + s['addr'], size=s['size'],
                       file_off=None if s['typ'] == 8 else s['off'])
                  for s in mod.sh_list
                  if s['typ'] in (1, 8) and s['nm'] != '' and s['size'] > 0]
        manifest.append(OrderedDict(
            name=name, path=os.path.relpath(p, root).replace(os.sep, '/'),
            base=base, size=mod.img_size,
            image_range=[base, base + mod.img_size],
            section_layout=layout, entry=entry,
            eemod=[hex(x) for x in mod.eemod],
            function_starts=mod.func_starts,
            function_count=len(mod.func_starts),
            imports=mod.imports,
            exports=mod.exports,
            reloc_counts={str(k): v for k, v in sorted(mod.reloc_counts.items())},
            external_targets={hex(k): v for k, v in sorted(ext_targets.items())},
            anomalies=mod.anomalies + problems,
            unverified_imports=[f"{i['library']}#{i['index']:#x}@{i['slot']:#x}"
                                for i in bad_imports],
            verified=verified))
        total_relocs += mod.reloc_counts
        rows.append((name, base, mod.img_size, len(mod.func_starts),
                     len(mod.imports), sum(len(e['funcs']) for e in mod.exports),
                     'OK' if verified else 'UNVERIFIED'))
        base += mod.img_size

    total = base - args.base
    print(f'\nregion {args.base:#x}..{base:#x}  total {total:#x} ({total} bytes)  '
          f'limit {args.end:#x}')
    if base > args.end:
        print('FATAL: total does not fit below', hex(args.end))
        sys.exit(1)

    hdr = f'{"module":10s} {"base":>10s} {"size":>8s} {"funcs":>6s} {"impts":>6s} {"expts":>6s}  status'
    print(hdr)
    for r in rows:
        print(f'{r[0]:10s} {r[1]:#10x} {r[2]:#8x} {r[3]:6d} {r[4]:6d} {r[5]:6d}  {r[6]}')
    print('relocation totals by type:', dict(sorted(total_relocs.items())))

    with open(os.path.join(outdir, 'manifest.json'), 'w') as f:
        json.dump(dict(region_base=args.base, region_end=base, total_size=total,
                       main_erx_lib={k: dict(ver=v['ver'], n=len(v['funcs']))
                                    for k, v in mainelf.libs.items()},
                       modules=manifest), f, indent=1)
    print('wrote', os.path.join(outdir, 'manifest.json'))
    n_bad = sum(1 for r in rows if r[6] != 'OK')
    print(f'{len(rows) - n_bad}/{len(rows)} modules verified')
    return 0 if n_bad == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
