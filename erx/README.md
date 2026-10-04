# ERX pre-linker for UFE3 (SLPS-25441) — step 1 of ERX support

Offline tool: `erx/erx_prelink.py` (python3 stdlib only; capstone is used only for the
disassembly cross-check and is optional). It turns all **59** `disc/ERX/*/*.ERX` modules
into fully linked fixed-address images (`erx/out/<MODULE>.bin`) plus `erx/out/manifest.json`.

```
python3 erx/erx_prelink.py [--base 0x01C00000] [--end 0x01F00000] [--root <repo>] [--out <dir>]
```

Status: **all 59 modules linked and verified** (relocations applied, imports resolved to
main-ELF functions and stubs replaced, control-flow self-check clean). Nothing outside
`erx/` was created or modified.

## 1. Address layout

* Reserved guest-RAM region: **0x01C00000 .. 0x01F00000** (3.00 MB), overridable with `--base`.
* Modules are packed in stable order **PLAYER00 (PL_00..PL_19), PLAYER20 (PL_20..PL_38),
  MISN (MISN_1..MISN_20)**, each numerically sorted; each module start is 0x100-aligned.
* Each module keeps **its own contiguous ERX section layout** (`.text`, `.eh_frame`,
  `.erx.lib`, `.erx.stub`, `.rodata`, `.data`, `.sdata`, `.sbss`, `.bss`) with the file's
  own `sh_addr` offsets, rebased by the module base. Rationale: the `.eemod` header
  references `.erx.lib`/`.erx.stub` by vaddr, and all `.rel.*` `r_offset`s are *image
  (module-vaddr) relative*, so keeping the original inter-section offsets is exactly what
  the reloc stream was generated for. All `sh_addralign` requirements hold (max 32, and
  base is 0x100-aligned). `.sbss/.bss` are zero-filled; PROGBITS sections are copied from
  file.
* **Total footprint 0x1ECA6C (2,017,900 B)** — fits; ends at 0x01DECA6C, 0x11354 B below
  the 0x01F00000 limit. The tool exits non-zero if it ever overflows.

## 2. Relocations ("load base" semantics, sym index 0 = image base)

Every `.rel.*` section is applied in **stream (file) order**. r_offset is the module vaddr
of the field (verified: `.rel.data` offsets fall inside `.data`'s vaddr range, etc.).

* **R_MIPS_32 (2)**: `word = base + word` (59,873). Also covers `.eh_frame` (incl. a few
  unaligned sites), `.erx.lib`/`.erx.stub` name/func pointers.
* **R_MIPS_26 (4)**: `jal/j` target `t = base + t` (7,447). Every pre-reloc target is
  checked to be inside the module image.
* **R_MIPS_HI16/LO16 (5/6)** (1,449 / 1,540): the rel stream is **grouped per symbol and is
  NOT address-sorted**. Pairing rule (validated on every module): each LO16 pairs with the
  *nearest preceding* HI16 (or 250) entry in stream order — one `lui` may feed **several**
  16-bit immediates (`addiu` *and* `lw/sw` displacements: e.g. PL_00 `lui $t7,1` @0x1f50
  feeding `addiu` @0x1f58 and `lw` @0x1f5c). Addend `A = (imm_hi<<16) + sext(imm_lo)`;
  patch: `lo' = (base+A) & 0xffff`, `hi' = ((base+A) - sext(lo')) >> 16` (exact for
  `lui+addiu`; `+0x8000` carry equivalent), `ori`-based pairs get the unsigned variant.
  Every pair value is checked to land inside the module image.

### Custom types 250/251 — determined and implemented

28 sites (14 + 14) in 8 modules: MISN_6, PL_00, PL_01, PL_08 (×2), PL_09, PL_16, PL_17 (×2),
PL_20 (×2), PL_22, PL_26 (×2). All in `.rel.text`, all `sym=0`. The reloc stream always
contains the **triple** `[250 @ lui][251 @ data-word][6 @ lo-immediate]`:

* **250 marks the `lui` of a HI/LO pair**, **251 marks the pair's target word** (its
  address is the symbol; the word itself is never modified — it is a `.sdata`/`.data`
  word or an import-stub `break` slot), **6 is a normal LO16** completing the pair.
