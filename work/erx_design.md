# UFE3 (SLPS-25441) ERX / liberx — Design Study for PS2Recomp

Read-only analysis, ~40 min. Main ELF: `disc/SLPS_254.41` (MIPS R5900, stripped).
Full disassembly: `work/re/main.s` (llvm-objdump format). Data/rodata vaddr→file offset = vaddr − 0x1FF000
(.data vaddr 0x3b2580, .rodata 0x485d80, .sdata 0x4b2d80, .sbss 0x4bd280, .bss 0x4bd980;
`.erx.stub` vaddr 0x4794e0 size 0x560, `.erx.lib` vaddr 0x483600 size 0x25e4).

**Headline result**: UFE3 does *not* lazy-load ERX modules on demand — it **eagerly loads all 59 ERX
modules at boot** through kernel `modload` stubs that route into Sony liberx (ELOADP driver). The
main ELF imports each module's exports through per-character stub code, and each module imports the
main ELF's `baseelf`/`libvu0` exports through 8-byte `break`-based stubs. This makes the recommended
AOT design (fixed load address per module + hooking the loader) straightforward: there are only 59
modules, all resident simultaneously, each loaded exactly once.

Note: the prompt said 62 modules; the disc actually contains **59**: `PLAYER00/PL_00..PL_19` (20),
`PLAYER20/PL_20..PL_38` (19), `MISN/MISN_1..MISN_20` (20). (`PL_37`, `PL_38` exist under PLAYER20.)

---

## 0. ERX file format (established from `disc/ERX/PLAYER00/PL_00.ERX`)

ELF32 LSB, machine "MIPS R3000" (R5900 flags `0x20920001`: noreorder|5900|mips3), **type 0xFF91**
(relocatable EEMOD). Entry field in ELF header = `0xFFFFFFFF` (unused; see `.eemod`).

Sections: `.eemod` (SHT_LOPROC+0x90 metadata), `.text`, `.rel.text`, `.eh_frame` (+`.rel`),
`.erx.lib` (+`.rel.erx.lib`), `.erx.stub` (+`.rel.erx.stub`), `.rodata` (+`.rel`), `.data` (+`.rel`),
`.sdata`, `.sbss`, `.bss`. One PT_LOAD segment: file off 0x100 → vaddr 0, size ≈ whole image
(module vaddr → file offset = vaddr + 0x100). `.symtab` is a single dummy symbol; **all relocations
use sym index 0** — binding is by section + convention, not symbol names.

`.eemod` (0x2c bytes), PL_00 example (11 LE words):
`0xffffffff, 0xffffffff, 0x17638, 0x2934, 0xcd3c, 8, 0x3614, 0x4c, 0x3660, 0x40, 0`
= [entry=none, ?=none, field2 (per-module, ≈ 0x10000–0x1d000; exact meaning not confirmed — likely
requested/total allocation size or ID), .eh_frame vaddr 0x2934, field4 (offset in .data), 8 (align?),
.erx.lib vaddr+size (0x3614/0x4c), .erx.stub vaddr+size (0x3660/0x40), 0].

### .erx.stub entry (32 B) — imports
`"ErXsTuB\0" | 0 | 0 | name_ptr | version | stub_code_start | stub_code_end`
PL_00: `("libvu0", 0x0101, code 0x18–0x38)`, `("baseelf", 0x0102, code 0x48–0xb40)`.
`stub_code_*` is a vaddr range in the module's `.text` containing one **8-byte import stub per
imported function**: `break 0x0,0x1` + `addiu $zero,$zero,N`, where N is the function's index in the
named library's export table (verified in main.s: 304 `break 0,1` sites, and module `.text` begins
with these pairs). Liberx patches the `break` with a jump to the resolved export (or leaves it; the
break trap then goes to the unlinked-call handler, see 0x372850).

### .erx.lib entry (variable) — exports
`"eRxLiB\0A" | pad | name_ptr | version | 4 ptrs (start/stop etc., often duplicated) | N × func_ptr |
0xFFFFFFFF`. PL_00 exports library `"Man"` v0x0102 with 10 functions. Every PL module exports exactly
one library named after the character (`Man Sev Jack Ace Taro Leo Zof Bal Kj Bem Ak Tai Mag Gomo Zet
Dada Eity Tiga Dyna Gaia Agul Cos Jus Lege Golz Rei Ganq Gb Rk Gudo Twin Vaki Evil Robo Dsev Idyna
Astra Gata Sb`); every MISN module exports `"Misn"` v0x0101 with 9 functions.

### Relocation types seen (per module, all sym=0)
`R_MIPS_26` (internal j/jal), `R_MIPS_32` (data pointers, bulk of them: 1129–1656 per PL module),
`R_MIPS_HI16`/`R_MIPS_LO16` pairs, and rare custom types **250/251** (1–2 per some modules).
`.rel.erx.lib`/`.rel.erx.stub` are all `R_MIPS_32` (patching name/func pointers to the final base).

