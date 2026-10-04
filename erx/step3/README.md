# ERX module support for UFE3 — step 3 (runtime install & bind)

This README is the running report for step 3. Sections are appended after every
milestone; earlier sections are never rewritten. Spec for this step:
`erx/step2/README.md` section 5. Context: `erx/README.md` (step 1), `erx/step2/README.md`
(step 2), `work/erx_design.md` (RE study).

Session constraints: edits limited to `game/ufe3_overrides.cpp`, `rebuild.sh`,
`erx/step2/*.py`, `erx/step3/*` (nothing in `tools/PS2Recomp` was needed in the end —
see milestone 2); build via `nice -n 19 ./rebuild.sh`, run only via `./run_headless.sh
<seconds>`; no installs, no git operations.

---

## 0. Milestone log

| # | Milestone | How verified |
|---|-----------|--------------|
| 1 | Build integration (`rebuild.sh` regenerates module sources + table and wires them into `ps2EntryRunner`) | rebuild from existing tree; regeneration + copy + configure re-run observed (below) |
| 2 | Guest heap capped below the module region via existing `configureGuestHeap` | `PS2X_TRACE_HEAP=1` boot trace scanned: no allocation in 0x01C00000..0x01F00000 (below) |
| 3 | Loader hooks (0x3723E0 load / 0x372420 start + module start function) | 90 s headless boot trace (below) |
| 4 | `PS2X_TRACE_ERX=1` instrumentation | log lines quoted (below) |
| 5 | 90 s headless verification (+ optional pad-script run) | `erx/step3/` logs + shots (below) |

## 1. Milestone 1 — build integration

### What changed

* **`erx/step3/erx_tables.py` (new)** — build-time generator that turns
  `erx/out/manifest.json` + `erx/step2/out/erx_bindings.json` into a small C++
  table (`erx/step3/out/erx_tables.{h,cpp}`): per module the name, the disc path as
  the game passes it, fixed base/size, the `.erx.lib` export-entry address, and the
  list of main-side stub-slot bindings (`slot -> target`), the `Misn` slots included
  per MISN module (3 slots: 0x35BA18/0x35BA20/0x35BA28 — the 0x35BA30..0x35BA38 tail
  of the `0x35BA18..0x35BA38` stub-code range is zero padding, verified against the
  ELF: `break 0,1; addiu idx` pairs exist at 0x35BA18/20/28 only).
  **Why a build-time table instead of runtime JSON parsing**: the only thing the
  override must read at runtime is the raw image `erx/out/<MOD>.bin` (2 MB of data
  that must stay a data file); everything else is static metadata, so compiling it
  in avoids a JSON parser and path discovery for metadata in `game/ufe3_overrides.cpp`.
  The `.bin` files are still read from `erx/out/` at load time, located by walking up
  from the runner's working directory (`disc/`) for `erx/out/manifest.json`, or
  `PS2X_ERX_DIR` if set.
