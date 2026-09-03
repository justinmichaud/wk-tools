# What is resident in a macOS guest, and what memory is left, as key=value
# lines. Runs *in* the guest, over ssh, and reads only -- nothing here may kill
# anything or change a setting: `wk vm check` is a report.
#
# A guest holds its whole memory allocation whether or not it is busy
# (_check_memory_budget, targets/vm.sh), so a build in there runs out of
# whatever else stayed resident -- and the things that stay are the ones that
# keep their own process across an ssh session: an editor's remote server (each
# terminal pane in it another shell), an agent, a detached build.
#
# Raw readings only. The grouping, the sums and the thresholds are
# vm_load_findings (targets/vm.sh), where a test drives them against a captured
# sample instead of against somebody's live guest.
#
# Self-contained: it sources nothing, so it answers about a guest provisioned by
# an older base too. bash 3.2, the macOS system bash.

printf 'mem_total_mb=%s\n' "$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1048576 ))"

# macOS's own verdict on its memory, not one computed from page counts here:
# `memory_pressure` prints the free percentage the kernel acts on.
printf 'mem_free_pct=%s\n' "$(memory_pressure 2>/dev/null \
    | sed -n 's/^System-wide memory free percentage: *\([0-9]*\).*/\1/p' | tail -1)"

# Whole line, parsed on the other side:
#   total = 2048.00M  used = 512.25M  free = 1535.75M  (encrypted)
printf 'swapusage=%s\n' "$(sysctl -n vm.swapusage 2>/dev/null)"

# Every process, as resident size and the executable itself -- `comm`, not the
# command line: an argument that happens to name an editor is not one running.
# All of them, unfiltered, so a guest eaten by something this repo has never
# heard of still reports what ate it.
ps -Ao rss=,comm= 2>/dev/null | sed 's/^ *//; s/^/proc=/'
