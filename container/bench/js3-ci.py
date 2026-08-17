#!/usr/bin/env python3
"""Per-subtest and overall b/a ratio with 95% CI for JetStream3 base vs patched runs.
compare-results gives FDR significance; this gives the CI so we can check the
equivalence bound (Step 5): smallest regression ruled out = 1 - lowerCI.
b/a > 1 => patched faster; < 1 => regression (JS3 bigger-is-better).
"""
import glob, json, math, sys

def load(pattern):
    rounds = []
    for fn in sorted(glob.glob(pattern)):
        with open(fn) as f:
            d = json.load(f)
        tests = d["JetStream3.0"]["tests"]
        scores = {}
        for name, t in tests.items():
            cur = t["metrics"]["Score"]["current"]
            scores[name] = sum(cur) / len(cur)  # avg over --count samples
        rounds.append(scores)
    return rounds

def geomean(vals):
    return math.exp(sum(math.log(v) for v in vals) / len(vals))

# t critical (two-sided 95%) approx via inverse; use table fallback
def tcrit(df):
    # 95% two-sided t critical values
    table = {1:12.71,2:4.303,3:3.182,4:2.776,5:2.571,6:2.447,7:2.365,8:2.306,
             9:2.262,10:2.228,12:2.179,15:2.131,20:2.086,30:2.042,60:2.0,1000:1.96}
    keys = sorted(table)
    for k in keys:
        if df <= k:
            return table[k]
    return 1.96

def welch_log_ratio(a, b):
    """Two-sample on log scores -> ratio b/a with 95% CI (delta/log method)."""
    la = [math.log(x) for x in a]; lb = [math.log(x) for x in b]
    na, nb = len(la), len(lb)
    ma, mb = sum(la)/na, sum(lb)/nb
    va = sum((x-ma)**2 for x in la)/(na-1) if na > 1 else 0.0
    vb = sum((x-mb)**2 for x in lb)/(nb-1) if nb > 1 else 0.0
    diff = mb - ma  # log(b/a)
    se = math.sqrt(va/na + vb/nb) if (va+vb) > 0 else 0.0
    if se > 0:
        df = (va/na + vb/nb)**2 / ((va/na)**2/(na-1) + (vb/nb)**2/(nb-1))
    else:
        df = na + nb - 2
    tc = tcrit(df)
    lo, hi = diff - tc*se, diff + tc*se
    return math.exp(diff), math.exp(lo), math.exp(hi)

base = load("/tmp/js3-runs/base_*.json")
pat  = load("/tmp/js3-runs/patched_*.json")
nb, npat = len(base), len(pat)
print(f"rounds: base={nb} patched={npat}")
if nb < 2 or npat < 2:
    print("need >=2 rounds each for CI"); sys.exit(0)

NOISY = {"json-parse-inspector","doxbee-promises","Babylon","splay","tsf-wasm",
         "async-fs","first-inspector-code-load","multi-inspector-code-load"}

names = sorted(set().union(*[set(r) for r in base+pat]))
rows = []
for name in names:
    a = [r[name] for r in base if name in r]
    b = [r[name] for r in pat if name in r]
    if len(a) < 2 or len(b) < 2:
        continue
    ratio, lo, hi = welch_log_ratio(a, b)
    ruled_out = (1 - lo) * 100  # smallest regression % the CI rules out
    rows.append((name, ratio, lo, hi, ruled_out))

rows.sort(key=lambda r: r[1])
print(f"\n{'subtest':<32}{'b/a':>8}{'95%CI':>20}{'1-loCI%':>9}  flag")
for name, ratio, lo, hi, ro in rows:
    noisy = "NOISY" if name in NOISY else ""
    bound = "" if name in NOISY else ("<=0.5% OK" if ro <= 0.5 else "")
    print(f"{name:<32}{ratio:>8.4f}  [{lo:.4f},{hi:.4f}]{ro:>8.2f}  {noisy}{bound}")

# overall geomean per round
gb = [geomean(list(r.values())) for r in base]
gp = [geomean(list(r.values())) for r in pat]
ratio, lo, hi = welch_log_ratio(gb, gp)
print(f"\nOVERALL geomean b/a = {ratio:.5f}  95%CI [{lo:.5f},{hi:.5f}]")
print(f"  smallest regression ruled out (1-loCI) = {(1-lo)*100:.3f}%  (target <=0.02%)")
print(f"  patched mean geomean={sum(gp)/len(gp):.2f}  base mean geomean={sum(gb)/len(gb):.2f}")
