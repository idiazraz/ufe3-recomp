#!/usr/bin/env python3
"""Wrap pre-linked ERX module images (erx/out/<MOD>.bin) as minimal MIPS ELF32
ET_EXEC files so ps2_recomp can recompile them.

For each module:
  * one PT_LOAD segment at the module's fixed base,
  * one section per entry of the manifest's `section_layout` (PROGBITS, .text
    marked SHF_EXECINSTR), .sbss/.bss as SHT_NOBITS,
  * a .symtab with one STT_FUNC symbol per function start. The starts are the
    manifest's `function_starts` (internal jal targets + export pointers)
    UNION the internal `j`/`jal` targets re-scanned from the pre-linked .text
    (tail-called internal functions are not jal targets in the manifest scan).
    Symbol size = next start - start (authoritative ranges for the parser).

Also emits one ps2_recomp config per module (erx/step2/cfg/<MOD>.toml).

Usage: python3 erx/step2/erx_wrap.py [--root <repo>] [--modules PL_00,MISN_1]
"""
import argparse
import json
import os
import struct
import sys

EM_MIPS = 8
ET_EXEC = 2
SHT_PROGBITS = 1
SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_NOBITS = 8
SHF_WRITE = 1
SHF_ALLOC = 2
SHF_EXECINSTR = 4
STB_GLOBAL = 1
STT_FUNC = 2


def u32(buf, off):
    return struct.unpack_from('<I', buf, off)[0]


def scan_branch_targets(text, text_addr):
    """Internal j/jal targets of the pre-linked .text (absolute guest addrs)."""
    targets = set()
    for off in range(0, len(text) - 3, 4):
        w = u32(text, off)
        op = w >> 26
        if op in (2, 3):  # j / jal
            tgt = (w & 0x3FFFFFF) << 2
            if text_addr <= tgt < text_addr + len(text):
                targets.add(tgt)
    return targets


def build_elf(mod_name, img, base, layout, func_starts):
    """Return a minimal ET_EXEC ELF32 image wrapping `img` at `base`."""
    text = next((s for s in layout if s['name'] == '.text'), None)
    if text is None:
        raise SystemExit(f'{mod_name}: no .text in section_layout')

    # --- section list (index 0 is the mandatory NULL section) ---
    sec_names = ['.null'] + [s['name'] for s in layout] + ['.symtab', '.strtab', '.shstrtab']
    sec_index = {n: i for i, n in enumerate(sec_names)}

    # --- string tables ---
    def build_strtab(names):
        out = b'\x00'
        offs = {}
        for n in names:
            offs[n] = len(out)
            out += n.encode() + b'\x00'
        return out, offs

    sym_names = [f'sub_{a:08X}' for a in func_starts]
    strtab, stroffs = build_strtab(sym_names)
    shstrtab, shstroffs = build_strtab(sec_names)

    # --- file layout ---
    off = 0x54  # ehdr (52) + one phdr (32) -> 0x54
    off = (off + 0xF) & ~0xF
    img_off = off
    off += len(img)
    off = (off + 3) & ~3
    symtab_off = off
    nsyms = 1 + len(func_starts)
    off += 16 * nsyms
    strtab_off = off
    off += len(strtab)
    shstrtab_off = off
    off += len(shstrtab)
    off = (off + 3) & ~3
    shoff = off
    nsh = len(sec_names)
    off += 40 * nsh

    out = bytearray(off)

    # --- ELF header ---
    out[0:16] = b'\x7fELF\x01\x01\x01\x00' + b'\x00' * 8
    struct.pack_into('<HHIIIIIHHHHHH', out, 16,
                     ET_EXEC, EM_MIPS, 1,
                     0,               # e_entry (ERX modules have no entry)
                     0x34,            # e_phoff
                     shoff,           # e_shoff
                     0x20920001,      # e_flags (noreorder | R5900 | mips3)
                     52, 32, 1,       # e_ehsize, e_phentsize, e_phnum
                     40, nsh, sec_index['.shstrtab'])

    # --- program header: one PT_LOAD at the module base ---
    struct.pack_into('<IIIIIIII', out, 0x34,
                     1,               # PT_LOAD
                     img_off,         # p_offset
                     base,            # p_vaddr
                     base,            # p_paddr
                     len(img),        # p_filesz
                     len(img),        # p_memsz
                     7,               # PF_R|PF_W|PF_X
                     0x100)           # p_align

    # --- module image ---
    out[img_off:img_off + len(img)] = img

    # --- symtab ---
    pos = symtab_off  # null symbol
    struct.pack_into('<IIIBBH', out, pos, 0, 0, 0, 0, 0, 0)
    pos += 16
    text_shndx = sec_index['.text']
    for i, (a, name) in enumerate(zip(func_starts, sym_names)):
        nxt = func_starts[i + 1] if i + 1 < len(func_starts) else base + len(img)
        size = max(4, nxt - a)
        struct.pack_into('<IIIBBH', out, pos,
                         stroffs[name], a, size,
                         (STB_GLOBAL << 4) | STT_FUNC, 0, text_shndx)
        pos += 16

    out[strtab_off:strtab_off + len(strtab)] = strtab
    out[shstrtab_off:shstrtab_off + len(shstrtab)] = shstrtab

    # --- section headers ---
    def shdr(i, name, typ, flags, addr, offset, size, link=0, info=0, align=4, entsize=0):
        struct.pack_into('<IIIIIIIIII', out, shoff + 40 * i,
                         shstroffs[name], typ, flags, addr, offset, size,
                         link, info, align, entsize)

    shdr(0, '.null', 0, 0, 0, 0, 0, align=0)
    for i, s in enumerate(layout):
        idx = i + 1
        is_text = s['name'] == '.text'
        is_bss = s['name'] in ('.sbss', '.bss')
        flags = SHF_ALLOC | (SHF_EXECINSTR if is_text else SHF_WRITE)
        typ = SHT_NOBITS if is_bss else SHT_PROGBITS
        shdr(idx, s['name'], typ, flags,
             s['addr'], img_off + (s['addr'] - base), s['size'],
             align=16 if is_text else 4)
    shdr(sec_index['.symtab'], '.symtab', SHT_SYMTAB, 0, 0,
         symtab_off, 16 * nsyms,
         link=sec_index['.strtab'], info=1, align=4, entsize=16)
    shdr(sec_index['.strtab'], '.strtab', SHT_STRTAB, 0, 0, strtab_off, len(strtab), align=1)
    shdr(sec_index['.shstrtab'], '.shstrtab', SHT_STRTAB, 0, 0, shstrtab_off, len(shstrtab), align=1)
    return bytes(out)