* **Semantics implemented: the pair forms `(module_base + r_offset(251))`**, with the
  same hi/lo patching as a normal pair. Evidence:
  1. **PL_17 proves the target identity**: the float at module vaddr 0x151c0 (15.0f) is
     loaded from two code paths. The normal path uses `lui $t7,1` / `addiu $t7,$t7,0x51c0`
     (HI16/LO16 at 0x2220/0x2224) = `base+0x151c0`. The other path is the triple:
     `250 @ lui $t7,0xffda` (0x2214) + `251 @ 0x151c0` + `6 @ addiu $t7,$t7,0x51c0` (0x21b8),
     and the very next insn is `lwc1 $f15,($t7)` — a raw dereference. Both paths must form
     the same address, and only `base+0x151c0` exists in the address space.
  2. **PL_00**: assert-style call `helper(a0, "file", 0x2b3)` reached with `a0 = 0xc8` via a
     plain `lui 0`/`addiu 0xc8` pair *and* with `a0 = 0xffe800c8` via a triple whose 251
     target is exactly 0xc8 (the baseelf import-stub slot). Same conclusion.
  3. **MISN_6**: `lui $a0,0xfffd` / `addiu $a0,$a0,0xb10` passes `0xfffd0b10`; its triple's
     251 target is 0xb10 (.data word).
* The stored hi16 is **biased**: `stored_pair = target − delta` with `delta` a per-site
  multiple of 0x10000 (0x30000 .. 0x3C0000 here; e.g. PL_17 has 0x270000 and 0x3C0000,
  PL_20 uses 0x140000 twice). Since `delta ≡ 0 (mod 0x10000)` the lo16 is unaffected, and
  the 251 entry is what disambiguates the intended target. Why the ERX toolchain stored a
  biased hi16 is an open question (probably an artifact of the original link origin); it
  does not affect the linked result.

All 8 modules carrying 250/251 pass every self-check, so they are marked `verified`
(the rule "implement nothing if not understood" was not needed).

## 3. Imports (`.erx.stub`) — resolved against the main ELF

Per stub entry (library name + version + 8-byte slots `break 0x0,0x1 ; addiu $zero,$zero,N`),
each slot's N is resolved to the N-th function of that library in the **main ELF's `.erx.lib`**
(`disc/SLPS_254.41`, vaddr 0x483600 = file 0x284600; 34 libraries incl. `baseelf`, `libvu0`,
`basemisn`). Manifest records per slot: module slot address, library, index, resolved target.

* All indices are in range of the target library; **every resolved target is a function
  start in the main ELF** (present in the main `.text` `jal`-target set ∪ main `.erx.lib`
  export pointers; 3,871 main-ELF jal targets scanned). Zero misses.
* In the linked image every 8-byte stub is **replaced by `j <target> ; nop`** — safe because
  stubs are reached by `jal` and the target's own `jr $ra` returns to the caller. Verified:
  no `break 0,1` pattern remains in any image (raw scan + disassembly scan).
* Trailing zero padding inside stub-code ranges (8 B, `idx=0` lookalikes) is correctly
  skipped, not treated as an import.

## 4. Exports (`.erx.lib`)

Each exported library (name, version) and its function-pointer list is recorded in the
manifest with **absolute addresses inside the linked module image** (after relocation):
one library per module (`Man Sev Jack … Sb`, 9–12 funcs; `Misn` 9 funcs).

## 5. Outputs and self-checks

`erx/out/<MODULE>.bin` + single `erx/out/manifest.json` (per module: name, disc path, base,
size, section layout, entry point — `.eemod[0]`, all 59 are 0xFFFFFFFF "none", so entry=null
— function starts = all `jal` targets inside the image + export pointers + entry, imports,
exports, relocation counts by type, external-target call counts, `verified` flag).

Self-checks (all in `erx_prelink.py`, all clean):
1. Raw word scan of each `.text`: every `j/jal` target is inside the same module or a
   resolved main-ELF function.
2. Capstone disassembly cross-check of every image (walks over undecodable R5900 words):
   every decoded `j/jal` passes the same target test and no `break 0,1` remains. The only
   `break` left anywhere is compiler-generated `break 0,7` after `div` (divide-by-zero
   trap, e.g. PL_20 @0x1d0e944) — legit code. (`llvm-objdump` on this system lacks binary
   input mode, hence capstone.)
3. Relocation sanity: every R_MIPS_26 target and every HI/LO pair value is inside the image.
4. Import sanity: every index resolves; every target is a main-ELF function start.

### Summary table (59/59 OK)