* **`rebuild.sh`** — after the existing game-code regeneration it now:
  1. runs `erx/erx_prelink.py` if `erx/out/manifest.json` is missing (step-1 products
     are part of the repo; this is the fallback for a checkout without them),
  2. regenerates `erx/step2/out/` when it is missing or older than any of
     `erx/step2/*.py`, `erx/out/manifest.json`, or the `ps2_recomp` binary
     (`erx_wrap.py` -> `ps2_recomp` per `erx/step2/cfg/<MOD>.toml` -> `erx_post.py`,
     against the **main build's** `tools/PS2Recomp/build/ps2xRecomp/ps2_recomp`),
     otherwise reuses it,
  3. always regenerates `erx/step3/out/erx_tables.{h,cpp}` (sub-second),
  4. copies `erx/step2/out/*` to `tools/PS2Recomp/ps2xRuntime/src/runner/erx/`
     (picked up by `PS2X_ERX_SOURCE_DIR`, see step-2 README section 4),
     `erx_tables.cpp` to `src/runner/` and `erx_tables.h` to `ps2xRuntime/include/`,
  5. re-runs `cmake -S tools/PS2Recomp -B tools/PS2Recomp/build` so the
     `if(EXISTS erx_register.cpp)` ERX block in `ps2xRuntime/CMakeLists.txt` activates
     when the module sources first appear (the check is configure-time; the cache
     keeps all flags from `setup.sh`),
  6. then builds `ps2EntryRunner` as before.
  The generated-file cleanup `find` now skips `src/runner/erx/` so it cannot delete
  the module sources it does not own.

### Clean-checkout order (documented as required)

```
./setup.sh        # clone/patch/build tools/PS2Recomp (patches/ps2recomp.patch carries all step-2 changes; verified in sync with the working tree)
./rebuild.sh      # game code + ERX module sources + erx_tables + ps2EntryRunner
```

Notes: `disc/` (extracted disc incl. `disc/ERX/*/*.ERX`) must be in place before
`rebuild.sh` (needed by `work/ufe3.toml` and, if `erx/out/` is absent, by
`erx_prelink.py`). `erx/out/` and `erx/step2/out/` are repo artifacts; `erx/step2/gen/`
is a scratch dir that rebuild.sh clears before regeneration.

### Verified

* `nice -n 19 ./rebuild.sh` from the existing tree: the step-2 pipeline regenerated
  (`erx/step2/recomp_erx.log`: "Recompilation completed successfully", 59 modules /
  1891 functions via `erx_post.py`), `runner/erx/` populated (1896 files incl.
  `erx_register.cpp`), `erx_tables.{h,cpp}` generated (59 modules, 288 bindings) and
  copied, cmake re-configure ran, `ps2EntryRunner` linked (73,685,288 B).
* The clean-checkout order (setup.sh -> rebuild.sh) is exercised by construction:
  setup.sh builds the tools (the patch carries all step-2 changes; verified
  byte-identical to the working tree for ps2_runtime.{h,cpp}, CMakeLists.txt,
  ps2_recompiler.cpp) and rebuild.sh re-runs everything game-specific.

Quirk handled: step 2's synthetic-ELF verification left `erx/step2/{cfg/TEST.toml,
wrap/TEST.elf,gen/TEST}` scratch behind, which `erx_post.py` would fold into the
shipped runner (60 "modules", one stray `sub_01F00000`). rebuild.sh now drops that
scratch before regenerating (it is regenerable from erx/step2/README.md section 2),
so the output is exactly the documented 59 modules / 1891 functions.

## 2. Milestone 2 — guest heap below the module region

**What was done**: `game/ufe3_overrides.cpp` calls the **existing**
`PS2Runtime::configureGuestHeap(0, 0x01C00000)` in `applyUfe3` (i.e. during
`loadElf`, before any guest allocation exists). No change to `tools/PS2Recomp` was
needed — the guest-heap task's "minimal generic change" allowance went unused.

Why this is sufficient and clean:

* The heap's only hard ceiling is `kGuestHeapHardLimit = 0x01F00000`
  (`ps2_runtime.cpp`); `clampGuestHeapLimit` accepts any *lower* limit, so
  `configureGuestHeap(base, 0x01C00000)` yields exactly `[suggestedBase,
  0x01C00000)` — the allocator structurally cannot hand out 0x01C00000..0x01F00000.
* Nothing re-opens the range later: UFE3 executes **no** SetupHeap (syscall 0x3D)
  or EndOfHeap (0x3E) — verified by scanning the ELF (the only two 0x3D words in
  the file are data at 0x4423F8/0x452F3C, not code). The other high-RAM users stay
  above the region: RPC packet pool at 0x01F00000, async callback stacks grow down
  from 0x02000000 to the 0x01F00000 floor.

**Verified** with `PS2X_TRACE_HEAP=1` (90 s boot, `work/run_erx1.log`) using
`erx/step3/check_heap_trace.py`:

```
heap ops seen: 43 {'malloc_r': 1, 'free_r': 42}
OK: no allocation in 0x1c00000..0x1f00000
```

(all returned addresses were in 0x00500000..0x00BE26C0, far below the region; the
selftest run additionally allocated the argv/path blocks and still stayed below).

## 3. Milestone 3 — loader hooks

### 3.1 Hooks (game/ufe3_overrides.cpp)

* `replaceFunction(0x3723E0, ufe3ErxLoad)` — modload #8 "load": reads the path from
  guest memory, maps it to the module (`cdrom0:\ERX\PLAYER00\PL_00.ERX;1` -> `PL_00`),
  copies `erx/out/<MOD>.bin` to its fixed base from the manifest table, returns
  `index+1` as the handle. Idempotent (repeat loads return the same handle and skip
  the copy). Unknown/non-ERX paths get a dummy positive handle (log-once) — never a
  negative, because the game's load loop retries forever on negative.
* `replaceFunction(0x372420, ufe3ErxStart)` — modload #16 "start": resolves the
  module from handle (a0) or path (a1), `bindErxImport(slot, target)` for every
  entry of that module's binding list — for MISN modules that includes the 3 shared
  `Misn` slots (0x35BA18/0x35BA20/0x35BA28; the 0x35BA30..0x35BA38 tail of the
  documented `0x35BA18..0x35BA38` range is zero padding in the ELF, so the brief's
  "4 slots" are 3 real pairs), i.e. starting a mission module re-binds them to it —
  then calls the module start function (below) and returns 0, also writing the start
  return code to `outStruct+0` like sceErxStartModule (0x36f388).
* Metadata comes from a **build-time C++ table** (`erx/step3/erx_tables.py` ->
  `erx_tables.{h,cpp}`): name, disc path, base/size, `.erx.lib` entry address, and
  the 288 slot->target bindings. Justification: the only runtime data needed is the
  raw image `erx/out/<MOD>.bin` (kept as a data file, located by walking up from the
  runner's working directory `disc/`; `PS2X_ERX_DIR` overrides), everything else is
  static — compiling it in avoids a JSON parser and metadata path discovery in the
  override.

### 3.2 Module start function (reverse-engineered from work/re/main.s)

`sceErxStartModule`'s call at **0x36f330..0x36f358** (frame prologue at 0x36f080
shows `$19 = a0 = the module's "eRxLiB" descriptor`, `fp+8 = the out pointer`):

