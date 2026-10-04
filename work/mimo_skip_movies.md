# UFE3 (SLPS-25441) — skip opening FMV movie(s)

Date: 2026-10-04
Status: **patch implemented and built. Runtime verification PENDING — blocked: a game
(dota2, PID 362957) was running on the machine, and the hard rule forbids launching
while `pgrep -x dota2` prints a PID.**

## 1. What plays the movie, and how "finished" is decided

The game has one shared FMV player object (global instance at **0x412AD0**, methods in
`0x32B5xx–0x32D0xx`). It is driven by scene wrappers in `0x21Bxxx`.

### Player internals (sub_ names = recompiled guest functions)

| addr | role |
|---|---|
| `sub_0032B980` (0x32b980) | open/start state machine, state at obj+0xdc, 7 states, jump table 0x49F804. Wrappers: `0x32b950` (arg 1) / `0x32b968` (arg 0). |
| `sub_0032BD50` (0x32bd50) | decode step state machine, state at obj+0xe0, jump table 0x49F820. Step 1 (`0x32bf38`) is what drives the IPU directly: stores to 0x10002000 / 0x10002010 plus `sceIpuSync(0,0)` calls at 0x32bfe4/0x32bff4/0x32c024/0x32c034/0x32c058. This (and step 2 = `0x32c140`) produces the endless garbage 16x16 macroblock uploads (GS `xfer w=16 h=16 dbp=0x1184` spam in the logs). |
| `sub_0032BCD8` (0x32bcd8) | pump: loops `0x32bd50` until decode state 6. |
| `sub_0032C398` (0x32c398) | builds GS packets for the current movie frame (uses hdr width/height via `0x32c9d8/0x32c9e8`). |
| `sub_0032C9F8` (0x32c9f8) | query: total frame count (movie hdr +0xc). |
| `sub_0032CAC0` (0x32cac0) | query: current position/frame index (obj+0x12c, or obj+0x134 if <0). |
| `sub_0032CAD8` (0x32cad8) | **query: "end of movie reached?" — returns 1 iff `0x32cac0(obj) == 0x32c9f8(obj) - 2`. Pure read-only query.** |
| `sub_0032BCB0` (0x32bcb0) | set pause flag (obj+0xe4) — used by the "user pressed Start to skip" path. |
| `sub_0032B5B0` (0x32b5b0) | movie close/stop. |

### Scene wrapper (`0x21B9xx`)

- `0x21b940` — start movie (FSM at wrapper+0): opens the player (`0x32b8f8`/`0x32b930`),
  starts it (`0x32b968`), returns 1 when started.
- `0x21ba40` — per-frame: pump (`0x32bcb8`+`0x32bcd8`, only when `0x32cad8()==0`), draw
  current frame (`0x32c398` + `0x34f4d0`), then **isDone: returns 1 iff
  `0x32cad8(global) != 0` AND `0x356b88() == 0`** (`0x356b88` = sound-driver RPC
  query cmd 0x24 "busy?", via `0x356e10` → `sceSifCallRpc`; sole caller is 0x21bb34).
- `0x21bb68` — stop wrapper.

### Boot scene: COpening (vtable 0x47AA38; `COpening::process` = 0x2a2ed8)

Sub-state machine (via `0x357f40`/`0x357f30`, table 0x491E20):
- state 1 (0x2a2f74): start movie via `0x21b940(&obj+0x10, 0x84, 0x31, 0)`; when started → state 2.
- state 2 (0x2a2f98): poll `0x21ba40`; if != 0 → **skip path 0x2a2fd0** (set player pause
  flag `0x32bcb0(0x412AD0,1)`, `0x355fa0(0x43F040,0)`, brightness 0xff, state 3).
  This is *exactly* the same path taken when the pad check (buttons mask 0x8f0 at
  0x43E580+0x2cc/+0x37c, i.e. Start/Cross etc.) registers a skip.
- state 3 (0x2a3000): 16-frame fade-out, then `0x21bb68` + `0x32b5b0` (movie stop) → state 4.
- state 4 (0x2a304c): `0x32fcb8(0x42A560, 0x14)` — switch to the next task/scene (title).

So "movie finished" and "user pressed Start" converge on the same code; the scene flow
itself is untouched.

