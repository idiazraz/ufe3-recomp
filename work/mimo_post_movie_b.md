# Post-movie black screen investigation (part B)

## Finding 0 — Context (verified before this session)

- Game: Ultraman Fighting Evolution 3 (SLPS-25441), static recompilation via PS2Recomp in `tools/PS2Recomp`.
- Boot sequence: memory-card check -> health warning -> opening movie. The movie is skipped via `work/ufe3.toml`:
  `"ret1@0x0032CAD8"` (movie-ended query returns 1) and `"ret0@0x00356B88"` (movie-audio-busy returns 0).
  After the skip: permanent black screen.
- The game's FILEn.BIN load queue (manager at 0x4BD000; phase/cur/end at 0x4BD868 / 0x4BD86C / 0x4BD870) now drains
  fully thanks to `game/ufe3_overrides.cpp`: SOUNDMAN load RPCs 0x356948 / 0x356998 are served synchronously and
  0x356A18 "done" is forced to 1. Details in `work/mimo_post_movie.md`.
- Still black: every frame the GS receives only the framebuffer clear plus two untextured full-screen triangles with
  alpha 128 (a full black fade overlay). No textured draws, no VIF1/VU1 traffic.
- Per-frame call counts (`work/shots28/hist_4800.txt`, ~267 frames): sub_00326AF0 x801; sub_0032EFB8, sub_0032FCD8
  (task manager), sub_0032B980, sub_003274D8, sub_00326760, sub_0021AC30 x534; sub_0032F9C0, sub_0032EE20,
  sub_0032CB48, sub_0032F618, sub_0032BCA8, sub_0032B968, sub_00327580, sub_00327468, sub_003270A0, sub_00327038,
  sub_00326298 x267.
- sub_0021AC30 / 0x21AC50 sit next to the opening scene (COpening wrapper 0x21BA40 polls the movie-ended query).
- Scene ids: `sub_0032fcb8(taskmgr, id)` stores the next scene id at taskmgr+0x600; COpening's exit requests id 0x14.
- Tools: disassembly in `work/re/main.s`; generated C++ in `work/output/sub_*.cpp`; data offset:
  file offset = vaddr - 0x200000 + 0x1000. Env switches: `PS2X_SHOT_DIR=<dir> PS2X_SHOT_EVERY=300` (PNG shots +
  hist_*.txt + "[shot:gs]" lines), `PS2X_IDLE_DUMP=<ms>`, `PS2X_EE_PEEK=addr[*],...` (EE words),
  `PS2X_HIDE_DEBUG_UI=1`.
- Rules for this session: only edit `work/mimo_post_movie_b.md`, `game/ufe3_overrides.cpp`, `work/ufe3.toml`.
  Launch only via `./run_headless.sh <seconds>`; rebuild via `nice -n 19 ./rebuild.sh`. No installs. 40 min budget.

## Finding 1 — Prior report summary (work/mimo_post_movie.md, read in full)

- The load-queue hang root cause was an IOP-emulation gap (SOUNDMAN job-engine semaphore
  token stranded; plus type-6 jobs memcpy into IOP RAM space so data never reached EE RAM).
  Workaround in `game/ufe3_overrides.cpp`: `sub_00356948`/`sub_00356998` (0x356948/0x356998)
  → synchronous `loadFileRangeToEe` reading FILE1-4.BIN straight into the EE dest, and
  `sub_00356A18` (0x356A18) → always "done" (1).
- Verified previously: queue drained (phase=0, cur=end=0x14 at 0x4BD868/86C/870), entry-13
  data ("POM" magic) present at 0x00BFCCC0, EE alive in scene code (PC=0x00323008, 2 threads).
- The fade overlay still covers the screen: shots were obscured by the runner's debug UI;
  a follow-up with `PS2X_HIDE_DEBUG_UI=1` was recommended — that is this session's starting
  point. `PS2X_SHOT_DIR` must be an **absolute** path.
