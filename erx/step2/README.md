# ERX module support for PS2Recomp — step 2 (runtime, recompiler, module codegen)

Status: **complete and verified** (see "Verification" below). Working tree:
`tools/PS2Recomp-erx` (branch `erx-support`). Everything here is game-agnostic and
upstreamable; game-specific knowledge lives only in generated data
(`erx/step2/out/erx_bindings.json`) and in the step-3 game override (not written yet).

Budget note: this session is the delegated `opencode run -m xiaomi/mimo-v2.6-pro`
implementation run for the brief; implementation, verification and reporting were
all done here (the leader will review the diff, generated output and this README).

---

## 0. Milestone log

| # | Milestone | How verified |
|---|-----------|--------------|
| 1 | Runtime overflow function registry (`registerOverflowFunction` etc.) + `bindErxImport`/`callErxImport` API | `ps2x_tests` suites "ERX overflow function registry", "ERX import stub dispatch" (6 tests, all pass; full suite 440/440) |
| 2 | Recompiler: generic ERX import stub slot support (`callErxImport` emission + per-slot function splitting) | synthetic-ELF end-to-end run (below); regenerated output inspected |
| 3 | Module codegen: wrap 59 pre-linked images as ELF, recompile, post-process, `erx_register.cpp` | 59/59 modules recompiled, 1891 functions; `ps2EntryRunner` links with all module sources |
| 4 | Runner build wiring (`PS2X_ERX_SOURCE_DIR`) + link/size measurements | `ps2EntryRunner` links (71 s build); size delta measured |
| 5 | This README + `erx_bindings.json` for the step-3 loader | data cross-checked against `work/erx_design.md` and the manifest |

What is left: **step 3 (game override)** — see section 5. Also optional polish listed
in section 6.

---

## 1. Runtime: overflow function registry + ERX import API

Files: `ps2xRuntime/include/ps2_runtime.h`, `ps2xRuntime/src/lib/ps2_runtime.cpp`.

* `generatedFunctionTableSlot` / the dense table fast path are **unchanged**. All new
  lookups happen only after the dense table misses.
* New process-global sparse registry (like the dense table itself):
  * `PS2Runtime::registerOverflowFunction(addr, fn)` (also erases on `fn == nullptr`),
  * `PS2Runtime::lookupOverflowFunction(addr)` / `hasOverflowFunction(addr)`,
  * `PS2Runtime::clearOverflowFunctions()` (tests / re-init).
  Guarded by a `std::shared_mutex`; never taken on the dense fast path.