| module | base | size | funcs | imports | exports | status |
|---|---|---|---|---|---|---|
| PL_00 | 0x1c00000 | 0xf678 | 28 | 353 | 10 | OK |
| PL_01 | 0x1c0f700 | 0xf9a8 | 27 | 349 | 10 | OK |
| PL_02 | 0x1c1f100 | 0x10518 | 32 | 358 | 10 | OK |
| PL_03 | 0x1c2f700 | 0x12c74 | 31 | 351 | 10 | OK |
| PL_04 | 0x1c42400 | 0xd438 | 32 | 355 | 10 | OK |
| PL_05 | 0x1c4f900 | 0xfbdc | 47 | 362 | 10 | OK |
| PL_06 | 0x1c5f500 | 0xa820 | 26 | 348 | 10 | OK |
| PL_07 | 0x1c69e00 | 0xbab0 | 31 | 352 | 10 | OK |
| PL_08 | 0x1c75900 | 0xb530 | 30 | 346 | 10 | OK |
| PL_09 | 0x1c80f00 | 0xc000 | 30 | 349 | 10 | OK |
| PL_10 | 0x1c8cf00 | 0xbb08 | 26 | 351 | 10 | OK |
| PL_11 | 0x1c98b00 | 0x9bb8 | 30 | 351 | 10 | OK |
| PL_12 | 0x1ca2700 | 0xa920 | 30 | 353 | 10 | OK |
| PL_13 | 0x1cad100 | 0x95b8 | 23 | 348 | 9 | OK |
| PL_14 | 0x1cb6700 | 0xbce0 | 26 | 350 | 10 | OK |
| PL_15 | 0x1cc2400 | 0xb000 | 26 | 351 | 9 | OK |
| PL_16 | 0x1ccd400 | 0xcda8 | 26 | 348 | 10 | OK |
| PL_17 | 0x1cda200 | 0x15248 | 35 | 354 | 11 | OK |
| PL_18 | 0x1cef500 | 0xdc38 | 34 | 354 | 11 | OK |
| PL_19 | 0x1cfd200 | 0xeab4 | 41 | 359 | 12 | OK |
| PL_20 | 0x1d0bd00 | 0xd2e0 | 36 | 353 | 10 | OK |
| PL_21 | 0x1d19000 | 0xdb40 | 42 | 360 | 11 | OK |
| PL_22 | 0x1d26c00 | 0xc810 | 33 | 353 | 11 | OK |
| PL_23 | 0x1d33500 | 0x8618 | 25 | 349 | 10 | OK |
| PL_24 | 0x1d3bc00 | 0xa088 | 27 | 349 | 10 | OK |
| PL_25 | 0x1d45d00 | 0xb170 | 23 | 350 | 9 | OK |
| PL_26 | 0x1d50f00 | 0xbd40 | 27 | 349 | 10 | OK |
| PL_27 | 0x1d5cd00 | 0x99e0 | 21 | 340 | 9 | OK |
| PL_28 | 0x1d66700 | 0x98b8 | 26 | 351 | 9 | OK |
| PL_29 | 0x1d70000 | 0x7b00 | 21 | 347 | 9 | OK |
| PL_30 | 0x1d77b00 | 0x5b18 | 24 | 224 | 9 | OK |
| PL_31 | 0x1d7d700 | 0xb138 | 29 | 352 | 10 | OK |
| PL_32 | 0x1d88900 | 0x9e48 | 26 | 349 | 11 | OK |
| PL_33 | 0x1d92800 | 0xa0e0 | 26 | 352 | 10 | OK |
| PL_34 | 0x1d9c900 | 0xa430 | 24 | 349 | 10 | OK |
| PL_35 | 0x1da6e00 | 0xa098 | 27 | 351 | 11 | OK |
| PL_36 | 0x1db0f00 | 0xa908 | 26 | 349 | 10 | OK |
| PL_37 | 0x1dbb900 | 0x88d0 | 44 | 124 | 11 | OK |
| PL_38 | 0x1dc4200 | 0x6094 | 29 | 106 | 10 | OK |
| MISN_1 | 0x1dca300 | 0x1784 | 12 | 9 | 9 | OK |
| MISN_2 | 0x1dcbb00 | 0x1c98 | 21 | 15 | 9 | OK |
| MISN_3 | 0x1dcd800 | 0x1b04 | 17 | 12 | 9 | OK |
| MISN_4 | 0x1dcf400 | 0x15b0 | 16 | 11 | 9 | OK |
| MISN_5 | 0x1dd0a00 | 0x3300 | 12 | 9 | 9 | OK |
| MISN_6 | 0x1dd3d00 | 0x1c2c | 14 | 10 | 9 | OK |
| MISN_7 | 0x1dd5a00 | 0x11e0 | 12 | 9 | 9 | OK |
| MISN_8 | 0x1dd6c00 | 0x1630 | 19 | 13 | 9 | OK |
| MISN_9 | 0x1dd8300 | 0x1284 | 17 | 14 | 9 | OK |
| MISN_10 | 0x1dd9600 | 0x5870 | 15 | 12 | 9 | OK |
| MISN_11 | 0x1ddef00 | 0x10e4 | 13 | 10 | 9 | OK |
| MISN_12 | 0x1de0000 | 0x614 | 6 | 4 | 9 | OK |
| MISN_13 | 0x1de0700 | 0x1140 | 13 | 9 | 9 | OK |
| MISN_14 | 0x1de1900 | 0x1dd4 | 18 | 14 | 9 | OK |
| MISN_15 | 0x1de3700 | 0x16ac | 18 | 11 | 9 | OK |
| MISN_16 | 0x1de4e00 | 0x1898 | 15 | 13 | 9 | OK |
| MISN_17 | 0x1de6700 | 0x618 | 4 | 2 | 9 | OK |
| MISN_18 | 0x1de6e00 | 0x3120 | 13 | 10 | 9 | OK |
| MISN_19 | 0x1dea000 | 0xeb0 | 14 | 11 | 9 | OK |
| MISN_20 | 0x1deaf00 | 0x1b6c | 14 | 9 | 9 | OK |

