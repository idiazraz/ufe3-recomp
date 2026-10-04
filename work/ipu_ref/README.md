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
