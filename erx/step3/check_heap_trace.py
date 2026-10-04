#!/usr/bin/env python3
"""Verify a PS2X_TRACE_HEAP=1 run log: no guest-heap allocation may land in the
ERX module region 0x01C00000..0x01F00000 (module images live at 0x01C00000..0x01DECA6C).

Usage: python3 erx/step3/check_heap_trace.py <run-log> [--region-base 0x1C00000 --region-end 0x1F00000]

Parses lines like:
  [trace:heap] malloc_r addr=00500010 size=64 ra=003a0620
and checks every non-zero returned addr (and addr..addr+size) against the region.
Exits non-zero on any hit; prints a summary either way.
"""
import argparse
import re
import sys

LINE = re.compile(r'\[trace:heap\] (\S+) addr=([0-9a-fA-F]+) size=(\d+)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log')
    ap.add_argument('--region-base', type=lambda s: int(s, 0), default=0x01C00000)
    ap.add_argument('--region-end', type=lambda s: int(s, 0), default=0x01F00000)
    args = ap.parse_args()

    total = 0
    hits = []
    ops = {}
    with open(args.log, errors='replace') as f:
        for lineno, line in enumerate(f, 1):
            m = LINE.search(line)
            if not m:
                continue
            op, addr_s, size_s = m.groups()
            addr = int(addr_s, 16)
            size = int(size_s)
            total += 1
            ops[op] = ops.get(op, 0) + 1
            if addr == 0:
                continue
            end = addr + max(size, 1)
            if addr < args.region_end and end > args.region_base:
                hits.append((lineno, op, addr, size))

    print(f'heap ops seen: {total} {ops}')
    if hits:
        print(f'FAIL: {len(hits)} allocation(s) inside '
              f'{args.region_base:#x}..{args.region_end:#x}:')
        for lineno, op, addr, size in hits[:20]:
            print(f'  line {lineno}: {op} addr={addr:#010x} size={size}')
        sys.exit(1)
    print(f'OK: no allocation in {args.region_base:#x}..{args.region_end:#x}')


if __name__ == '__main__':
    main()
