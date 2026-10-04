#!/usr/bin/env python3
"""Generate the bindErxImport() table: main-ELF .erx.stub slots x ERX module exports.

Reads disc/SLPS_254.41 (.erx.stub section at vaddr 0x4794E0) and
erx/out/manifest.json, then emits erx/step2/out/erx_bindings.json with one entry
per main-side import stub slot:

    { "library": "Tiga", "slot": 0x35B408, "index": 4, "module": "PL_26",
      "target": 0x01C6xxxx }

`slot` is the guest address of the 8-byte `break 0x0,0x1 ; addiu $zero,$zero,N`
slot in the main ELF (what callErxImport()/bindErxImport() key on), `index` is
N (the export word index, counting the .erx.lib leading start/stop quadruple as
words 0..3), and `target` is the module's export word N (absolute guest address
after pre-linking).

Notes for the game override (step 3):
  * every PL module exports a unique character library -> bind once at load,
  * all 20 MISN modules export the same library name "Misn": the "Misn" slots
    must be RE-bound to the currently started mission module (see README).
"""
import argparse
import json
import os
import struct
import sys

MAIN_V2F = 0x1FF000          # main ELF vaddr -> file offset (linear image)
ERX_STUB_VADDR = 0x4794E0    # main .erx.stub
ERX_STUB_SIZE = 0x560
BREAK_STUB_WORD = 0x0000004D


def u32(buf, off):
    return struct.unpack_from('<I', buf, off)[0]


def read_cstr(data, off):
    end = data.index(b'\x00', off)
    return data[off:end].decode('ascii')


def parse_main_stub_entries(d):
    """Yield (library, version, stub_code_start, stub_code_end) from main .erx.stub."""
    entries = []
    base = ERX_STUB_VADDR - MAIN_V2F
    for i in range(ERX_STUB_SIZE // 0x20):
        e = d[base + i * 0x20: base + (i + 1) * 0x20]
        if e[0:8] != b'ErXsTuB\x00':
            continue
        name_ptr, ver, code_start, code_end = struct.unpack('<IIII', e[0x10:0x20])
        name = read_cstr(d, name_ptr - MAIN_V2F)
        entries.append((name, ver & 0xFFFF, code_start, code_end))
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    with open(os.path.join(root, 'disc', 'SLPS_254.41'), 'rb') as f:
        main_elf = f.read()
    with open(os.path.join(root, 'erx', 'out', 'manifest.json')) as f:
        manifest = json.load(f)

    stub_entries = parse_main_stub_entries(main_elf)

    # main library -> [(slot_addr, index)]
    main_slots = {}
    for name, ver, code_start, code_end in stub_entries:
        slots = []
        addr = code_start
        while addr + 8 <= code_end:
            w1 = u32(main_elf, addr - MAIN_V2F)
            w2 = u32(main_elf, addr + 4 - MAIN_V2F)
            if w1 == BREAK_STUB_WORD and (w2 >> 26) == 9 and ((w2 >> 16) & 0x1F) == 0 and ((w2 >> 11) & 0x1F) == 0:
                slots.append((addr, w2 & 0xFFFF))
            addr += 8
        main_slots[name] = {'version': ver, 'code': [code_start, code_end], 'slots': slots}

    # module library -> module name + funcs
    exports = {}
    for mod in manifest['modules']:
        for ex in mod.get('exports', []):
            exports.setdefault(ex['library'], []).append(
                {'module': mod['name'], 'version': ex['version'], 'funcs': ex['funcs']})

    bindings = []
    unbound = []
    for lib, info in sorted(main_slots.items()):
        if lib in ('sysmem', 'loadcore', 'modload'):
            continue  # kernel imports: handled by the runtime, not ERX modules
        sources = exports.get(lib, [])
        if not sources:
            if info['slots']:
                unbound.append(lib)
            continue
        if len(sources) > 1:
            print(f'NOTE: library "{lib}" exported by multiple modules: '
                  f'{[s["module"] for s in sources]} (rebind on mission start)')
        for src in sources:
            for slot, idx in info['slots']:
                if idx >= len(src['funcs']):
                    print(f'WARN: {lib} slot 0x{slot:X} index {idx} out of range '
                          f'({len(src["funcs"])} funcs in {src["module"]})')
                    continue
                bindings.append({'library': lib, 'slot': slot, 'index': idx,
                                 'module': src['module'], 'target': src['funcs'][idx]})

    out = {
        'main_stub_entries': {n: {'version': i['version'], 'code': i['code'],
                                  'slots': [{'slot': s, 'index': idx} for s, idx in i['slots']]}
                              for n, i in sorted(main_slots.items())},
        'module_exports': {n: [{'module': s['module'], 'version': s['version'], 'funcs': s['funcs']}
                               for s in v] for n, v in sorted(exports.items())},
        'bindings': bindings,
        'libraries_without_module': unbound,
    }
    out_path = os.path.join(root, 'erx', 'step2', 'out', 'erx_bindings.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=1)

    total_slots = sum(len(i['slots']) for i in main_slots.values())
    print(f'main .erx.stub: {len(main_slots)} libraries, {total_slots} slots')
    print(f'module exports: {len(exports)} libraries')
    print(f'bindings: {len(bindings)} -> {out_path}')
    if unbound:
        print(f'libraries with slots but no module export: {unbound}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
