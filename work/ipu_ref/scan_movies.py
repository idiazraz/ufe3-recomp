#!/usr/bin/env python3
"""Scan disc images for 'ipum' streams and dump the movie table to movies.csv.

Validates each candidate: header sanity, frame table readable, table end sector
consistent with the payload extent (end_sector * 2048) and with the offset of
the next 'ipum' in the same file.

Usage: scan_movies.py OUT.csv
Reads ipum offsets from ipum_offsets.txt (grep -abo ipum disc/FILE*.BIN).
"""
import csv
import os
import struct
import sys

SECTOR = 2048
HERE = os.path.dirname(os.path.abspath(__file__))
OFFS = os.path.join(HERE, "ipum_offsets.txt")
DISC = os.path.abspath(os.path.join(HERE, "..", "..", "disc"))


def load_offsets():
    hits = {}
    with open(OFFS) as fh:
        for line in fh:
            path, off, _ = line.rstrip("\n").split(":", 2)
            hits.setdefault(os.path.basename(path), []).append(int(off))
    for v in hits.values():
        v.sort()
    return hits


def check(path, off):
    """Structural validation only (no cross-checks against neighbours)."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(off)
        hdr = fh.read(0x1C)
        if len(hdr) < 0x1C or hdr[:4] != b"ipum":
            return None
        sz, w, h, frames, zero, frames2, unk = struct.unpack("<IHHIIII", hdr[4:0x1C])
        rec = dict(file=os.path.basename(path), offset=off, size_field=sz, w=w, h=h,
                   frames=frames, hdr_zero=zero, frames2=frames2, field_18=unk,
                   valid="", end_sector="", extent="", next_ipum="", note="")
        try:
            assert frames == frames2 and 0 < frames < 100000, "frame count"
            assert 16 <= w <= 1920 and 16 <= h <= 1080, "dimensions"
            assert zero == 0, "u32@0x10 != 0"
            fh.seek(off + 0x1C)
            blob = fh.read(8 * frames + 4)
            assert len(blob) == 8 * frames + 4, "truncated table"
            recs = [struct.unpack("<II", blob[i:i + 8]) for i in range(0, 8 * frames, 8)]
            end_sec = struct.unpack("<I", blob[8 * frames:8 * frames + 4])[0]
            # each frame must start on its sector and fit
            assert recs[0][0] >= 1, "first sector < 1"
            assert all(r[1] > 0 for r in recs), "zero-size frame"
            assert all(recs[i][0] < recs[i + 1][0] for i in range(len(recs) - 1)), \
                "sectors not increasing"
            extent = end_sec * SECTOR
            # payload of last frame must fit before extent
            last_sec, last_sz = recs[-1]
            real_end = last_sec * SECTOR + last_sz   # true end of payload
            assert last_sec * SECTOR + last_sz <= extent, "last frame past extent"
            assert off + extent <= size, "extent past EOF"
            rec["end_sector"] = end_sec
            rec["extent"] = extent
            rec["real_end"] = real_end
            rec["valid"] = "1"
        except AssertionError as e:
            rec["valid"] = "0"
            rec["note"] = str(e)
        return rec


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "movies.csv")
    hits = load_offsets()
    rows = []
    for fn, offs in sorted(hits.items()):
        path = os.path.join(DISC, fn)
        if not os.path.exists(path):
            continue
        for off in offs:
            r = check(path, off)
            if r:
                rows.append(r)
    # cross-check: a valid movie must end (real payload end) before the next
    # VALID 'ipum' in the same file; an inner 'ipum' that fails structurally is
    # just data inside the payload, not a neighbour.
    for i, r in enumerate(rows):
        if r["valid"] != "1":
            continue
        nxt = None
        nxt_valid = None
        for q in rows[i + 1:]:
            if q["file"] != r["file"]:
                break
            if nxt is None:
                nxt = q
            if q["valid"] == "1":
                nxt_valid = q
                break
        if nxt is not None:
            r["next_ipum"] = nxt["offset"]  # may be a garbage hit inside payload
        if nxt_valid is not None and r["offset"] + r["real_end"] > nxt_valid["offset"]:
            r["valid"] = "0"
            r["note"] = "payload overlaps next valid movie"
    fields = ["file", "offset", "size_field", "w", "h", "frames", "end_sector",
              "extent", "real_end", "next_ipum", "field_18", "hdr_zero",
              "frames2", "valid", "note"]
    with open(out, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=fields)
        wr.writeheader()
        for r in rows:
            wr.writerow(r)
    nv = sum(1 for r in rows if r["valid"] == "1")
    print(f"{len(rows)} ipum candidates, {nv} valid movies -> {out}")


if __name__ == "__main__":
    main()