- Prior caution: a headless run can leave a `dbus-run-session -- kwin_wayland --socket
  ufe3-headless` process behind; must be cleaned up (run_headless.sh's group kill may miss it).

## Finding 2 — Task manager (scene switcher) semantics, from `work/re/main.s`

- `sub_0032FCB8(taskmgr, id)`: if `id < 0x2A` (42 scenes) stores `id` at **taskmgr+0x600**
  (pending next-scene id) and returns 1. `sub_0032FC60(taskmgr, id)`: same store plus
  `taskmgr+0x5FC = 0`.
- `sub_0032FCD8(taskmgr)` (per-frame tick): if `*(taskmgr+0x600) != -1` → copies it to
  **taskmgr+0x5FC** (current scene id), sets `+0x600 = -1`, calls `sub_0032FD30`
  (scene switch). Then calls 0x357AA8(`taskmgr+0x5F8`) = update current scene object.
- `sub_0032FD30(taskmgr)`: if `taskmgr+0x5F8` (current scene object) nonzero → calls its
  vtable+4 (scene exit) and zeroes it; allocates 0x1C bytes (0x3312B0), zero-fills
  (0x3574E8), stores at `+0x5F8`; looks up scene table entry `taskmgr+0x10 + id*0x24`
  (entry size 0x24) and calls 0x357A40(newObj, entry+0x10 = create/init fn or taskmgr, 0).
- Tick call site 0x231FF8: `sub_0032FCD8(r22 - 0x5AA0)` with `r22 = 0x00430000` (lui only)
  ⇒ **taskmgr base = 0x0042A560**. Live state words to peek:
  - current scene id: **0x42AB5C** (taskmgr+0x5FC)
  - pending scene id: **0x42AB60** (taskmgr+0x600, -1 = idle)
  - current scene object: **0x42AB58** (taskmgr+0x5F8)
  - scene table: 0x42A570, 0x24-byte entries, create fn at entry+0x10.

## Finding 3 — COpening's per-frame tick (sub_0021BA40) returns "done" correctly under the stubs

- `sub_0021BA40(this)`: `this[0]` = frame counter (init via `sub_0021AC50` when 0). Odd
  frames only: polls the movie-ended query `sub_0032CAD8(0x412AD0)` (movie player object
  at **0x412AD0**); if not ended it ticks the movie player (0x32BCB8/0x32BCD8).
- Every frame: 0x32C398(obj, 0x80A, 0), 0x32CB28(obj) → info struct, then draws a
  full-screen fade via **0x34F4D0** with args (r4..r7 = 0x80,0x80,0x80,0x80, r10 = 0xFFFFFF,
  r11 = 0x280, sp+0xE0, w/h halves from info+0x38/0x3A, sp+0x48 = 0x80A) — this is the
  "two untextured full-screen triangles, alpha 128" overlay seen on the GS.
- End of tick: polls 0x32CAD8 again; if movie ended → polls `sub_00356B88` (movie-audio
  busy, stubbed ret0) → returns **1 ("scene finished")**. With the current stubs the tick
  returns 1 on its very first odd call. If this function were running, COpening would have
  exited already — so either it is not running any more, or the return value is not acted on.

## Finding 4 — Live state (30 s headless run `work/run_b1.log`, shots `work/shots_b1/`)

`PS2X_HIDE_DEBUG_UI=1 PS2X_IDLE_DUMP=3000 PS2X_EE_PEEK=0x42ab58,0x42ab58*,0x4bd228,0x412ad0,0x4bd868`:

- **Q1 answer (first half): the scene running is scene id 2, not 0x14.** taskmgr words at
  0x42AB58: `obj=0x00638BC0, cur_id=0x00000002, pending=0xFFFFFFFF (-1)` — stable across all
  dumps. Scene object at 0x638BC0: `{vtable=0x0047C380, 0x0067A404, 0, 0x0067A404,
  0x0067A404, 0x0067A430, ...}`.
- Load queue (0x4BD868): `phase=0, cur=0x7, end=0x7` early (queue re-filled to 7 entries and
  drained again), then stable — never stuck. The `cur=end=0x14` of the previous session was
  the first batch; the game queues more as the scene runs and it all drains.
- Movie player object 0x412AD0: all zeros (fully torn down).
- The 0x4BD228 object (used by the per-frame sub_0021AC30/0x21AC50): `{1, 0, 0x0067CD84, 0,
  0x10000, 0x10000, 0x18, ...}` — active, holding a resource pointer.
- **The GS log is no longer "clear + 2 untextured triangles" only**: per displayed frame the
  history shows textured draws (prim=6, tme=1, verts=2, tbp0=0x2400 1024x512 PSMCT24/0x13,
  tfx=0, tcc=1, cld=1 clut at 0x1A90, abe=1, alpha=0x44, a=128..128) covering the full
  640x224 area at x=1728..2368 y=1936..2160, plus local-to-local xfers (dir=3, 640x224
  dbp=0x1180) and untextured a=0 draws. So scene 2 IS rendering content through a
  fade/blend layer (alpha 128), not pure black.

## Finding 5 — Scene id map (registration block 0x232450..0x2326C0) and scene names (ELF)

`sub_0032FC60(taskmgr, 1)` at boot ⇒ initial scene = **id 1**. `sub_0032FC70(taskmgr, id,
value, name)` registrations; names read from the ELF (offset = vaddr-0x200000+0x1000):

| id | name | entry value | id | name | entry value |
|---|---|---|---|---|---|
| 0 | FLOW SELECTER | taskmgr (0x42A560) | 0xC | ASTM TEST | 0x3D15E8 |
| 1 | POWER ON | 0x3F11A0 | 0xD | OPTION | 0x3E3CD0 |
| 2 | **WARNING** | **0x407680** | 0xE | AUTOSAVE | 0x3C15D0 |
| 3 | LOGO | 0x3D7340 | 0xF | AUTOLOAD | 0x3C1650 |
| 4 | OPENING | 0x3E45F8 | 0x10 | DEMO | 0x3D4CE0 |
| 6 | MTITLE | 0x3D9148 | 0x11 | LOADING | 0x3D66A8 |
| 7 | ---- | *(0x4B4690) | 0x12 | STORY SEL | 0x402548 |
| 8 | KOMIYA_TEST | 0x3D6698 | 0x13 | MTITLE D | 0x410280 |
| 9 | ARIHARA_TEST | 0x3B4310 | 0x14 | HIDDEN ELEM | 0x40FD20 |
| | | | 0x15 | REPLAY SELECT | 0x40D820 |
| | | | 0x16 | NEW VIEW | 0x3E3BB8 |

- Live peek of the registered table at 0x42A5B8 confirms: entry 2 = 0x00407680 "WARNING";
  entry 3 = 0x003D7340 "LOGO" (entry layout: value at +0, name at +4).
- **Q1 refined: the running scene is id 2 = the WARNING (health-warning) scene**, NOT
  COpening (id 4) and NOT 0x14 ("HIDDEN ELEM"). pending = -1 (no transition requested).
- "COpening's exit requests id 0x14" (inherited context) = scene 4's normal exit →
  "HIDDEN ELEM". The `cur=end=0x14` of the previous session was the load-queue index,
  an unrelated coincidence.

## Finding 6 — The scene table's value field points into .data, not code

- All scene entry values (0x407680, 0x3D7340, 0x3E45F8, 0x3F11A0, 0x3B4310, ...) lie in
  **.data** (0x3B2580..0x440900); .text ends at 0x3AC118 (ELF section table) and the
  generated C++ in `work/output/` stops at sub_003B2548. The 0x407680 region contains
  mostly GS-register-looking values (e.g. 0x30000d = TEST register seen in the GS log) —
  scene *state/configuration* blocks, dispatched through the framework code at
  0x3579xx-0x357Fxx (.text).
- Framework dispatch (main.s 0x357A40/0x3579C8/0x357AA8): per-frame update is
  `0x357AA8(handle)`: `actor = *(handle+0xC); fn = *(*(*actor+0x18)); fn(*(actor+0x18),
  actor)`; then walks the `actor+0x1C` linked list. Live handle 0x638BC0 =
  `{vtable 0x47C380, 0x0067A404, 0, 0x0067A404, 0x0067A404, 0x0067A430}`.

## Finding 7 — The WARNING scene renders its text screen; the shot capture is phase-dependent

- `work/shots_b2/frame_600.png` (debug UI hidden) shows the real Japanese health-warning
  screen: white text on black — "このソフトを使用するときは、部屋を明るくし、なるべくテレビ画面から離れてください。"
  etc. (the standard epilepsy warning). So scene 2 IS rendering correctly.
- Pixel stats: frame_300/900/1500 are 100% black; frame_600 has 23,500 non-black pixels
  (white text). The presented frame alternates with the double-buffered draw stream — the
  per-frame GS pattern has 2 textured sprites (tbp0=0x2400, 1024x512 PSMCT24 — 24-bit so
  texture alpha reads 0x80 = the "alpha 128" in the log), local-to-local xfers (640x224,
  dbp=0x1180) and untextured a=0 sprites per draw buffer (fbp=0x8c / fbp=0x0). The blend
  never animates (a=128..128 constant) — a fade-in is NOT in progress.
- So the "black screen" is not a rendering failure: the WARNING scene is up and drawing,
  and simply never transitions onward. The interesting question is now what it waits on.

## Finding 8 — The WARNING scene is NOT stuck: it self-advances (run_b5/b6/b7)

Live peeks of the scene object chain: handle 0x638BC0 → actor 0x67A404 (`+0x18` = the
scene block 0x407680). The block's methods (filled at runtime at 0x407680:
`{vt 0x47C390, update=0x2C15A8 (nop), create=0x2C1468, destroy=0x2C1570}`) hide the real
driver: **`sub_002C15C0`** (registered as a framework callback; its pointer lives in the
.data tables at 0x45FE50 / 0x47ACD0) is a **9-state machine** (jump table 0x495340):

