# HANDOFF — mine Claude transcripts for automation candidates

Not started.

## Remaining

Look through the Claude Code transcripts on a device and find places where the
agent repeated manual multi-step work that should be a deterministic tool — to
save tokens and increase reproducibility. Output: a list of candidates with the
transcript evidence, proposed as `wk` verbs or skill scripts.

Transcripts are per-machine and a sandboxed workspace cannot read another
device's, so this runs **once per device** (moose, tolken, any workspace homes
worth keeping), one session each. Findings merge into
`docs/HANDOFF-claude.md`'s worklist.
