#!/usr/bin/env python3
"""Detect samply/framehop phantom caller frames -- two adjacent physical frames (inlineDepth 0) in one stack resolving to the same function, the stale-link-register artifact around a bl into a frameless fragment. Reports the share of sampled stacks affected, and the isRope->sweep seam by name."""
import gzip, json, sys
from collections import Counter

d = json.load(gzip.open(sys.argv[1], "rt"))
sh = d["shared"]
st, ft, fu, strs = sh["stackTable"], sh["frameTable"], sh["funcTable"], sh["stringArray"]
prefix = st["prefix"]
sframe = st["frame"]
ffunc = ft["func"]
faddr = ft.get("address")
fdepth = ft.get("inlineDepth")
funame = fu["name"]

def fname(frame):
    return strs[funame[ffunc[frame]]]

def is_physical(frame):
    return (fdepth[frame] == 0) if isinstance(fdepth, list) else True

def phys_funcs(si):   # leaf->root, one entry per physical frame
    seq = []
    while si is not None:
        fr = sframe[si]
        if is_physical(fr):
            seq.append((fname(fr), faddr[fr] if isinstance(faddr, list) else None))
        si = prefix[si]
    return seq

sample_stacks = Counter()   # only stacks a sample lands on, weighted by that count
for t in d["threads"]:
    s = t["samples"]
    for si in s["stack"]:
        if si is not None:
            sample_stacks[si] += 1

total = sum(sample_stacks.values())
dup_samples = 0
isrope_sweep = 0
dup_pairs = Counter()
for si, w in sample_stacks.items():
    seq = phys_funcs(si)
    flagged = False
    for a, b in zip(seq, seq[1:]):
        if a[0] == b[0] and a[1] != b[1]:   # same func, distinct address = phantom dup
            dup_pairs[a[0]] += w
            flagged = True
        # leaf-ward isRope with sweep as its (physical) caller
        if "isRope" in a[0] and "weep" in b[0]:
            isrope_sweep += w
    if flagged:
        dup_samples += w

print(f"file: {sys.argv[1]}")
print(f"sampled stacks (weighted): {total}")
pct = 100.0*dup_samples/total if total else 0.0
print(f"stacks with adjacent same-func physical frames (phantom): {dup_samples} ({pct:.1f}%)")
print(f"isRope->sweep seam samples: {isrope_sweep} ({100.0*isrope_sweep/total if total else 0:.1f}%)")
if dup_pairs:
    print("top duplicated functions (by sample weight):")
    for name, c in dup_pairs.most_common(12):
        print(f"   {100.0*c/total:5.1f}%  {c:6d}  {name}")