---

## Q1. liberx in the main ELF — key functions

liberx ("PsIIliberx 3000", string 0x4a2338) lives in `.text` **0x369978–0x375000** (the recompiler's
function splitter merges much of it into one block; real sub-functions are visible via `jal` targets
and string xrefs). All string xrefs below verified with a lui/addiu-pair scanner over `work/re/main.s`.

| address | evidence | role |
|---|---|---|
| 0x369978–0x36b000 | xrefs 0x36ad70–0x36af4c to "PsIIliberx 3000", "liberx: sceKernlErxExport(err=%08x)", "rom0:D2ELOADP300", "rom0:D2ELOADP", "liberx: ELOADP is not available" | **liberx init**: calls kernel `sceKernlErxExport` to obtain `sysmem/loadcore/modload` export tables (main's .erx.stub imports), loads/attaches the **ELOADP** loader driver from `rom0:D2ELOADP300` (fall back `rom0:D2ELOADP`). |
| 0x377938–0x378330 | 0x37823c–0x378244 builds "SceErx ELOADP driver" name, 0x378274 sets driver ops | **ELOADP driver registration** (module-loader driver handed to the kernel; the kernel `modload` LoadModule calls dispatch here). |
| 0x36e150–0x36e280 | 0x36e194/0x36e1c8/0x36e214: creates objects named "SceErxModuleLoaderMutex", "SceErxModuleLoaderSync", "SceErxModuleLoaderThread" (via `sceKernelCreateSema/Thread`-style calls to 0x374960) | **loader-service init**: mutex + sync + dedicated loader thread. |
| 0x36e000–0x36e150 and 0x36e320+ | calls 0x36eb58 from many branches (0x36e368…0x36e550) | request-queue processing in the loader thread (load/start/stop/unload jobs). |
| ~0x36e800–0x36ea00 | panic strings "sceErxStopUnloadModule(): panic !!! called from target module !!!" (0x36e83c), "…Async…" (0x36e8ec), "sceErxSelfStopUnloadModule(): panic !!! call from unknown Module !!!" (0x36e9a4), "…Unexpected case!!!" (0x36e9e8) | **stop/unload API** (`sceErxStopUnloadModule`, `…Async`, `sceErxSelfStopUnloadModule`) — panic if called from the module being unloaded. |
| ~0x36f100–0x36f500 | "sceErxStartModule(): panic !!! Illegal .erx.lib" (xref 0x36f1b8), "…library relase fail error=%d" (0x36f458) | **`sceErxStartModule`**: validates the module's `.erx.lib`, registers its export library, links it to importers. |
| ~0x36f800 | "sceErxStopModule(): panic !!! library relase fail error=%d" (xref 0x36f868) | **`sceErxStopModule`**: unregisters/releases the module's library. |
| 0x36fc00–0x370200 | `jal 0x372218` (sysmem stub idx 8 = **AllocSysMemory**) at 0x36fcc4/0x36fe78/0x370474/0x370aa0/0x370b08; `jal 0x372220` (idx 9 = **FreeSysMemory**) at 0x36fd10/0x36feb8/0x370150/0x370190/0x3701c8/0x37054c/0x3706f8/0x370b68 | **module memory allocator / loader core**: image is copied and relocated here; memory comes from the kernel heap (`AllocSysMemory`), *not* a fixed address. |
| 0x372850–0x373080 | "PANIC!! Unlinked function call was occured." (xref 0x372c14), "called from: %08x,unknown because liberx hasn't be initialized" (xref 0x372cec); called from game code at 0x354780 | **unlinked-stub handler** (break-exception path): resolves/lazy-binds `break 0,1` stubs, panics when a stub is executed before its library is linked. |
| 0x372200–0x372490 | `.erx.stub` code ranges of entries `sysmem` (0x372200–0x372268), `loadcore` (0x372278–0x3723b8), `modload` (0x3723c8–0x372480) | **kernel import stubs** (main ELF imports these libs from the kernel via `sceKernlErxExport`): each 8 B `break; addiu $zero,$zero,idx`. 0x372218 = sysmem#8 AllocSysMemory, 0x372220 = #9 FreeSysMemory, 0x372230 = #0xb, 0x372348 = loadcore#0x2b (RegisterLibraryEntries-like), 0x3723e0/0x372420/0x372430/0x372440 = modload#… (LoadModule family). |
| 0x36acf8 | called once from 0x2762c0 | liberx API entry used at game init (register a library / sceErxInit-style). |

Game-side helpers:

| address | role |
|---|---|
| **0x276298–0x276404** | **erx system init** (called from startup at **0x2323e8**, right after the IRX loads at 0x232110–0x232224): calls 0x36acf8, then registers ~18 game libraries with liberx (pairs of `lib-register 0x3743c8/0x3743f8/0x374438/…` + `loadcore#0x2b 0x372348`), passing `.erx.lib` entry addresses (0x483600 "baseelf", 0x483d14, …) — i.e. **main ELF publishes its `.erx.lib` export tables to the kernel/liberx linker**. |
| **0x276408–0x276504** | **load-all-modules loop** (own prologue; caller likely via function pointer from the startup/task chain): for each of the 59 descriptors at **0x3d3468**: call `modload` stub 0x3723e0(path, flags|0x100, 0) storing handle into state array 0x3d32e8, retry on negative; then call `modload` stub 0x372420(state, path, 0, struct 0x4b4598) per module (increments a counter at descriptor+0xc). Infinite self-loop `0x2764e4: b 0x2764e4` on hard failure (boot hang). |

---

## Q2. Loading flow

1. **Path source**: static table of **59 × 0x14-byte descriptors at 0x3d3468** (.data): `+0` = pointer
   to `cdrom0:\ERX\{PLAYER00|PLAYER20|MISN}\{PL_xx|MISN_xx}.ERX;1` string (strings at 0x48b958–0x48c1d0,
   0x28 spacing), `+4..+0x10` = runtime state (all zero in the file). A parallel state array at
   0x3d32e8 holds handles returned by the load calls.
2. **Who reads the file**: the game calls kernel `modload` exports through its 8-byte stubs
   (0x3723e0 = load-by-path, 0x372420 = start/attach; exact index semantics not fully disassembled).
   `modload` dispatches to the registered **ELOADP driver** (registered by liberx at 0x377938,
   name "SceErx ELOADP driver") which is liberx's loader. liberx itself was initialized at boot:
   `sceKernlErxExport` (string xref 0x36ae34) supplies `sysmem/loadcore/modload` tables, and
   `rom0:D2ELOADP300` is the kernel-side load engine ("liberx: ELOADP is not available" is fatal).
3. **Memory allocation**: **kernel heap via `AllocSysMemory`** (sysmem stub 0x372218, called from the
   liberx loader core at 0x36fcc4/0x36fe78/0x370474/0x370aa0/0x370b08; frees at 0x372220). Not fixed
   addresses — the image base is whatever the kernel heap returns (consistent with `.eemod` field2
   being an allocation size). This is why the same module can in principle be loaded twice at
   different addresses, but the game's one-descriptor-per-module design (fixed 0x3d3468 table,
   library names like "Tiga" registered globally) means **each module is loaded exactly once** —
   two copies would collide at library registration ("sceErxStartModule(): panic !!! Illegal .erx.lib").
