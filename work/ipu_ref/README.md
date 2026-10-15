# UFE3 IPU reference-frame ground truth (work/ipu_ref)

Goal: extract ground-truth frames for the game's `ipum` movies with the installed
ffmpeg, to validate the IPU decoder being ported.

## Step 1: header/table dump of movie0.ipu

`work/ipu_ref/movie0.ipu` (first movie, FILE2.BIN offset 0):

```
0x00  'ipum'
0x04  u32 size = 0x13EA26   (NOT the total extent - see Step 3)
0x08  u16 width  = 320
0x0A  u16 height = 240
0x0C  u32 frames = 0x83 (131)
0x10  u32 = 0
0x14  u32 = 0x83 (frame count again)
0x18  u32 = 0x0B (11)        [meaning unknown - constant in movie2 too?]
0x1C.. frame table, 131 x {u32 sector_start, u32 byte_size}
```

Table read as u32 pairs from 0x18: `(size_i, end_sector_i)` for i = 0..131 (132
pairs, i.e. frames+1). Pair 0 is `(11, 1)`; pairs 1..131 are the real frames:
frame k (1-based) starts at byte `pair[k-1].end_sector * 2048` and is
`pair[k].size` bytes long. `pair[131].end_sector = 697` = total end sector of
the movie (0x15C800).

The C column (end_sector) obeys exactly `C_i = C_{i-1} + size_i//2048 + 1`
(all 132 entries), i.e. every frame is padded out to a 2048-byte sector boundary
plus one extra sector when size is an exact multiple of 2048.

## Step 2: how ffmpeg's ipu demuxer/decoder behave

- `ffprobe -f ipu -i movie0.ipu` parses the 16-byte header fine (pos=16 after
  read_header) - so the standard `ipum + size + w + h + frames` header is accepted.
- Packets are delimited by the `00 00 01 B0` marker that ends every frame:
  with the raw game file packet 0 = bytes [0x10, 0x138E) which ends exactly after
  frame 1's trailing `00 00 01 B0` at 0x138A. In other words the demuxer reads
  sequentially and cuts a packet at each `00 00 01 B0`.