- 0-2: staged init (`0x2A8318`/`0x2A8410`/`0x2A8520` resource/trigger setup);
- 3-4: set up fade (fade byte = 0);
- 5: **fade-in**: fade byte `+= 4` per tick, copied to RGB, until `>= 0x80` → clamp 0x80,
  `sub+0xBC = 0x258` (600), state 6;
- 6: **hold**: pad words `(sub+0xC0)|(sub+0xC4) & 0x8F0` nonzero ⇒ timer = 0; else timer
  `-1` per tick; at 0 ⇒ state 7;
- 7: **fade-out**: fade `-4` per tick to 0, then `sub_0032FCB8(taskmgr, 3)` — **request
  scene 3 (LOGO)**; state 8 = done.

Live values (`0x67CE78` = scene obj+0xB8; `sub` = obj+0x10 = 0x67CDD0):
- `sub+0xC8` (fade byte) = **0x80** (fade-in complete, state 6);
- `sub+0xBC` (timer) = 0x1B9 → 0xC9 → **0** (counts at ~50 ticks/s; pad words are 0 — no
  controller in the headless runtime, so it advances purely on the 600-tick timer).

**45 s run `work/run_b7.log` (shots `work/shots_b7/`) — the game advances on its own**:
- t≈5-10 s: scene **2 (WARNING)**, timer counting down; the warning text screen is shown
  (frame_300.png = white Japanese health-warning text on black);