```
36f320: lw   $5, 0x1c($19)     ; fn  = descriptor+0x1c  = .erx.lib word at entry+0x20 = export word 0
36f330: beq  $5, -1 -> skip    ; 0xFFFFFFFF = no start function (all 59 have one)
36f338: lw   $2, 0x20($19)     ; gp  = export word 1 (entry+0x24)
        move $gp, $2
36f344: lw   $2, 0x1c($19)     ; v0  = fn (jalr $2 leaves v0 = fn on return!)
36f348: move $5, $18           ; a1  = argc (count of the built string list, >= 1)
36f34c: move $6, $21           ; a2  = argv (pointer array, 0-terminated, built on stack)
36f350: move $7, $19           ; a3  = the .erx.lib descriptor
36f354: jalr $2                ; call fn(a0=0, a1=argc, a2=argv, a3=entry)
36f358: move $4, $zero         ; (delay) a0 = 0
36f35c: move $17, $2           ; return code; low 2 bits select the status update
                               ; (0 = success path), and it is stored to *out (0x36f388).
```

(The two `<unknown>` R5900 MMI words around the `gp` load are the `move $16,$gp` /
`move $gp,$16` save/restore pair.) The argv strings come from this routine's own
arguments (fp+0/fp+4 blocks fed by the ELOADP request path) — their exact contents
are unknowable without running liberx and are **unobservable**: export words 0..3
are identical in all 59 modules and point at a bare `jr $ra` liberx-entry stub
(verified: word at `funcs[0]` = `0x03E00008` in all 59 images). Because `jalr $2`
leaves v0 = fn and the stub returns it unchanged, `ret == fn` and `ret&3 == 0` — the
success path — on real hardware too.