- The decoder rejects everything on the game file because packets contain the
  sector padding/table junk between frames ("Invalid data found when processing
  input", decode error rate exceeded).

## Step 3: movie0.ipu was truncated; frames are sector-aligned in FILE2.BIN

- Walking the table from sector 1 (0x800) matches the actual data: every frame k
  starts exactly at `C_{k-1}*2048`, size `A_k` bytes, then zero padding.
- Frame starts verified against FILE2.BIN for all 131 frames; 106/131 tails
  contain the `00 00 01 B0` end marker in the last ~17 bytes.
- The size field 0x13EA26 cuts the movie mid-way (~frame 124); the real payload
  ends at 0x15C800 (697 sectors), where the next asset begins. So the extracted
  `movie0.ipu` is missing the last 7 frames -> re-extract from FILE2.BIN.
- Next `ipum` in FILE2.BIN starts at 0x1A7000.

## Step 4: the converter - work/ipu_ref/ipum2ffmpeg.py

```
python3 ipum2ffmpeg.py SRC [OFFSET] -o OUT.ipu [--keep-pad] [--list]
# e.g. full movie 0 (the correct source is the disc image, NOT movie0.ipu):
python3 ipum2ffmpeg.py ../../disc/FILE2.BIN 0 -o movie0_std.ipu
```

What it does:
1. Parses the game header (magic, size, w, h, frame count, the extra words at
   0x10/0x14/0x18) and the frame table at `0x1C = frame_count x {u32 sector,
   u32 byte_size}`; a trailing u32 at `0x1C + 8*frame_count` is the movie's end
   sector (cross-checked against the last frame).
2. For each frame reads `sector*2048 .. +byte_size` from the disc image - the
   table is the only place frames are located (they are zero-padded out to the
   2048-byte grid, so contiguous concatenation does NOT work).
3. Trims the 1..16 zero pad bytes that follow each frame's `00 00 01 B0`
   end-of-frame marker. This is the crucial bit: ffmpeg's ipu demuxer reads the
   16-byte header, then splits packets sequentially at every `00 00 01 B0`.
   With the pad left in place, every packet after the first would begin with
   1..16 stray zero bytes (and in the raw game file packet 0 contained the
   whole frame table) - which is exactly why the decoder said "Invalid data
   found when processing input" for every packet.
4. Emits the standard Sony layout: `'ipum' + u32 total_size + u16 w + u16 h +
   u32 frames + <frames back-to-back>`.

Result: `ffprobe movie0_std.ipu` -> 131 frames, and
`ffmpeg -f ipu -i movie0_std.ipu ...` decodes all 131 with no warnings.

`--keep-pad` disables step 3 (debugging), `--list` prints per-frame
sector/offset/size/marker info.

## Step 5: reference frames produced

- `ref_movie0/frame_000.png .. frame_130.png` - all 131 frames of the first
  movie (FILE2.BIN offset 0, 320x240). Regenerate with:
  ```
  ffmpeg -f ipu -i movie0_std.ipu -fps_mode passthrough -start_number 0 \
         ref_movie0/frame_%03d.png
  ```
  (`-fps_mode passthrough` + `-start_number 0` matter: no frame is dropped or
  duplicated, and `frame_000` == game frame 0 == demuxer packet 0.)
- `ref_movie1/frame_000.png .. frame_103.png` - second reference, FILE2.BIN
  offset 0x1A7000 (104 frames, 320x240), via `movie1_std.ipu`.
- NOTE: the older `movie0.ipu` in this directory is **truncated** (it was cut
  at the header's size field 0x13EA26, which is not the extent - the movie
  really runs to 0x15C800 and holds 7 more frames). Use `movie0_std.ipu`.

## Step 6: movies.csv - every ipum stream

`scan_movies.py` (reads `ipum_offsets.txt` = `grep -abo ipum disc/FILE*.BIN`)
writes `movies.csv` with file, offset, size_field, w, h, frames, end_sector,
extent, real_end, next_ipum, the unknown 0x18 field, validity + reject reason.

- 170 raw `ipum` hits: FILE1=2, FILE2=135, FILE3=33.
- 164 valid movies: FILE2=131 (2x 320x240, 129x 320x224, 81144 frames total),
  FILE3=33 (30x 640x448, 3x 320x224, 48834 frames), FILE1=0.
- 6 rejects: 4 are `ipum` byte patterns inside other data (frame count field
  nonsense, frames != frames2); 2 (FILE2 81573888 and 403535872) are
  constant-frame-size filler streams whose LAST table entry points at the very
  next movie's header (their frame 383/1135 is the next `ipum` verbatim), so
  they overlap their neighbour by one frame and are excluded.
- Validation performed per row: magic, frames==frames2 at 0x14, u32@0x10==0,
  sane w/h, full table readable, sectors strictly increasing, last frame inside
  the table's end sector, payload end <= next valid movie / EOF.

Field 0x18 (u32 after 0x14) is a small per-movie integer (3..32 across the set)
of unknown meaning; it is not needed to locate frames - table records from 0x1C
work directly.

## Step 7: how the decoder agent should compare

1. **Index alignment**: `frame_NNN.png` is game frame NNN (0-based). ffmpeg
   reports 25 fps / 1 reference frame, but timing is nominal - compare by index,
   never by timestamp. All frames are intra; there is no reordering.
2. **Pixel formats**: ffmpeg's ipu decoder outputs `yuv420p` (4:2:0 planar
   YCbCr, colour range/space `unspecified`); the PNGs are `rgb24` full-range
   (swscale converts on export). The game's IPU does the same YCbCr 4:2:0
   decode and its IDEC then converts to RGB32 in hardware. Two ways to compare:
   - *Preferred (bit-exact-ish)*: if your decoder exposes the IPU's YCbCr 4:2:0
     output, dump it as I420 and compare with ffmpeg's raw planar output:
     ```
     ffmpeg -f ipu -i movie0_std.ipu -pix_fmt yuv420p -f rawvideo yuv.raw
     # yuv.raw = N frames of W*H bytes Y, then W*H/4 U, then W*H/4 V
     ```
   - *RGB path (what the game displays)*: dump your RGB32 frames as raw RGB24
     and convert ffmpeg's side with the same tool, e.g.
     ```
     ffmpeg -f ipu -i movie0_std.ipu -pix_fmt rgb24 -f rawvideo rgb.raw
     ```
     PSNR in pure python, per frame:
     ```python
     import math
     W,H,N=320,240,131; FS=W*H*3
     a=open('rgb.raw','rb').read(); b=open('mine.raw','rb').read()
     for k in range(N):
         x=a[k*FS:(k+1)*FS]; y=b[k*FS:(k+1)*FS]
         mse=sum((p-q)**2 for p,q in zip(x,y))/FS
         print(k, 'inf' if mse==0 else 10*math.log10(255*255/mse))
     ```
3. **Colour caveats**: ffmpeg leaves the stream colourspace unspecified, so the
   YUV->RGB conversion assumes SD/BT.601 limited range; the IPU's internal
   YCbCr->RGB has its own fixed-point rounding. Expect small per-pixel
   differences (a few LSB) and PSNR in the 40-50 dB range rather than exact
   equality on the RGB path; the YUV path should be near bit-exact. If your
   output looks uniformly brighter, you have a tv/pc range mismatch.
4. **Frame byte layout**: each frame packet starts with the 32-bit picture
   header word (movie0 frame0 = `80 42 a9 4a`; the game reads it with FDEC
   fb=0/fb=8) and ends with `00 00 01 B0`; ffmpeg ends the packet exactly
   there (the converter has already removed the trailing zero pad). Everything
   after `00 00 01 B0` must not be fed to the bitstream reader.
5. **Debugging aid**: `ipum2ffmpeg.py --list SRC OFFSET` prints every frame's
   sector, file offset, size, trimmed size and last 4 bytes, so a single
   mismatched frame in your decoder can be re-extracted and decoded alone:
   `ffmpeg -f ipu -i one_frame.ipu -f rawvideo -` .