- t≈15 s: scene **3 (LOGO)** — fade-out of the warning visible in frame_900.png;
- t≈20 s: scene **4 (OPENING)** (movie skipped instantly by the stubs; frame_1200.png
  shows striped/smearing 2D content of the transition);
- t≥25 s: scene **0x13 "MTTITLE D"** — **stable** through the end of the run (t=45 s).
  frame_2100/2400.png: black bands top/bottom and a **solid white 224-line band** across
  the middle (the movie display area rendering with no movie).

## Finding 9 — Scene 0x13 "MTTITLE D" reproduces the previous session's "black screen" signature

At scene 0x13 the GS stream matches the inherited description exactly: per frame
**untextured full-screen triangles (prim=4, verts=3, tme=0, a=128..128, abe=1,
alpha=0x44, tbp0=0x1184)** over the 640x224 movie area — i.e. the previous session's
"two untextured full-screen triangles with alpha 128" and "no textured draws" were
**this scene's movie plane**, not a fade stuck over a hidden scene. The previous session
had already progressed past WARNING/LOGO/OPENING (which is why its load queue showed
cur=end=0x14 and the scene code it saw was the movie player wrapper).

## Finding 10 — Scene 0x13 "MTTITLE D" method block and flow machinery

- Live peek of the static block **0x410280** (scene 0x13's entry value):
  `{vt 0x47C390, update=0x326298, create=0x3261E0, destroy=0x326260}` (same 3-slot method
  layout as WARNING's 0x407680). `sub_00326298` (the per-frame update) is a **no-op**
  (`jr $ra`); the scene is driven by callbacks like WARNING's.
- `sub_003261E0` (create): clears gp+0x2AB8/0x2ABC, `0x357CF0(2)` → gp+0x2AB8 actor,
  alloc 0x54 (0x3265A0 init) → gp+0x2ABC, `0x357A40(actor, obj, 0)`, tail-calls 0x2712F8.
- **Flow engine** (`sub_003262A0`/`sub_00326308`/`sub_00326350`/`sub_003263A8`/
  `sub_00326400`/`sub_00326458`) works over a **12-entry table at 0x410290** (block+0x10,
  0x1C bytes/entry: +2/+3 status bytes, **+4 = next scene id**, +8/+0x10/+0x14 params).
  `sub_00326458(idx, flag)`: entry+4 >= 0 ⇒ `0x2714F8`, (entry+4 == 7 ⇒ `0x282020`
  special) then **`sub_0032FCB8(taskmgr, entry+4)`** = request the next scene.
- Flow status object at **0x4BDC30** (via gp+0x2694): `+0x70` = flag (set by 0x2714F8),
  `+0x74` = value, `+0x78 + slot` = per-slot status byte (read by 0x2715B8, cleared by
  0x2715D0/0x2715E4). Slot pollers: 0x271A68/0x271A88, 0x28E678; slot completers:
  0x28E68C and 0x29CFBC/0x29D038/0x29D0A0 (0x29D030 also calls sub_0032FCB8 — a scene
  transition helper).

## Finding 11 — What scene 0x13 waits on (live) and what the previous session saw

- Live peek `0x4BDCA0` (= 0x4BDC30+0x70): `+0x70 = 1`, `+0x74 = 0`, status bytes at
  0x4BDCA8: **slots 0-5 = 0 (done), slots 6-7 = 0xFF (-1, pending)** — stable across
  dumps. The flow is parked on slots 6/7; only 0x28E68C (inside the port/memory-card
  handler 0x28E628, which itself calls 0x35FC60 + the sceMc* stubs) and the input/flow
  helpers at 0x29CFBC/0x29D038/0x29D0A0 complete those slots.
- Scene 0x13's per-frame profile (hist_2400.txt, 261 frames): `sub_0021AC30 x522`,
  `sub_0032CB48 x261` (a movie-player getter, `*(u8*)(obj+0xE6)`) — **exactly the
  previous session's per-frame list** (sub_0021AC30/0x21AC50 x534, sub_0032CB48 x267).
  This is positive identification: **the previous session was at scene 0x13 the whole
  time**, not in COpening. sub_0021BA40 (COpening's tick) never appears in any hist.
- The WARNING scene's own pad check reads words at 0x42E580+0x2CC/+0x37C (copied to
  sub+0xC0/0xC4) which are **all zero** — the headless runtime delivers no controller
  data anywhere (also 0x42E84C/0x42E8FC zero).

## Finding 12 — Answers to the three questions

**Q1 — Which scene is running / what keeps it from advancing?**
It is *not* one fixed scene: the game **self-advances** through its normal boot chain —
scene 2 WARNING (600-tick hold after a 32-tick fade-in, ~13 s) → scene 3 LOGO → scene 4
OPENING (movie skipped instantly by the ret1 stub) → and then **parks at scene 0x13
"MTTITLE D"** (title/movie-demo). At scene 0x13 the state lives in the 12-entry flow
table at 0x410290 + status bytes at 0x4BDCA8: **flow slots 6/7 remain -1 (pending)**
forever, so `sub_00326458` never requests the next scene. The scene's per-frame update
is a no-op by design (0x326298); the fade/movie plane keeps drawing untextured
full-screen quads at alpha 128 (the "black screen" signature). The white 224-line band
in the PNGs is the movie display plane with no video content in it.

**Q2 — Gap, divergence, or movie-skip consequence?**
- WARNING/LOGO/OPENING: **no bug at all** — the previous "permanent black" diagnosis was
  a misreading (it sampled scene 0x13's movie plane and attributed the per-frame
  sub_0021AC30/sub_0032CB48 pair to COpening). No recompiler divergence found; all
  examined code paths (taskmgr 0x32FCD8/0x32FD30, scene dispatch 0x357AA8/0x357250,
  WARNING state machine 0x2C15C0) run and drive the game forward.
- The scene 0x13 park is **partly the movie skip, partly an input/runtime gap**:
  (a) the movie plane renders white because the runtime has no IPU decoder and the movie
  is stubbed to "already ended" — cosmetic, a direct consequence of the skip;
  (b) the flow slots 6/7 are completed only by the input/flow handlers
  (0x29CFBC/0x29D038/0x29D0A0) and the port/memory-card handler (0x28E628 → 0x35FC60 +
  the sceMc* bindings in ufe3.toml). The pad buffer the game reads (0x42E580+0x2CC/0x37C)
  is permanently zero in this runtime — **no controller is ever reported**, so the
  "press START / button" paths can never fire. That is the same class of runtime gap as
  the IOP one from part A: the runtime's pad/sio2man emulation (in `tools/PS2Recomp`,
  off-limits here) never presents an input device.

**Q3 — Fix?**
No game-side change was made, deliberately: the game **is** following its own normal path
(boot → warning → logo → opening → title, then waiting at the title for input — exactly
what a console with no one pressing START does). Forcing the flow table forward would be
bypassing the game's own logic, not following it. What remains is not fixable game-side:
1. **Runtime fix (blocker for any interactive progress)**: implement pad input in
   `tools/PS2Recomp/ps2xRuntime` — present a virtual controller through sio2man/scePad
   so the game's pad buffer (0x42E580+0x2CC/0x37C region fed by the port handler) reports
   at least one connected pad, and inject button presses (START = 0x0800 region of the
   button mask; the WARNING screen's skip mask is 0x8F0). Even a "START pressed once at
   the title" hook would let the game reach the main menu.
2. **Runtime fix (cosmetic, movie-skip consequence)**: the movie display plane draws
   white because no decoded frames ever exist. A real fix is an IPU/MPEG stub that writes
   a black frame buffer into the movie plane, or (game-side and small) making the movie
   player's display path draw black instead of the uninitialized plane — not done here
   because it only changes the color of the placeholder, not the game's behavior.
3. Optionally (game-side, small, if the title is undesired): `work/ufe3.toml` could stub
   scene 0x13's create to request scene 6 (MTTITLE) or 0x10 (DEMO) directly — but that
  skips game content and was not done for the same reason as above.

## Finding 13 — Verification runs and PNGs (this session)

- `work/run_b1.log` (30 s, `work/shots_b1/` — shots failed: PS2X_SHOT_DIR must exist
  beforehand or the runner cannot create files in it), `run_b2.log` (30 s, `shots_b2/`),
  `run_b3/b4/b5/b6` (12-14 s peek runs), `run_b7.log` (**45 s**, `shots_b7/`),
  `run_b8.log` (20 s), `run_b9.log` (30 s).
- PNGs (`shots_b2/`, `shots_b7/`, debug UI hidden): frame_300 = the white-on-black
  Japanese health-warning text (real scene 2 content); frame_600/1500/1800 = pure black
  (capture phase alternates with the double-buffered present — only half the shots land
  on a content frame); frame_900 = dim content (mean 3.0, 127k non-black px — the
  WARNING→LOGO fade transition); frame_1200 = the LOGO/OPENING transition (horizontal
  white streaks = 2D scrolling content mid-transition); frame_2100/2400 = scene 0x13:
  black bands top/bottom + solid **white 224-line band** (the movie plane with no movie).
- Scene timeline from taskmgr+0x5FC peeks (5 s apart): 2, 2, 2(→3), 4, 0x13, 0x13, 0x13,
  0x13 — WARNING holds ~10-15 s (its 600-tick timer at ~50 ticks/s), LOGO ~3 s, OPENING
  <5 s (skipped movie), then 0x13 forever.
- No background processes left (verified: only the user's desktop kwin and the other
  agent's opencode session are running; no ufe3-headless kwin, no ps2EntryRunner).
- Nothing was rebuilt and no game file was modified in this session: the previous
  session's `game/ufe3_overrides.cpp` SOUNDMAN load bypass and `work/ufe3.toml` movie
  stubs are unchanged and are working as intended.