### Other FMV paths (same end-check `0x32cad8`)

- `0x286e18` (scene with obj+0xdb0 state, pad mask 0x8f0) — done when
  `0x32cad8()==1` or pad pressed → `0x21bb68` + `0x32b5b0` + task switch. (attract/demo-style FMV)
- `0x289xxx` (obj+0x318 state) — done when `0x32cad8()!=0`, else pump+draw.
- `0x2b84b4` — done check at the end of a frame step; returns 1 vs 3 based on `0x32cad8`.
- `CEffMisn10FinalMovie` (vtable 0x47A1D8, `0x230bc8`) — in-mission ending movie effect;
  drives the pump at fixed frame indices (0x6e1 / 0x71b) via `0x32cac0`; not part of boot.

All non-ending-movie FMV players funnel their "finished" decision through `0x32cad8`.

Confirmed non-FMV neighbours (no extra skip needed):
- `CLogo::process` (0x295028, vtable 0x47A948) is a static logo scene (own 9-state FSM at
  table 0x4917D8, renders via 0x2a7a88 / 0x2952c8 / 0x2952e8); no movie player involved.
- `CMtitle::process` (0x29CE20, vtable 0x47A9D8) is the title/menu screen (pad left/right
  handling on bits 0x1000/0x4000); no movie player at its start.
- Scene switching: `0x32fcb8(taskmgr, id)` stores the next scene id into taskmgr+0x600;
  `0x32fcd8` (task manager tick) latches it and switches scenes. COpening's exit state
  requests id 0x14 (= 20, the post-opening scene, expected to be the title).

## 2. Change made

`work/ufe3.toml`, `stubs` list, one line added (plus a comment):

```toml
"ret1@0x0032CAD8",
```

i.e. the recompiled `sub_0032CAD8` (the player's end-of-movie query) now executes
`ps2_stubs::ret1(...)` and always reports "finished". Verified in generated code:
`work/output/sub_0032CAD8_0x32cad8.cpp:13: ps2_stubs::ret1(rdram, ctx, runtime);`

Rationale for this exact spot:
- It is the *player reporting finished*, not a scene being ripped out — COpening still runs
  its own skip/fade/stop/next-scene sequence (identical to the user pressing Start).
- It's a read-only query, so replacing it loses no side effects (frames still drawn via
  `0x32ba40`'s draw path, fade still runs, movie still stopped/closed by game code).
- It covers every FMV scene (opening + the other three call sites above), not just the
  first one.

No changes to `tools/PS2Recomp/`, no other files touched.

Note: with `0x32cad8` returning 1, the pump is skipped in `0x21ba40` (and the other
scenes), so the endless garbage macroblock uploads stop as well — the movie is skipped
on the first frame of the play state.

Possible residual risk (to watch in verification): `0x21ba40`'s done also requires
`0x356b88()` (sound "busy" RPC) to return 0. If the HLE sound RPC reports busy forever,
COpening would still wait. If that shows up, the follow-up is `ret0@0x00356B88` (its only
caller is 0x21bb34, the done check).

## 3. Verification

PENDING. Planned command (new output dirs, no screenshot tools, per hard rules):

```
pgrep -x dota2            # must print nothing first
PS2X_HIDE_DEBUG_UI=1 PS2X_SHOT_DIR=$PWD/work/shots_mimo1 PS2X_SHOT_EVERY=300 \
  ./run.sh 120 > work/run_mimo1.log 2>&1
```

What to check:
- No more `xfer ... w=16 h=16 dbp=0x1184` macroblock spam in the log after the warning screen.
- Frames after the warning screen describe what comes next (expected: brief blank/fade,
  then the title screen / CMtitle, or a new stuck point to be diagnosed).
- `hist_*.txt` should show `sub_002A2ED8` (COpening) briefly, then the next scene's
  functions instead of the 0x32Cxxx movie cluster.

Blocked at time of writing: `pgrep -x dota2` printed PID 362957 (Steam dota2, started
~9 min before the check). Hard rule: do not launch while a game is running.

Resolution: a background waiter (`/tmp/opencode/wait_dota2.sh`, max 2 h) polls
`pgrep -x dota2` every 30 s; as soon as it exits, the verification run above will be
launched (after re-checking `pgrep -x dota2` prints nothing) and this file updated.
