#!/usr/bin/env python3
"""Convert a UFE3 'ipum' game movie into the standard Sony IPU layout that
ffmpeg's ipu demuxer/decoder accepts.

Game variant layout (all little-endian), verified on FILE2.BIN movie 0 and 1:
    0x00  'ipum'
    0x04  u32 size          (NOT the total extent on disc - informational)
    0x08  u16 width, 0x0A u16 height
    0x0C  u32 frame_count
    0x10  u32 0
    0x14  u32 frame_count   (again)
    0x18  u32 unknown       (11 for movie0, 8 for movie1 - not used here)
    0x1C  frame_count * { u32 sector, u32 byte_size }
          -> frame k payload lives at file offset 'sector * 2048',
             'byte_size' bytes long (then zero padding to the sector grid;
             next frame starts at the next recorded sector).
    0x1C + 8*frame_count  u32 end_sector  (total extent of the movie in
          2048-byte sectors, incl. header sector 0)

Standard layout ffmpeg expects:
    'ipum' + u32 total_size + u16 w + u16 h + u32 frames + raw bitstream,
    frames concatenated back-to-back.  Each frame payload ends with the IPU
    sequence marker 00 00 01 B0 (then 1..16 zero pad bytes in the game file).
    ffmpeg's ipu demuxer cuts a packet at each 00 00 01 B0, so we trim the
    trailing zero pad after the marker: one packet == one frame.

Usage:
    ipum2ffmpeg.py SRC [OFFSET] -o OUT.ipu [--keep-pad]
    SRC is any file containing the movie (e.g. disc/FILE2.BIN) or an already
    extracted raw movie region.  OFFSET is the byte offset of 'ipum' (default 0).
"""
import argparse
import struct
import sys

SECTOR = 2048
MARKER = b"\x00\x00\x01\xb0"


def parse_header(fh, off):
    fh.seek(off)
    hdr = fh.read(0x1C)
    if len(hdr) < 0x1C or hdr[:4] != b"ipum":
        raise SystemExit(f"no 'ipum' magic at offset {off:#x}")
    size, w, h, frames, _zero, frames2, _unk = struct.unpack("<IHHIIII", hdr[4:0x1C])
    if frames != frames2:
        raise SystemExit(f"frame count mismatch: {frames:#x} vs {frames2:#x}")
    return dict(size=size, w=w, h=h, frames=frames)


def read_table(fh, off, frames):
    fh.seek(off + 0x1C)
    blob = fh.read(8 * frames + 4)
    if len(blob) < 8 * frames + 4:
        raise SystemExit("truncated frame table")
    recs = [struct.unpack("<II", blob[i:i + 8]) for i in range(0, 8 * frames, 8)]
    end_sector = struct.unpack("<I", blob[8 * frames:8 * frames + 4])[0]
    return recs, end_sector


def read_frames(src, off, recs, keep_pad):
    frames = []
    with open(src, "rb") as fh:
        for k, (sec, sz) in enumerate(recs):
            fh.seek(off + sec * SECTOR)
            data = fh.read(sz)
            if len(data) != sz:
                raise SystemExit(f"frame {k}: short read at sector {sec}")
            if not keep_pad:
                i = data.rfind(MARKER)
                if i < 0:
                    print(f"warning: frame {k} has no 00 00 01 B0 marker, keeping raw",
                          file=sys.stderr)
                else:
                    data = data[:i + len(MARKER)]
            frames.append(data)
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("offset", nargs="?", type=lambda s: int(s, 0), default=0)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--keep-pad", action="store_true",
                    help="keep the zero pad after the end marker (not ffmpeg friendly)")
    ap.add_argument("--list", action="store_true", help="print per-frame info and exit")
    args = ap.parse_args()

    with open(args.src, "rb") as fh:
        hdr = parse_header(fh, args.offset)
        recs, end_sector = read_table(fh, args.offset, hdr["frames"])

    frames = read_frames(args.src, args.offset, recs, args.keep_pad)

    total_sec = 0
    for k, ((sec, sz), data) in enumerate(zip(recs, frames)):
        total_sec += sz
        if args.list:
            print(f"{k:4d} sector={sec:6d} off={args.offset + sec * SECTOR:#010x} "
                  f"size={sz:#08x} out={len(data):#08x} "
                  f"pad={sz - len(data):3d} tail={data[-8:].hex()}")
    exp_end = end_sector * SECTOR
    print(f"frames={len(frames)} table_end_sector={end_sector} "
          f"extent={exp_end:#x} raw_payload={total_sec} "
          f"(size field in header was {hdr['size']:#x})", file=sys.stderr)

    payload = b"".join(frames)
    out = b"ipum" + struct.pack("<IHHI", 0, hdr["w"], hdr["h"], hdr["frames"]) + payload
    out = out[:4] + struct.pack("<I", len(out)) + out[8:]  # size = total length
    with open(args.out, "wb") as fh:
        fh.write(out)
    print(f"wrote {args.out}: {len(out)} bytes, {len(frames)} frames "
          f"{hdr['w']}x{hdr['h']}", file=sys.stderr)


if __name__ == "__main__":
    main()