* `registerFunction`/`replaceFunction` now fall back to the registry for guest PCs
  outside `[g_ps2RecompiledFunctionTableBase, End)` instead of logging an error —
  so module code at `0x01C00000+` (outside the main game's dense table) can be
  registered and dispatched. `hasFunction`/`lookupFunction` consult the registry
  after the dense table (including the "in range but empty slot" case).
* ERX import stub API:
  * `bindErxImport(slotAddress, targetAddress)` / `unbindErxImport` /
    `isErxImportBound` / `clearErxImportBindings`,
  * `bool callErxImport(rdram, ctx, slotAddress)` — looks up the binding and
    synchronously dispatches to `lookupFunction(target)`. The stub slot is a
    **tail-call trampoline**, so if the target returns without moving `ctx->pc`,
    the PC falls back to guest `$ra`. Returns `true` when dispatched.
  * Unbound slots **log once** per slot (`[erx-import] unbound import stub ...`)
    and take the original unlinked trap: `handleBreak()` = `break 0x0,0x1`'s
    breakpoint exception, exactly like the generated code did before.

## 2. Recompiler: ERX import stub slots

Files: `ps2xRecomp/src/lib/ps2_recompiler.cpp` (`collectErxImportStubSlots`),
`ps2xRecomp/src/lib/code_generator.cpp` / `include/ps2recomp/code_generator.h`
(`setErxImportStubSlots`), `ps2xRecomp/src/lib/special_translator.cpp`
(`SPECIAL_BREAK`).

What `ps2xRecomp` generated for these slots before (from `work/output`, read-only):
the slot compiled to `runtime->handleBreak(rdram, ctx)` + nop fall-through, and
slots merged inside larger functions (e.g. `sub_0035B3D8` 0x35b3d8–0x35b428 held 7
slots) had **no table entry of their own**, so `jal`/`jalr` into them failed with
"No exact recompiled function".

Detection is **pattern-based, not `.erx.stub`-driven**. A slot is the exact 8-byte
pair

    0x0000004D   break 0x0,0x1
    0x24000000|N addiu $zero,$zero,N

(scanned over every executable section). Justification for choosing the pattern
over the `.erx.stub` section ranges:

1. `.erx.stub` is a Sony-proprietary SHT_LOPROC section; parsing it duplicates
   knowledge the recompiler otherwise never needs and would have to be redone per
   binary. The byte pattern is what the toolchain emits for *every* unlinked
   liberx import (304 sites in the main ELF, plus every module image before
   pre-linking), so one rule covers main-ELF and module ELFs alike.
2. The pattern is unambiguous in practice (`break 0,1` followed by a write-to-zero
   `addiu` is the stub signature; a genuine `break` trap with such a delay word is
   not something compiler output produces) and needs no section-name assumptions.
3. The slot *address* is the natural dispatch key (`callErxImport(rdram, ctx,
   slotAddress)`), matching the brief's API; the index `N` is carried by the
   binding table built offline (section 5) instead of being re-derived in C++.

Behavior per detected slot at `A`:

* every containing function is **split at `A`** (and uncovered slots get their own
  8-byte function), so each slot is an individually dispatchable entry, exactly
  like hardware (`jal`/`jalr` into any slot works — the dense table maps every
  slot address to its own generated function);
* the `break` at `A` compiles to
  `runtime->callErxImport(rdram, ctx, 0x<A>u); return;` (tail dispatch; the
  trailing `addiu $zero,$zero,N` still compiles to a comment-nop).

Note this also covers the main ELF's own kernel import stubs (sysmem/loadcore/
modload slots at 0x372200..0x372480) — they become per-slot functions too, which
makes `replaceFunction(0x3723E0, ...)` etc. precise (see section 5).

### End-to-end codegen check (synthetic ELF)

A 24-byte image with two stub slots + `jr $ra`, wrapped with the step-2 wrapper and
run through `build/ps2xRecomp/ps2_recomp` (artifacts: `erx/step2/{wrap,cfg,gen}/TEST*`):

* both slots split into `sub_01F00000` / `sub_01F00008` and registered in the dense
  table at their exact addresses (`register_functions.cpp` lines for 0x1f00000 and
  0x1f00008),
* each emits `runtime->callErxImport(rdram, ctx, 0x…u); return;`.

## 3. Module code generation (59 pre-linked modules)

Scripts (stdlib-only python3): `erx/step2/erx_wrap.py`, `erx/step2/erx_post.py`,
`erx/step2/erx_bindings.py`. Reproduce with:

```
python3 erx/step2/erx_wrap.py                                   # 59 wrapper ELFs + tomls
for m in erx/step2/cfg/*.toml; do
  tools/PS2Recomp-erx/build/ps2xRecomp/ps2_recomp "$m"          # -> erx/step2/gen/<MOD>/
done                                                            # (0.4 s total)
python3 erx/step2/erx_post.py                                   # -> erx/step2/out/
```