Region 0x1c00000..0x1deca6c, total 0x1eca6c. Relocation totals: `{2: 59873, 4: 7447,
5: 1449, 6: 1540, 250: 14, 251: 14}`.

## 6. Q7: the game's load loop and stub binding (evidence from `work/re/main.s`)

### 6a. The two `modload` calls (0x3723e0, 0x372420) — step-2 hook points

Both are 8-byte `break 0,1; addiu $zero,$zero,N` import stubs in the main ELF's own
`.erx.stub` `modload` range (0x3723c8–0x372480): **0x3723e0 = modload export #0x8**,
**0x372420 = modload export #0x10** (index read from the `addiu`; indices skip around —
#3,4,6,8,9,0xa..0xf,0x10,0x11,0x12,...).

The load loop at **0x276408–0x276504** walks the 59 descriptors (it is two-way unrolled;
stride constants 0x14 and 0xc appear literally at 0x276474/0x276478, and `mult`-based
indexing suggests the true entry receives indices in `$12/$13`/`$4/$5`; exact caller
still unverified as in `work/erx_design.md`):

* **Call 1 — `0x3723e0(path, flags, 0)`** (0x27644c): `$4 = [desc+0]` (path string),
  `$5 = [desc+4] | 0x100` (flags; all-zero in the file, so 0x100 is always set — likely
  "load module" mode for ELOADP), `$6 = 0`. Return **≥ 0 = module handle**; stored at
  **state-array slot `+0`** (`0x3d32e8 + i*12`; `sw $2, 0x0($17)` at 0x276458). On negative
  return the code increments **descriptor `+0x10`** (retry counter, 0x27645c–0x276464) and
  retries (`bltzl` back to 0x276444) — an unbounded retry loop.
* **Call 2 — `0x372420(handle, path, 0, struct)`** (0x2764c0): `$4 = [state+0]` (handle from
  call 1), `$5 = [desc+0]` (path again), `$6 = 0`, `$7 = 0x4b4598` (a static struct in
  `.sdata`/scratch — result/status block, contents not yet decoded). Return ≥ 0 on success,
  stored at **state-array slot `+4`** (`sw $2, 0x4($16)` at 0x2764cc). On negative return the
  loop falls into **`b 0x2764e4` — infinite loop (boot hang)**, matching the design study.
* **Descriptor table 0x3d3468** (59 × 0x14): `+0` = path ptr, `+4` = flags (OR'd with 0x100
  at call time, file value 0), `+0xc` = load counter (incremented at 0x2764a4–0x2764bc per
  call-2 attempt), `+0x10` = retry counter. **State array 0x3d32e8** (59 × 0xc): `+0` = load
  handle (call-1 return), `+4` = start result (call-2 return), `+8` = touched as a third
  field (`$13 = 0x3d32e8+8` at 0x276494) — semantics not yet pinned down.