def write_toml(path, elf_path, out_dir):
    with open(path, 'w') as f:
        f.write(f'''# Generated by erx/step2/erx_wrap.py -- ps2_recomp config for one ERX module.
[general]
input = "{elf_path}"
ghidra_output = ""
output = "{out_dir}"
single_file_output = false
patch_syscalls = false
patch_cop0 = true
patch_cache = true
stubs = []
entry_points = []
''')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    ap.add_argument('--modules', default='', help='comma-separated module names (default: all)')
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    step2 = os.path.join(root, 'erx', 'step2')
    wrap_dir = os.path.join(step2, 'wrap')
    cfg_dir = os.path.join(step2, 'cfg')
    gen_dir = os.path.join(step2, 'gen')
    os.makedirs(wrap_dir, exist_ok=True)
    os.makedirs(cfg_dir, exist_ok=True)

    with open(os.path.join(root, 'erx', 'out', 'manifest.json')) as f:
        manifest = json.load(f)

    wanted = set(x for x in args.modules.split(',') if x)
    count = 0
    for mod in manifest['modules']:
        name = mod['name']
        if wanted and name not in wanted:
            continue
        bin_path = os.path.join(root, 'erx', 'out', name + '.bin')
        img = open(bin_path, 'rb').read()
        base = mod['base']

        layout = sorted(mod['section_layout'], key=lambda s: s['addr'])
        text = next(s for s in layout if s['name'] == '.text')
        text_bytes = img[text['addr'] - base: text['addr'] - base + text['size']]

        starts = set(mod['function_starts'])
        starts |= scan_branch_targets(text_bytes, text['addr'])
        starts = sorted(a for a in starts if text['addr'] <= a < text['addr'] + text['size'])
        if not starts:
            raise SystemExit(f'{name}: no function starts found')

        elf = build_elf(name, img, base, layout, starts)
        elf_path = os.path.join(wrap_dir, name + '.elf')
        with open(elf_path, 'wb') as f:
            f.write(elf)

        write_toml(os.path.join(cfg_dir, name + '.toml'),
                   elf_path, os.path.join(gen_dir, name) + os.sep)
        count += 1
        print(f'{name}: base=0x{base:08X} size=0x{len(img):X} funcs={len(starts)} elf={len(elf)}B')

    print(f'wrapped {count} modules')
    return 0


if __name__ == '__main__':
    sys.exit(main())