4. **Relocation**: liberx applies the `.rel.*` sections (R_MIPS_26 to instruction targets, R_MIPS_32
   to data words, HI16/LO16 pairs, custom 250/251) with sym index 0 = "add load base". `.rel.erx.lib`
   and `.rel.erx.stub` fix up the name/function pointers inside those tables.
5. **Linking**:
   - *Module → main*: module `.erx.stub` entries ("libvu0", "baseelf") are matched by name+version
     against registered libraries; each 8-byte `break;idx` stub in the module's `.text` range is bound
     to the `idx`-th function pointer of that library's `.erx.lib` table in the **main ELF**
     (main exports 34 libraries: `baseelf basemisn libgraph libcdvd libdma libipu libdev libvu0 libpkt
     sysmem loadcore modload libpad LibgccCommon LibcMalloc LibcStdio LibcStdlib LibcString LibmDouble
     LibmDoubleExt LibmFloat LibmFloatExt eekernel eepatch eetlb eetty sifcmd fileio iopldfl iopreset
     iopsmem deci2api eetimer glue` — full list read from main's `.erx.lib`).
   - *Main → module*: main's `.erx.stub` entries (39 character libs + `Misn` + `sysmem/loadcore/modload`)
     each own a stub-code range in main's `.text` (e.g. "Tiga" 0x35b408–0x35b448, "Misn" 0x35ba18–0x35ba38).
     When a module starts, `sceErxStartModule` registers its `.erx.lib` ("Tiga" etc.) and binds these
     stubs to the module's exported function pointers.
   - *Module → module*: no evidence of any (no PL module imports anything but `baseelf`/`libvu0`; MISN
     modules import only `basemisn`). All cross-module traffic goes through the main ELF.
6. **Calling into a module**: the game calls the main-side stub functions (0x35af50–0x35ba38 character
   stubs, one ~0x30–0x48-byte stub function per imported function) or dereferences the module's export
   function pointers from its `.erx.lib` entry. If unbound, the stub's `break 0,1` traps into 0x372850
   ("PANIC!! Unlinked function call"). No evidence of vtables across the boundary — it is a pure
   function-pointer/stub ABI (each PL lib exports 9–12 functions; Misn exports 9).

---

## Q3. Module inventory (59 modules)

Script used (`python3` + struct parsing; **pyelftools is NOT installed** — readelf/llvm-objdump are):

```python
# /tmp/opencode/erx/inv2.py (self-contained; run from repo root)
import struct, glob, os
from collections import Counter
def parse(path):
    d = open(path,'rb').read()
    e_shoff = struct.unpack('<I', d[0x20:0x24])[0]
    e_shentsize, e_shnum, e_shstrndx = struct.unpack('<HHH', d[0x2e:0x34])
    sh = []
    for i in range(e_shnum):
        o = e_shoff + i*e_shentsize
        v = struct.unpack('<10I', d[o:o+40])
        sh.append(dict(name=v[0],typ=v[1],addr=v[3],off=v[4],size=v[5]))
    stro = sh[e_shstrndx]['off']
    for s in sh:
        e = d.index(b'\0', stro+s['name']); s['nm'] = d[stro+s['name']:e].decode()
    byn = {s['nm']: s for s in sh}
    def gstr(vaddr):                     # module vaddr -> file offset = vaddr + 0x100
        o = vaddr + 0x100
        try:    return d[o:d.index(b'\0', o)].decode()
        except Exception: return f'?{vaddr:#x}'
    relc = {}
    for s in sh:
        if s['nm'].startswith('.rel'):
            tc = Counter()
            for i in range(s['size']//8):
                off, info = struct.unpack('<II', d[s['off']+i*8:s['off']+i*8+8])
                tc[info & 0xff] += 1
            relc[s['nm'][4:]] = tc
    stubs = []                           # imports
    s = byn.get('.erx.stub')
    if s:
        for i in range(s['size']//0x20):
            e = d[s['off']+i*0x20 : s['off']+(i+1)*0x20]
            namep, ver, a, b = struct.unpack('<IIII', e[0x10:0x20])
            stubs.append((gstr(namep), ver & 0xffff, a, b))
    libs = []                            # exports: (name, version, #funcs)
    s = byn.get('.erx.lib')
    if s:
        buf = d[s['off']:s['off']+s['size']]; off = 0
        while off + 0x18 <= len(buf):
            if buf[off:off+8][:6] == b'eRxLiB':
                namep, ver = struct.unpack('<II', buf[off+0x10:off+0x18])
                o2, nexp = off + 0x20, 0
                while o2 + 4 <= len(buf):
                    if buf[o2:o2+8][:6] == b'eRxLiB': break
                    w = struct.unpack('<I', buf[o2:o2+4])[0]
                    if w == 0xffffffff: o2 += 4; break
                    nexp += 1; o2 += 4
                libs.append((gstr(namep), ver & 0xffff, nexp)); off = o2
            else: off += 4
    eem = byn.get('.eemod')
    eemod = struct.unpack('<11I', d[eem['off']:eem['off']+0x2c]) if eem else ()
    return dict(sh={s['nm']:s for s in sh}, relc=relc, stubs=stubs, libs=libs, eemod=eemod)
for f in sorted(glob.glob('disc/ERX/*/*.ERX')):
    r = parse(f); sh = r['sh']
    sz = lambda n: sh.get(n, {'size':0})['size']
    tc = Counter()
    for c in r['relc'].values(): tc += c
    print(f, hex(sz('.text')), hex(sz('.data')), hex(sz('.sdata')),
          hex(sz('.sbss')+sz('.bss')), dict(tc), r['stubs'], r['libs'], [hex(x) for x in r['eemod']])
```

Table (`relocs 26/32/hi/lo/oth`; sizes in hex bytes; imports shown as `lib:stubcode-bytes`;
exports as `lib:#funcs`; `.eh_frame` omitted — it's 0x20–0xce0 per module):

| module | text | data | sdata | bss | rodata | relocs 26/32/hi/lo/oth | imports | exports (lib:#funcs) |
|---|---|---|---|---|---|---|---|---|
| MISN_1  | 3e4 | 1208 | 0 | c | e | 12/27/5/5/0 | basemisn:50 | Misn:9 |
| MISN_2  | 97c | 10c0 | 18 | 10 | e | 40/33/14/14/0 | basemisn:80 | Misn:9 |
| MISN_3  | 7cc | 10c0 | 0 | 14 | e | 44/34/4/4/0 | basemisn:68 | Misn:9 |
| MISN_4  | 4cc | f30 | 0 | 10 | e | 19/27/4/4/0 | basemisn:60 | Misn:9 |
| MISN_5  | 7fc | 27e8 | 0 | 10 | 45 | 32/74/5/7/0 | basemisn:50 | Misn:9 |
| MISN_6  | 554 | 1490 | 0 | c | e | 23/39/4/5/2{250:1,251:1} | basemisn:58 | Misn:9 |
| MISN_7  | 37c | cc0 | 4 | c | e | 13/25/4/4/0 | basemisn:50 | Misn:9 |
| MISN_8  | 824 | be0 | 0 | 40 | e | 28/26/12/12/0 | basemisn:70 | Misn:9 |
| MISN_9  | 524 | b08 | 0 | c | e | 27/29/4/4/0 | basemisn:78 | Misn:9 |
| MISN_10 | 7a4 | 4dc8 | 4 | 14 | e | 37/71/12/12/0 | basemisn:68 | Misn:9 |
| MISN_11 | 3d4 | b48 | 0 | c | e | 17/28/4/4/0 | basemisn:58 | Misn:9 |
| MISN_12 | 19c | 398 | 0 | c | e | 3/17/2/2/0 | basemisn:28 | Misn:9 |
| MISN_13 | 454 | b20 | 4 | c | e | 15/29/5/5/0 | basemisn:50 | Misn:9 |
| MISN_14 | 604 | 1578 | 0 | c | e | 34/39/7/7/0 | basemisn:78 | Misn:9 |
| MISN_15 | 69c | dd0 | 0 | c | e | 37/33/4/4/0 | basemisn:60 | Misn:9 |
| MISN_16 | 60c | 1078 | 0 | 10 | e | 25/30/5/5/0 | basemisn:70 | Misn:9 |
| MISN_17 | 204 | 2a0 | 0 | 10 | 85 | 1/47/3/3/0 | basemisn:18 | Misn:9 |
| MISN_18 | 4e4 | 2ac0 | 0 | 10 | e | 15/24/4/4/0 | basemisn:58 | Misn:9 |
| MISN_19 | 4fc | 7d8 | 4 | c | 25 | 17/32/7/7/0 | basemisn:60 | Misn:9 |
| MISN_20 | 5b4 | 1390 | 0 | c | e | 24/38/6/6/0 | basemisn:50 | Misn:9 |
| PL_00 Man   | 2914 | bea8 | 28 | 8 | fc | 170/1577/31/35/2{250:1,251:1} | libvu0:20,baseelf:af8 | Man:10 |
| PL_01 Sev   | 2a2c | c2f8 | 28 | 8 | bc | 184/1528/28/31/2{250:1,251:1} | libvu0:18,baseelf:ae0 | Sev:10 |
| PL_02 Jack  | 348c | c1b0 | 70 | 8 | 1c5 | 250/1597/55/57/0 | libvu0:28,baseelf:b18 | Jack:10 |
| PL_03 Ace   | 2cc4 | f0e0 | 28 | c | dc | 208/1588/35/37/0 | libvu0:18,baseelf:af0 | Ace:10 |
| PL_04 Taro  | 2bcc | 9ab0 | 30 | 8 | 185 | 185/1589/38/40/0 | libvu0:18,baseelf:b10 | Taro:10 |
| PL_05 Leo   | 3a94 | b160 | 80 | c | 64 | 284/1546/55/57/0 | libvu0:48,baseelf:b18 | Leo:10 |
| PL_06 Zof   | 2974 | 7370 | 38 | 8 | a4 | 170/1496/26/28/0 | libvu0:18,baseelf:ad8 | Zof:10 |
| PL_07 Bal   | 28d4 | 8788 | 40 | 8 | 54 | 172/1494/25/27/0 | libvu0:20,baseelf:af0 | Bal:10 |
| PL_08 Kj    | 31bc | 7568 | 60 | 8 | 53 | 235/1557/34/38/4{250:2,251:2} | libvu0:18,baseelf:ac8 | Kj:10 |
| PL_09 Bem   | 300c | 8368 | b0 | 8 | 1f4 | 180/1598/68/71/2{250:1,251:1} | libvu0:18,baseelf:ae0 | Bem:10 |
| PL_10 Ak    | 28e4 | 8718 | 38 | 8 | 103 | 172/1537/25/27/0 | libvu0:18,baseelf:af0 | Ak:10 |
| PL_11 Tai   | 2d8c | 6210 | 40 | 8 | 5c | 213/1528/22/24/0 | libvu0:18,baseelf:af0 | Tai:10 |
| PL_12 Mag   | 2964 | 74c8 | 50 | 8 | 54 | 172/1511/27/29/0 | libvu0:18,baseelf:b00 | Mag:10 |
| PL_13 Gomo  | 1e1c | 70a0 | 40 | 8 | ad | 141/1482/19/21/0 | baseelf:ae8 | Gomo:9 |
| PL_14 Zet   | 2d64 | 8328 | 70 | 8 | 17c | 183/1571/42/44/0 | libvu0:18,baseelf:ae8 | Zet:10 |
| PL_15 Dada  | 28c4 | 7c60 | 40 | 8 | b5 | 169/1684/21/23/0 | libvu0:20,baseelf:ae8 | Dada:9 |
| PL_16 Eity  | 2aac | 95b0 | 30 | 8 | 11d | 186/1552/30/33/2{250:1,251:1} | libvu0:18,baseelf:ad8 | Eity:10 |
| PL_17 Tiga  | 370c | 10a70 | 90 | 8 | 175 | 238/1656/77/81/4{250:2,251:2} | libvu0:18,baseelf:b08 | Tiga:11 |
| PL_18 Dyna  | 2c54 | a048 | 48 | 8 | 1bd | 197/1640/36/38/0 | libvu0:20,baseelf:b00 | Dyna:11 |
| PL_19 Gaia  | 32a4 | a7e0 | 98 | c | 12d | 217/1606/65/67/0 | libvu0:28,baseelf:b20 | Gaia:12 |
| PL_20 Agul  | 3164 | 92f8 | 68 | 10 | 165 | 207/1587/50/54/4{250:2,251:2} | libvu0:18,baseelf:b00 | Agul:10 |
| PL_21 Cos   | 30ec | 9c28 | 50 | 8 | 94 | 226/1626/45/47/0 | libvu0:28,baseelf:b28 | Cos:11 |
| PL_22 Jus   | 300c | 8a80 | 88 | 8 | 13c | 202/1541/66/69/2{250:1,251:1} | libvu0:28,baseelf:af0 | Jus:11 |
| PL_23 Lege  | 230c | 5918 | 28 | 8 | 4d | 135/1457/16/18/0 | libvu0:18,baseelf:ae0 | Lege:10 |
| PL_24 Golz  | 289c | 6de0 | 40 | 8 | 5d | 174/1478/23/25/0 | libvu0:18,baseelf:ae0 | Golz:10 |
| PL_25 Rei   | 2934 | 7c48 | 40 | 8 | 15c | 169/1563/24/26/0 | libvu0:18,baseelf:ae8 | Rei:9 |
| PL_26 Ganq  | 2ea4 | 8228 | 70 | 8 | b5 | 194/1527/42/46/4{250:2,251:2} | libvu0:18,baseelf:ae0 | Ganq:10 |
| PL_27 Gb    | 2684 | 69a0 | 40 | 8 | 53 | 157/1466/18/20/0 | libvu0:18,baseelf:a98 | Gb:9 |
| PL_28 Rk    | 1ae4 | 77a0 | 40 | 8 | 4b | 127/1453/19/21/0 | baseelf:b00 | Rk:9 |
| PL_29 Gudo  | 18c4 | 5c88 | 40 | 8 | 4d | 109/1447/17/19/0 | baseelf:ae0 | Gudo:9 |
| PL_30 Twin  | f44 | 4858 | 50 | 8 | 45 | 58/1206/20/22/0 | libvu0:10,baseelf:700 | Twin:9 |
| PL_31 Vaki  | 2aec | 7b78 | 48 | 8 | 105 | 179/1536/40/42/0 | libvu0:18,baseelf:af8 | Vaki:10 |
| PL_32 Evil  | 27f4 | 6b60 | 50 | 8 | 10d | 151/1538/29/31/0 | libvu0:18,baseelf:ae0 | Evil:11 |
| PL_33 Robo  | 291c | 6d30 | 38 | 8 | c5 | 173/1502/21/23/0 | libvu0:18,baseelf:af8 | Robo:10 |
| PL_34 Dsev  | 27bc | 7210 | 38 | 8 | bd | 162/1497/20/22/0 | libvu0:18,baseelf:ae0 | Dsev:10 |
| PL_35 Idyna | 2694 | 6f48 | 48 | 8 | 116 | 146/1539/26/28/0 | libvu0:20,baseelf:ae8 | Idyna:11 |
| PL_36 Astra | 2964 | 7520 | 50 | 8 | 66 | 176/1459/34/36/0 | libvu0:18,baseelf:ae0 | Astra:10 |
| PL_37 Gata  | 28ac | 4e28 | 20 | cc | 4d | 243/1283/30/30/0 | libvu0:20,baseelf:3d0 | Gata:11 |
| PL_38 Sb    | e24 | 4c00 | 48 | c | 3b | 70/1129/35/35/0 | baseelf:358 | Sb:10 |

Notes: relocation counts per module are 45–1700 (PL modules carry ~1500 R_MIPS_32 each — mostly the
`.data` anim/param tables). Import stub code per PL module: `libvu0` 0x10–0x48 B (2–9 funcs),
`baseelf` 0x28–0xb28 B (5–223 funcs). Exports: 9–12 funcs. MISN modules import only `basemisn`
(0x18–0x80 B stub code) and export `Misn` (9 funcs). Total footprint ≈ 170 KB text + 700 KB data.

---

## Q4. When are modules loaded?

- **All 59 modules are loaded once, at boot.** Startup chain: 0x2323e8 (`jal 0x276298`) runs right
  after the IRX/IOPRP loads (strings `cdrom0:\IOPRP300.IMG`, `SIO2MAN.IRX`… xref'd at 0x232110–0x232224).
  0x276298 registers the game's libraries with liberx; the companion loop at **0x276408** iterates the
  59-entry descriptor table at 0x3d3468 and loads/starts every module through the `modload` stubs
  (0x3723e0 / 0x372420), with retry-on-negative and a **boot-hang loop on failure (0x2764e4)**.
- **Co-residency**: since all modules load at boot, P1 and P2 fighters (two PL_xx modules) and a
  mission module are always simultaneously resident. No dynamic load/unload per fight is done by this
  path — the unload APIs exist in liberx (`sceErxStopUnloadModule` etc.) but the game-side table has
  no unload call sites found.
- **Same module twice?** Not by this design: one descriptor per module, unique export-library names
  ("Tiga"…), and a load counter per descriptor (+0xc incremented at 0x2764bc) suggest refcounted
  reload is *possible* in the API but unused. Two instances would also collide in the global
  library registry.
- Caveat: 0x276408's direct caller was not located (likely `jalr` through a function-pointer task
  table); the pairing with 0x276298 and the boot context is strong but the exact call edge is unverified.

---

## Q5. Proposed PS2Recomp design for ERX support (AOT)

### Recommended approach: (a)+(b) — fixed load address per module, AOT-recompiled, loader hooked

**Why**: all 59 modules are statically known, loaded once, and never moved. The recompiler already
handles everything needed once a module has a *final* address: R_MIPS_26/32/HI16/LO16 are the same
relocs PS2Recomp consumes for the main ELF. Choosing fixed addresses removes the dynamic-relocation
problem entirely.

1. **Fixed address map** (new input in `ps2xRecomp`, e.g. `[[erx]]` entries in `work/ufe3.toml`):
   assign each module a non-overlapping base in a region the main ELF never touches. Main ELF occupies
   0x00200000–0x004e5e38 (.bss end), heap via `AllocSysMemory` grows above ~0x00500000, stacks at the
   top of RAM (0x01fff000 area). Pick e.g. **0x02000000 + i × 0x00100000** (1 MB slots; largest module
   image is PL_17 ≈ 0x11b40 < 1 MB): 59 slots → 0x02000000–0x05B00000, in *unmapped guest space*
   (PS2Recomp's memory is a flat buffer; unmapped simply means "not used by the main image/heap").
   Guest RAM is 32 MB (0x00000000–0x02000000), so instead map modules *inside* RAM but below the
   heap: e.g. **0x00800000 + i × 0x00040000** (256 KB slots; biggest module = 0x11b40 needs 0x120000 —
   so use 0x20000 (128 KB) slots for PL/MISN *or* size-class the slots: MISN ≤ 0x6000, most PL ≤
   0x12000, PL_17 = 0x11b40). Concrete proposal: 59 fixed slots at 0x00600000–0x00FFFFFF with
   0x20000 granularity (59 × 0x20000 = 0x760000, ends 0x00D60000), below the kernel heap region the
   game's `AllocSysMemory` hands out (heap top starts near 0x01000000 upward — verify against
   `AllocSysMemory` usage at runtime before finalizing).
2. **Pre-link each module offline**: new ps2xRecomp input mode that reads the ERX (ELF32/0xFF91),
   applies its `.rel.*` with `sym=0 → +base` at the chosen address, resolves every `.erx.stub`
   import against the *known* export tables (main's `.erx.lib` — those addresses are static in the
   main ELF — and no module imports other modules), and emits one `sub_<addr>_<name>.cpp` per guest
   function plus relocated `.data` initializers. Alternative: reuse `ps2_recomp` on a pre-linked ELF
   produced by a small `erx_prelink.py` (python + struct, no pyelftools needed) — this requires no
   changes to the recompiler's ELF parser at all (recommended first step).
3. **Register functions**: emit a `registerErxModule_<Name>()` per module calling
   `PS2Runtime::registerFunction` for every function in the module image (addresses = chosen base +
   function offsets; function starts discovered the same way control_flow_analyzer does, or simply
   from the `.rel.text` jal-targets + `.erx.stub` stub starts).
4. **Hook the loader (game_overrides.h)**: override the `modload` stubs' callees or the game load
   loop. Cleanest hook points:
   - override the two `modload` stubs **0x3723e0 / 0x372420** (or, more robustly, the guest functions
     the ELOADP driver calls for file read + `AllocSysMemory` at 0x372218) so that instead of reading
     the file and relocating, they: (i) copy the module's *initial data image* (text is not needed at
     runtime, but keeping it makes debugging/interpreter fallback possible) to the module's fixed
     base, (ii) zero .bss/.sbss, (iii) fix the `.erx.lib`/`.erx.stub` pointer words (or pre-bake them),
     (iv) return a fake handle matching what the descriptor/state array expects (0x3d32e8/0x3d3468).
   - keep liberx's **library registration** path running (sceErxStartModule's `.erx.lib` registration
     and the binding of main's 39 character stub ranges 0x35af50–0x35ba38), because those stub
     bindings are what the game's calls actually flow through. In the recompiled binary those stub
     functions are already recompiled C++; their `break 0,1` stub bodies need a recomp-side
     implementation: map each stub (by its `addiu $zero,$zero,N` index) directly to the *pre-linked*
     module function — i.e. in `ps2xRecomp`, recognize the `break 0x0,0x1; addiu $0,$0,N` pattern and
     emit a direct call (or an indirect call through a bindable table).
5. **Data/bss**: emit each module's `.data` as a C array applied to guest memory at the fixed base at
   registration time (or at load-hook time); `.sdata/.sbss` likewise. The gp-relative addressing
   inside modules is relocatable-with-base, so pre-linking handles it (R_MIPS_HI16/LO16 + GPREL if
   present; GPREL not observed).
6. **`.erx.lib`/`.erx.stub`**: since imports are fully known at build time (main's exports are static
   addresses in the recompiled image), *resolve them at AOT time*: module stub code
   (`break;idx`) becomes a direct call to the main ELF's recompiled function (or to the main's own
   stub function at 0x35af50+ to preserve identical semantics). Export tables are only needed for
   the runtime linker if you keep liberx's start/stop path; if you replace that too (see 7), the
   export tables are only needed to answer `sceErxStartModule` bookkeeping.
7. **Unload/reload**: the game never unloads; implement `sceErxStopUnloadModule`-family hooks as
   no-ops (or assert). Two-module co-residency needs nothing special with fixed bases.
8. **Runtime dispatcher**: ensure `lookupFunction` covers the module ranges so that indirect jumps
   (function pointers read from `.erx.lib` export tables or module data — the game likely calls via
   pointers) dispatch correctly.

### Alternatives considered

- **(c) MIPS interpreter fallback for module code**: pros — no per-module recompilation, handles any
  load address (liberx relocates normally, interpreter executes from guest memory). Cons — 59 modules
  × 10 KB code is the *core gameplay code* (fighter logic); interpreting it would be 10–100× slower
  and defeat the purpose. Viable only as a debugging crutch.
- **Runtime recompilation (JIT) of the loaded image**: matches dynamic addresses naturally, but
  PS2Recomp is AOT-only (per-function C++ files) and the toolchain has no in-memory LLVM path;
  massive engineering cost. Reject.
- **Let liberx relocate, then map recompiled code at whatever address resulted**: requires
  position-independent recompiled code or patching immediates at runtime — fragile. Reject; fix the
  address instead (approach a+b).

### Concrete code changes (files/functions)

`tools/PS2Recomp/ps2xRecomp`:
1. New input mode or pre-link step (`erx_prelink.py` or `elf_loader` case for type 0xFF91): parse
   `.eemod/.erx.lib/.erx.stub`, apply relocs with base, emit linked image.
2. `control_flow_analyzer`: function discovery on ERX text (reloc/jal-target driven, since symtab is
   empty); treat `break 0x0,0x1; addiu $0,$0,N` pairs as import stubs (see 3).
3. `code_generator`: emit `registerErxModule_<Name>()` and, for each import stub, a direct call to the
   resolved main-ELF function (target = main `.erx.lib` export #N or main stub 0x35af50+ entry).
4. Config: `[[erx]] name, path, base` in `work/ufe3.toml`.

`tools/PS2Recomp/ps2xRuntime`:
5. `PS2Runtime::registerFunction` calls for module functions (from the generated registration).
6. Guest-memory initialization arrays for module .data/.bss.
7. `game_overrides.h`: overrides for the load path (0x3723e0/0x372420 stubs or the ELOADP driver
   entry at 0x377938 region) to skip file I/O + relocation and instead install the pre-linked image;
   no-op/assert overrides for `sceErxStopUnloadModule` family; optionally keep 0x372850 panic handler
   as a diagnostic.
8. Extend the function lookup ranges to the module slot region so indirect calls land correctly.

### Risks

- **Fixed base collisions** with `AllocSysMemory` heap or any absolute addresses the game computes
  (e.g. 0x4b4598-style .sdata structs are fine, but game buffers are not): must verify real heap
  range at runtime (runtime logs exist in `work/run*.log`).
- **`.eemod` field2 semantics unconfirmed** (probably alloc size); if it feeds an allocator size, use
  the slot size instead.
- **Unverified call edge** for 0x276408 (boot loop) — if modules are in fact loaded lazily somewhere
  else too (e.g. battle start), the hook must cover that path as well; search for `jalr`-based calls
  or trace `0x3d3468` reads at runtime to confirm.
- **Custom relocs 250/251** (1–4 per some modules) semantics unknown — need one targeted
  disassembly look before pre-linking those modules (only 8 modules carry them).
- **Break-stub semantics**: if the game relies on late binding (calls before sceErxStartModule),
  direct AOT calls change behavior; keep a bindable function-pointer table for those stubs instead of
  hard-coded calls if any such path shows up.
- **Two instances / reload**: unused in practice, but the design forbids it (unique export lib names).
