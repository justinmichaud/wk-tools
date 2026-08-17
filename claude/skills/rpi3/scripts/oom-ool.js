// Reliably trigger JSObject::allocateMoreOutOfLineStorage OOM by growing LIVE
// out-of-line (butterfly) property storage until an allocation fails.
// Keys are precomputed so growth is dominated by per-object butterflies, not strings.
const PROPS = 2048;                 // ~16KB out-of-line storage/object -> PreciseAllocation path
const keys = [];
for (let j = 0; j < PROPS; j++) keys.push("p" + j);
const keep = [];
let n = 0;
while (true) {
    const o = {};
    for (let j = 0; j < PROPS; j++) o[keys[j]] = j;   // repeated allocateMoreOutOfLineStorage
    keep.push(o);                                     // retain -> live, uncollectable
    if ((++n & 511) === 0)
        print(n + " objs  ~" + ((n * PROPS * 8) / 1048576 | 0) + " MB live butterfly");
}