`erxCallModuleStart` reproduces this exactly: fn/gp read from guest memory at
`entry+0x20/+0x24`, `gp` set around the call, v0 preset to fn, `a0=0, a1=1,
a2={path, NULL}` (a persistent 8-byte guest block), `a3=entry`, the call dispatched
through `runtime->lookupFunction(fn)` with `ctx->pc` semantics identical to a guest
`jal`, and the guest `$ra`/gp preserved. Unknowns left open: the exact argv contents
(unobservable, see above) and the ±4 byte ambiguity of the descriptor pointer
(`$19` behaves like `entry+4` vs the raw entry; unobservable because the export
quadruple words are identical).

### 3.3 Two dispatch-path findings (why the hooks are installed twice)

1. **Static-init crash (fixed in the allowed `erx/step2/erx_post.py`)**: step 2's
   generated `erx_register.cpp` registered the 1891 module functions from a *static
   initializer* — that ran before `ps2_runtime.cpp`'s namespace-scope
   `g_overflowFunctions` map was constructed (static initialization order fiasco) and
   the binary died instantly with SIGFPE inside the map's bucket math (gdb backtrace:
   `ErxModuleFunctionTableInitializer` -> `unordered_map::operator[]`). `erx_post.py`
   now emits `void erxRegisterModuleFunctions()` (idempotent) and the game override
   calls it at runtime-init time. No runtime change was needed.
2. **`replaceFunction` is bypassed by direct `j`**: the code generator emits
   `jal <stub>` as `dispatchGuestBranch(...)` (function table -> `replaceFunction`
   works) but `j <stub>` (tail jump) as a **direct C++ call to the generated slot
   body** (`sub_00372348_0x372348(rdram, ctx, runtime); return;` in
   `work/output/sub_00276298_0x276298.cpp`), which runs `callErxImport` and ignores
   the table. The boot's last library registration is exactly such a tail call
   (`0x2763f0: j 0x372348`). Every kernel stub slot is therefore hooked through
   **both** `replaceFunction(slot, handler)` and `bindErxImport(slot, synthetic
   target)` where the synthetic target (0x01F80000..0x01F8000C, overflow registry,
   never executed as guest code) maps to the same handler; `callErxImport`'s
   tail-call trampoline makes both paths resume at the caller's `$ra`.

### 3.4 Kernel stub slots (sysmem/loadcore/modload)

With per-slot dispatch, the old trap fall-through (which silently continued with an
undefined v0) became a boot-killing unlinked trap, so the override gives all 73
kernel slots deterministic handlers (the brief allows `replaceFunction` overrides
for these): 0x3723E0/0x372420 = the ERX hooks, **0x372400 (modload #0xC LoadModule)
returns -1** so liberx's ELOADP attach takes its documented graceful failure path
("liberx: ELOADP is not available(err=%08x)" at 0x36AECC, stores -1 at 0x4C52C4,
continues; the ERX modules do not need ELOADP), and the remaining 70 return 0
(success — their only boot-time callers are the game's library-registration loop at
0x2762D0.. whose 18 `RegisterLibraryEntries` returns are ignored, and liberx init's
ignored sysmem call at 0x36AF18).

### 3.5 When does the game actually load modules? (answer: on demand)

The loader loop's true entry is **0x2763f8** (the documented 0x276408 is its
prologue), it takes index arguments (`mult $a1, $12` with the 0x14/0xc descriptor/
state strides) and is called through a function pointer — it is an **on-demand
per-module loader**, not a boot-time eager loop (the design doc's Q4 eager-load claim
was flagged as a risk there and is wrong). Evidence from the 90 s boot: the state
array 0x3D32E8 stays all-zero and no `[erx]` line appears — the title screen needs
no character/mission modules. The hooks therefore fire "when the game calls them"
(battle/character-select paths), which is exactly why the selftest below exists.

