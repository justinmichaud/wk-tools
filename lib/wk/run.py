"""`wk run`: jsc from a workspace's build tree, plain, under lldb, looped until it crashes, or recorded and replayed with rr."""
import shlex

from wk import images, places, presets, store
from wk.ldpath import LLDB_PRELUDE, prelude, rr_ready, rr_trace_dir

ROOT = images.root()

LLDB_UNTIL_CRASH = '''{lldb_prelude}
{prelude}
LOGDIR={logdir_q}
mkdir -p "$LOGDIR"
i=0
while [ "$i" -lt {max} ]; do
    i=$((i + 1))
    STFILE=$(mktemp)
    WK_UNTIL_CRASH_STATUS="$STFILE" "$LLDB" {lldb_opts} -s {runfile_q} {jsc_q} -- --validateOptions=1 {tail_q}
    STATUS=$(cat "$STFILE" 2>/dev/null || true)
    rm -f "$STFILE"
    case "$STATUS" in
        ''|0) continue ;;
    esac
    echo "until-crash: iteration $i exited $STATUS -- args: {tail_q}" >&2
    exit "$STATUS"
done
echo "until-crash: reached the cap of {max} iterations without a crash"
exit 0'''

PLAIN_UNTIL_CRASH = '''{prelude}
export JSC_validateOptions=1
LOGDIR={logdir_q}
CORE_DIR={core_dir_q}
mkdir -p "$LOGDIR"
i=0
while [ "$i" -lt {max} ]; do
    i=$((i + 1))
    STAMP=$(date -u +%Y%m%dT%H%M%SZ)-$i
    LOG="$LOGDIR/$STAMP.log"
    MARK="$LOGDIR/.mark-$STAMP"
    touch "$MARK"
    {jsc_invoke} >"$LOG" 2>&1
    STATUS=$?
    if [ "$STATUS" -eq 0 ]; then
        rm -f "$LOG" "$MARK"
        continue
    fi
    echo "until-crash: crashed on iteration $i (exit $STATUS)" >&2
    echo "until-crash: args: {tail_q}" >&2
    echo "until-crash: log: $LOG" >&2
    CORES=$(find "$CORE_DIR" -newer "$MARK" -type f 2>/dev/null || true)
    rm -f "$MARK"
    if [ -n "$CORES" ]; then
        echo "until-crash: core:" >&2
        printf '%s\\n' "$CORES" | sed 's/^/  /' >&2
    fi
    exit "$STATUS"
done
echo "until-crash: reached the cap of {max} iterations without a crash"
exit 0'''

DIRECT_LLDB = '{lldb_prelude}\n{prelude}; exec "$LLDB" {lldb_opts} -s {runfile_q} {jsc_q} -- --validateOptions=1 {tail_q}'

RR_RECORD = '{prelude}; {trace_dir}; export JSC_validateOptions=1; exec rr record {jsc_q} --validateOptions=1 {tail_q}'

RR_REPLAY = '{lldb_prelude}\n{trace_dir}; exec rr replay -d "$LLDB" {tail_q}'

DIRECT_PLAIN = '{prelude}; export JSC_validateOptions=1; exec {jsc_q} --validateOptions=1 {tail_q}'


def main(name, opts):
    reg = places.Registry(ROOT)
    driver = reg.present(name)

    preset_name = store.build_preset() or presets.default_preset(reg, name)
    preset = presets.resolve_or_die(preset_name, driver)

    src = driver.src(name)
    jsc = preset.jsc_path(src)
    var = preset.run_var()
    pre = prelude(var, preset.run_dir(src))
    tail_q = shlex.join(opts["tail"])

    jsc_q = shlex.quote(jsc)
    logdir_q = shlex.quote(driver.home() + "/until-crash") if opts["until_crash"] else ""
    if opts["lldb"]:
        runfile = driver.tools(name) + "/container/lldb/" + ("until-crash-run-file" if opts["until_crash"] else "run-file")
        lldb = dict(lldb_prelude=LLDB_PRELUDE, prelude=pre, lldb_opts=driver.lldb_opts(), runfile_q=shlex.quote(runfile),
                    jsc_q=jsc_q, tail_q=tail_q)
    if opts["rr"] or opts["replay"]:
        rr_ready(driver, name)
        if opts["replay"]:
            cmd = RR_REPLAY.format(lldb_prelude=LLDB_PRELUDE, trace_dir=rr_trace_dir(driver), tail_q=tail_q)
        else:
            cmd = RR_RECORD.format(prelude=pre, trace_dir=rr_trace_dir(driver), jsc_q=jsc_q, tail_q=tail_q)
    elif opts["until_crash"] and opts["lldb"]:
        cmd = LLDB_UNTIL_CRASH.format(logdir_q=logdir_q, max=opts["max"], **lldb)
    elif opts["until_crash"]:
        cmd = PLAIN_UNTIL_CRASH.format(
            prelude=pre, logdir_q=logdir_q, core_dir_q=shlex.quote(driver.home() + "/wk-cores"),
            max=opts["max"], jsc_invoke="%s --validateOptions=1 %s" % (jsc_q, tail_q), tail_q=tail_q)
    elif opts["lldb"]:
        cmd = DIRECT_LLDB.format(**lldb)
    else:
        cmd = DIRECT_PLAIN.format(prelude=pre, jsc_q=jsc_q, tail_q=tail_q)
    argv_, cwd = driver.exec_argv(name, ["bash", "-lc", cmd], tty=opts["lldb"] or opts["replay"])
    reg.machine.exec(argv_, cwd)