* **Wrap** (`erx_wrap.py`): each `erx/out/<MOD>.bin` becomes a minimal ELF32
  `ET_EXEC`, `EM_MIPS`, `e_flags 0x20920001`, one PT_LOAD at the module base, one
  section per manifest `section_layout` entry (`.text` SHF_EXECINSTR,
  `.sbss/.bss` SHT_NOBITS), and a `.symtab` with one STT_FUNC symbol per function
  start. Starts = manifest `function_starts` (module-internal `jal` targets +
  export pointers) **union** internal `j` targets re-scanned from the pre-linked
  `.text` (tail-called internal functions are not `jal` targets and would otherwise
  fall inside another function's range). Symbol size = next start − start, which
  makes the parser treat the ranges as authoritative.
* **Recompile**: standard `ps2_recomp` per module into `erx/step2/gen/<MOD>/`
  (module code is small: 1891 functions / 59 modules).
* **Post** (`erx_post.py`): keeps every `sub_*.cpp`/`entry_*.cpp`, **drops** each
  module's `register_functions.cpp` (its dense table would clash with the main
  game's `g_ps2RecompiledFunctionTable*` symbols), rewrites
  `#include <ps2_recompiled_functions.h|stubs.h>` → `"erx_recompiled_functions.h"|
  "erx_recompiled_stubs.h"` so module code can compile next to the main game's
  generated code without include clashes, and emits:
  * `erx_recompiled_functions.h` — all module declarations (names
    `sub_<ADDR8>_0x<addr>`, unique across modules and disjoint from the main
    game's addresses),
  * `erx_register.cpp` — one static initializer registering every module function
    through `PS2Runtime::registerOverflowFunction` (section 1's overflow registry).

Cross-module call semantics come out of the existing emitter, no special casing:

* module → module direct `j`: emitted as a direct C++ tail call
  (`sub_…(rdram, ctx, runtime); return;`),
* module → module `jal`: `dispatchGuestBranch(DirectCall)` → `lookupFunction` →
  overflow registry,
* module → main `j <main-ELF fn>` (the pre-linked import replacement):
  `dispatchGuestBranch(DirectJump)` → returns with `ctx->pc = target` and the
  call chain unwinds to the EE scheduler, which dispatches the main function via
  the dense table (guest `$ra` preserved, so the main function returns straight
  back into the module caller),
* main → module: game code `jal`s a main-ELF stub slot → `callErxImport` →
  `lookupFunction(module export)` → overflow registry.

## 4. Runner build wiring, build time, binary size

`ps2xRuntime/CMakeLists.txt`: new cache path `PS2X_ERX_SOURCE_DIR` (default
`ps2xRuntime/src/runner/erx`); when `erx_register.cpp` exists there, every `*.cpp`
in it is added to `ps2EntryRunner` (CONFIGURE_DEPENDS glob). Absent directory =
feature disabled, no cost. Copy `erx/step2/out/*` to `ps2xRuntime/src/runner/erx/`
to enable (that copy is what the step-3 change in `rebuild.sh` should do).

Measured on this machine (32 threads, Release, clang, `-march=x86-64-v3`,
unity build 32/TU, configured exactly as the brief specifies):

* **ps2EntryRunner with modules links**: full build (main game's 8232 generated
  files + 1892 ERX sources + runtime, 317 unity TUs) **71 s wall / 1378 s CPU**.
* **Binary size**: 60,181,176 B (57.4 MiB) without modules →
  **69,896,712 B (66.7 MiB) with the 59 modules: +9,715,532 B (+16.1 %)**.
* Adding the module sources to an already-built game-only tree costs one unity TU
  + relink (~42 s here); the ERX tooling itself is negligible (wrap <1 s, all 59
  modules recompiled by `ps2_recomp` in **0.4 s**, post-process <1 s).
* `ps2x_tests`: 440/440 pass (6 new ERX tests included).

## 5. Step 3 — what the game override for loading must still do (NOT implemented here)

The recompiled module code is compiled in, but nothing copies the module **data**
into guest RAM and nothing binds the main-side stubs yet. A game override
(`game/ufe3_overrides.cpp` style, via `runtime.replaceFunction`) must provide that
at boot. Data source for all of it: `erx/out/manifest.json`,
`erx/out/<MOD>.bin`, and the generated `erx/step2/out/erx_bindings.json`.

### 5.1 Main-ELF stubs to replace

The boot-time load loop (`0x276408..0x276504`, descriptors `0x3d3468`, handle array
`0x3d32e8`, out-struct `0x4b4598` — see `work/erx_design.md` §Q2) calls two modload
import stubs per module:

* **`replaceFunction(0x3723E0, loadOverride)`** — modload#8 "load":
  `(a0=pathPtr, a1=flags|0x100, a2=0)`, must return a non-negative handle
  (the loop retries on negative and stores it into `0x3d32e8`). Implementation:
  read the path string from guest memory, map it to the module name
  (`cdrom0:\ERX\PLAYER00\PL_00.ERX;1` → `PL_00`), then **copy what to where**:
  `memcpy(rdram + manifest[PL_00].base, erx/out/PL_00.bin, manifest[PL_00].size)`
  (the whole pre-linked image at its fixed base — `.text` bytes are not executed
  but `.data/.rodata/.sdata` **must** be there because the recompiled code reads
  and writes those guest addresses; `.sbss/.bss` are already zero in the `.bin`).
  Total 2,017,900 B at `0x01C00000..0x01DECA6C`. Return e.g. `index+1` as handle.
  *Caution*: make sure the runtime's guest heap (`configureGuestHeap`) never hands
  out `0x01C00000..0x01F00000`, or allocations will collide with module data.

* **`replaceFunction(0x372420, startOverride)`** — modload#16 "start":
  `(a0=state, a1=path, a2=0, a3=outStruct 0x4b4598)`, called once per module after
  load (the game increments `descriptor+0xc` itself). Implementation, in order:

  1. **bindErxImport calls** (the `erx_bindings.json` `bindings` list filtered to
     this module; each entry = `{library, slot, index, module, target}`):
     `PS2Runtime::bindErxImport(slot, target)` for every entry. The rule behind the
     table (recomputed by `erx/step2/erx_bindings.py`): each main-side `.erx.stub`
     entry's 8-byte slot (`break 0,1 ; addiu $zero,$zero,N`, e.g. library `Tiga`
     code range `0x35B408..0x35B448`, `Misn` `0x35BA18..0x35BA38`) binds to export
     word **N** of the module exporting that library (word list counted the same
     way liberx does: the `.erx.lib` words after the version field, i.e. the
     manifest `exports[].funcs` array, **including** the leading start/stop
     quadruple as words 0..3 — which is exactly how `erx_prelink.py` already
     resolved module→main imports). 288 bindings for 39 character libraries.
  2. **Special case `Misn`**: all 20 MISN modules export library `Misn` (9 words
     each). The 4 `Misn` slots at `0x35BA18..0x35BA38` must be **re-bound on every
     mission-module start** to the currently started module's words (and optionally
     `unbindErxImport` on stop). `erx_bindings.json` lists all 20 candidates per
     slot for exactly this purpose.
  3. Kernel libraries (`sysmem`, `loadcore`, `modload`) are **not** ERX bindings —
     those slots keep their existing runtime stub handlers / `replaceFunction`
     overrides.
  4. **Module start functions**: each module's export word list begins with a
     quadruple `funcs[0..3]` that is identical in all 59 modules (verified over the
     manifest) — the liberx entry pointers (`start`/`stop`/…, `eRxLiB` entry at
     `module .erx.lib`+0x20). `sceErxStartModule` semantics = call word 0 (start)
     after registering/binding; words 1..3 are the stop/unload companions used by
     `sceErxStopModule`/unload (not needed while everything stays resident).
     The exact start-function prototype is the one piece still needing a RE pass
     (arguments visible at the call inside `sceErxStartModule` ~0x36f100 in
     `work/re/main.s`); if the game works without it, binding alone (step 1–3) is
     sufficient for calls through the character stubs.

Until `bindErxImport` runs, calls into character stubs hit the unlinked trap
(`callErxImport` → `handleBreak`, logged once per slot as
`[erx-import] unbound import stub at PC 0x…`) — the recompiled equivalent of the
game's "PANIC!! Unlinked function call".

### 5.2 What does NOT need doing

* No copying of code is needed for *execution* (module code is AOT-compiled and
  statically registered by `erx_register.cpp` at startup).
* Module→main imports need no binding: `erx_prelink.py` already rewrote each
  module stub to `j <main-ELF target>` and the recompiled form dispatches through
  the dense table.

## 6. Optional polish (not required)

* `jal` to module-internal functions currently routes through
  `dispatchGuestBranch(DirectCall)`; a direct-call fast path (like
  `emitDirectFunctionJumpIfAvailable` but for calls) would remove the map lookup.
* The dense table could get a configurable second dense range for module code;
  the hash-map overflow is correct and simple first.
* `erx_bindings.py` could emit ready-made C++ instead of JSON once the step-3
  override's shape is decided.