### 3.6 Hook verification (PS2X_ERX_SELFTEST=1, work/run_erx2.log)

The selftest drives all 59 modules through `PS2Runtime::callErxImport(rdram, ctx,
0x3723E0/0x372420)` — the exact dispatch path the generated slot bodies use — and
checks handle allocation + load idempotence + start return:

```
[erx] selftest: 59/59 modules load+start+idempotence OK
```

with zero `[erx-import] unbound`, zero missing-target lines.

## 4. Milestone 4 — instrumentation (`PS2X_TRACE_ERX=1`)

One line per load and per start (plus one idempotent-repeat line per repeated load;
error paths log once per distinct path/handle). Exact lines from `work/run_erx2.log`:

```
[erx] load PL_00 base=01c00000 size=0000f678 handle=1 tick=0 path=cdrom0:\ERX\PLAYER00\PL_00.ERX;1
[erx] load PL_00 base=01c00000 size=0000f678 handle=1 tick=0 (already loaded, idempotent)
[erx] start PL_17 base=01cda200 handle=18 tick=0 bindings=7 start=01cdd8fc ret=01cdd8fc
[erx] start MISN_20 base=01deaf00 handle=59 tick=0 bindings=3 (Misn slots rebound) start=01deb4a4 ret=01deb4a4
```

Format: `load <name> base= size= handle= tick=<vsync tick> path=` and
`start <name> base= handle= tick= bindings=<count>[ (Misn slots rebound)] start=
ret=` (start = export word 0 = the module start function; ret = its v0, which the
`jr $ra` stub returns as the function address, i.e. `ret&3 == 0` = sceErxStartModule
success).

## 5. Milestone 5 — 90 s headless boot verification

Command (logs in `work/run_erx1.log` / final confirmation `work/run_erx3.log`, shots
in `work/shots_erx1` / `work/shots_erx3`):

```
PS2X_TRACE_ERX=1 PS2X_TRACE_HEAP=1 PS2X_HIDE_DEBUG_UI=1 \
PS2X_SHOT_DIR=<abs>/work/shots_erx1 \
PS2X_PAD_SCRIPT="start@700-715,start@900-915,cross@1100-1110" \
PS2X_IDLE_DUMP=5000 PS2X_EE_PEEK=0x3d32e8,0x42ab5c ./run_headless.sh 90
```

### Modules loaded/started during the boot

**Zero — and that is correct**: the game's module loader (true entry 0x2763f8,
section 3.5) is on-demand and the boot-to-title path never needs a character or
mission module. Evidence (exact lines):

```
[idle-dump]  ee 003d32e8: 00000000 00000000 00000000 ...   (module state array: untouched in all dumps)
[idle-dump]  ee 003d3468: 0048b958 00000000 ...            (descriptor table: path ptrs, zeroed state)
```

`grep -c '\[erx\] load\|\[erx\] start ' work/run_erx1.log` -> `0`, and the same run
contains **no** `[erx-import] unbound import stub` and **no**
`[guest-branch:missing-target]` lines (`grep -c 'unbound\|missing-target'` -> `0`).
That the hooks work when the loader does call them is proven by the selftest
(section 3.6: 59/59 through the real dispatch path).

### Boot still reaches the title scene as before