So a step-2 hook can replace exactly two stubs: 0x3723e0 → "install pre-linked image for
`path`, return fake handle" and 0x372420 → "bind exports, return ≥ 0", writing the same
two state words the game expects.

### 6b. How main's character stubs (0x35af50..0x35ba38) get bound

They are **the same 8-byte `break 0,1; addiu $zero,$zero,N` slots** as the modules' import
stubs (verified at 0x35af50: `break/idx 4`, `break/idx 5`, …; each character/misn library
has its own range, e.g. "Tiga" 0x35b408–0x35b448, "Misn" 0x35ba18–0x35ba38, reached directly
by `jal` from game code — e.g. `jal 0x35b000` at 0x26d064, `jal 0x35ba18` at 0x2959a0).

Binding path (evidence from `sceErxStartModule` region 0x36f100–0x36f520):

1. The **module's `.erx.lib` entry** (the relocated table in module memory: name/version/
   N function pointers + a start/stop pair at entry `+0x1c/+0x20`) is registered with the
   kernel library registry via `loadcore` stub **0x372348 (export #0x2b, RegisterLibraryEntries
   -style)** at 0x36f128; the return code's low 16 bits are compared with **0x9036 / 0x9039**
   (SCE "library already exists / not found" family) at 0x36f134/0x36f1a0. On 0x9036 the
   routine at 0x36f13c–0x36f170 releases/adjusts with `loadcore` stubs 0x372358/0x372370.
2. `sceErxStartModule` then **calls the module's start function** through the `.erx.lib`
   header pointer: `lw $5, 0x1c($19)` / `beq $5, -1 → skip` / `jalr $2` (0x36f330–0x36f354,
   again at 0x36f3ec–0x36f414) — `$19` = the module's `.erx.lib` descriptor in module memory.
3. The main's character stubs resolve through **the registered library**: the `break 0,1`
   traps to the unlinked-call handler (0x372850 region, panic string "PANIC!! Unlinked
   function call was occured." xref 0x372c14). The handler derives the call from **EPC**
   (the stub slot address), locates the enclosing `.erx.stub` descriptor (main's own
   `.erx.stub` section at vaddr 0x4794e0: name, version, `stub_code_start/end`), reads the
   index N from the `addiu` at EPC+4, and takes the N-th function pointer **from the
   registered library's `.erx.lib` function array — i.e. from relocated module memory**.
   Thus the memory read at bind/start time is (a) main's `.erx.stub` descriptors (0x4794e0),
   (b) the module's relocated `.erx.lib` (function pointer array), (c) the EPC/instruction
   pair for the slot. Whether the handler *patches* the slot (like our `j <target>`) or
   caches a pointer is not yet pinned down (open question for step 2), but the resolution
   data source is the module's `.erx.lib`.

## 7. Open questions / next steps (step 2: runtime loader hook)

* Exact caller of the 0x276408 loop and meaning of state slot `+8` / struct 0x4b4598.
* Whether the break handler patches stub slots in place or calls through a table (decide
  which to emulate in `game_overrides.h`).
* `.eemod` field2 (0x17638-style per-module) still not confirmed as allocation size; not
  needed with fixed bases.
* The 250/251 hi16 bias origin (per-site delta 0x30000–0x3C0000) remains unexplained —
  result semantics proven, so this is cosmetic.
* Runtime loader hook design (step 2): override 0x3723e0 → copy `erx/out/<MOD>.bin` data
  image (text optional) to `<base>`, zero bss (already zero in the .bin), return fake
  handle; override 0x372420 → register the module's function starts + bind main's
  character stub slots to the manifest's export pointers, return 0.

## Progress log

* Relocation model (2/4/5/6, image-relative offsets, symbol-grouped stream, one-lui-to-
  many-immediates pairing) established and applied to all 59 modules — verified.
* Custom relocs 250/251 decoded from disassembly evidence and implemented — verified.
* Import resolution vs main `.erx.lib` + stub→`j` replacement — verified (0 misses).
* Exports recorded with post-reloc absolute pointers — verified.
* Self-checks (raw + capstone disassembly) clean on 59/59; manifest written.
* Q7 partially answered from `work/re/main.s` (6a fully, 6b source-of-truth established;
  patch-vs-indirect still open).