Scene id peeks (`0x42AB5C` = taskmgr current scene, 5 s apart) reproduce
work/mimo_post_movie_b.md's timeline exactly: `0 -> 2 (WARNING) -> 3 (LOGO) -> 0x13
("MTTITLE D")` at ~t=25 s (vsync tick ~575), then stable `0x13` to the end:

```
[idle-dump]  ee 0042ab5c: 00000002 ffffffff ...
[idle-dump]  ee 0042ab5c: 00000003 ...
[idle-dump]  ee 0042ab5c: 00000013 ...
```

44 PNG shots through `frame_5280.png` (tick 5280 ~= 88 s); the frames show the same
content as part B (WARNING text screen early; scene 0x13's movie plane later).
Frames/GS are healthy: 1535 `[shot:gs]` draw records in the log.

### Heap trace

`python3 erx/step3/check_heap_trace.py work/run_erx1.log`:

```
heap ops seen: 43 {'malloc_r': 1, 'free_r': 42}
OK: no allocation in 0x1c00000..0x1f00000
```

### Pad script (`PS2X_PAD_SCRIPT="start@700-715,start@900-915,cross@1100-1110"`)

`[pad-script] 3 entries` — the presses are delivered at the given vsync ticks, but
**nothing changes**: the scene stays 0x13 across all dumps (ticks 700-1110 fall
inside the 0x13 park), no module loads appear, and no error/trap lines appear. The
title's flow slots 6/7 (completed only by the input/flow handlers, part B finding
11/12) are still not completed by these presses.

### Final confirmation run (results)

`work/run_erx3.log` (90 s, exact final binary, same protocol as above) — matches
run_erx1 exactly:

* `[erx]` load/start lines: `0`; `unbound|missing-target|FAIL`: `0`.
* Scene peeks (18 dumps): `0, 2, 2, 3, 0x13 x14` — title reached and stable.
* Module state array `0x3d32e8` all-zero in every dump (loader not invoked).
* `check_heap_trace.py`: `heap ops seen: 43 {'malloc_r': 1, 'free_r': 42}` /
  `OK: no allocation in 0x1c00000..0x1f00000`.
* 1533 `[shot:gs]` draw records, shots through `frame_5280.png`; pad script
  (`[pad-script] 3 entries`) produced no change (scene stayed 0x13).

No background processes left behind (run_headless.sh tears down its private
kwin/Xwayland/D-Bus session; verified after each run).

## 6. Summary of what changed where

| file | change |
|---|---|
| `game/ufe3_overrides.cpp` | guest-heap cap; ERX load/start hooks; kernel-stub handlers + double dispatch binding; `erxRegisterModuleFunctions()` call; `PS2X_ERX_SELFTEST=1` selftest |
| `rebuild.sh` | ERX regeneration/copy wiring, cmake re-configure, step-2 TEST scratch cleanup, `find` exclusion |
| `erx/step3/erx_tables.py` | new: build-time metadata table generator |
| `erx/step3/check_heap_trace.py` | new: PS2X_TRACE_HEAP verifier |
| `erx/step2/erx_post.py` | generated registration is now `erxRegisterModuleFunctions()` (explicit call) instead of a static initializer (SIOF crash) |
| `tools/PS2Recomp` | **no changes** (the guest-heap allowance went unused) |

Known gaps / follow-ups: the real on-demand load trigger (battle/character-select
flow) was not reachable in a 90 s headless boot, so live-in-game loading is proven
only via the selftest dispatch path; module argv contents for the start function are
unobservable (section 3.2); the `Misn` slots are 3 real pairs, not 4.

## Post-run notes (reviewer, 2026-10-04)

- The run hit its time cap during verification. Two follow-ups were applied by the reviewer:
  - `0x3722A0` and `0x372348` are reached by direct `j` tail calls, which the recompiler emits as direct C++ calls that
    bypass `runtime.replaceFunction`; they are now bound at compile time in `work/ufe3.toml` (`ret0@...`). Boot no longer
    derails at the library-registration loop.
  - The design study's "all 59 modules load at boot" is wrong: the loader at 0x2763F8 (0x276408 is mid-function) loads
    ONE module by index and is only referenced from a function-pointer table at 0x4553CC. Modules load on demand
    (character/mission selection), so the 0x3723E0/0x372420 hooks are not exercised until the title screen is passed.
