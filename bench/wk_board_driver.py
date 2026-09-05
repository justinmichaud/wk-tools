# A run-benchmark BrowserDriver for a browser on another machine: `wk pi bench` copies it into the runner tree and sets WK_BOARD_*.
# The URL is rewritten to the reverse ssh tunnel it opens, because the tailnet's ACL lets a board reach nothing but boards.
import json
import logging
import os
import shlex
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit

from webkitpy.benchmark_runner.browser_driver.browser_driver import BrowserDriver

_log = logging.getLogger(__name__)

PROFILE_DIR = '/tmp/wk-prof'

# POSIX sh and busybox tools only: the buildroot image has nothing else.
_VERIFY_SH = r'''
p=$(pidof %(process)s | tr ' ' '\n' | head -1)
[ -n "$p" ] || { echo "pids=0"; exit 0; }
echo "pids=$(pidof %(process)s | wc -w)"
echo "exe=$(readlink /proc/$p/exe)"
want=$(ls -i %(lib)s 2>/dev/null | awk '{print $1}')
echo "lib_inode=$want"
echo "mapped=$(awk -v ino="$want" '$5 == ino { print $6; exit }' /proc/$p/maps)"
echo "other_webkit=$(awk '$6 ~ /libWPEWebKit|libjavascriptcore|libwebkit2/ { print $6 }' /proc/$p/maps | sort -u | grep -v "^%(lib)s$" | tr '\n' ' ')"
echo "lib_sha256=$(sha256sum %(lib)s 2>/dev/null | awk '{print $1}')"
'''


# The live web process is the only place these are readable. Raw lines out, arithmetic in Python.
_WARMUP_SH = r'''
p=$(pidof %(process)s | tr ' ' '\n' | head -1)
[ -n "$p" ] || { echo "pid=0"; exit 0; }
echo "pid=$p"
echo "exe=$(readlink /proc/$p/exe)"
echo "elf=$(od -An -tu1 -N20 /proc/$p/exe | tr -s ' ' | tr -d '\n')"
echo "dri=$(awk '$6 ~ /_dri\.so$/ { print $6 }' /proc/$p/maps | sort -u | tr '\n' ' ')"
echo "gl=$(awk '$6 ~ /lib(EGL|GLESv2|gbm)\./ { print $6 }' /proc/$p/maps | sort -u | tr '\n' ' ')"
for f in /proc/$p/fd/*; do
    t=$(readlink "$f" 2>/dev/null)
    case "$t" in /dev/dri/*) echo "drifd=$t" ;; esac
done
awk '$2 ~ /x/ && $5 == "0" && NF == 5 { print "execmap=" $1 }' /proc/$p/maps
'''


_GPU_SH = r'''
for d in /proc/[0-9]*; do
    p=${d#/proc/}
    for f in $d/fd/*; do
        case "$(readlink "$f" 2>/dev/null)" in /dev/dri/*) ;; *) continue ;; esac
        n=${f##*/}
        [ -r "$d/fdinfo/$n" ] || continue
        awk -v pid="$p" -v comm="$(cat $d/comm 2>/dev/null)" \
            '/^drm-engine-/ { sub(/^drm-engine-/,"",$1); sub(/:$/,"",$1);
                              print "gpu=" comm " " pid " " $1 " " $2 }
             /^drm-driver/  { print "gpudrv=" $2 }' "$d/fdinfo/$n"
    done
done
'''

# JSC prints one line per optimizing compile under JSC_report*CompileTimes:
#   Optimized <name> using <mode> with <FTL|DFG|Baseline> into <n> bytes in <t> ms.
_TIERS_SH = r'''
for t in FTL DFG Baseline; do
    echo "tier=$t $(grep -c "with $t into" /tmp/wk-browser.log 2>/dev/null || echo 0)"
done
'''


_PROFILE_START_SH = r'''
n=0
while [ "$n" -lt 60 ]; do
    p=$(pidof %(process)s | tr ' ' '\n' | head -1)
    [ -n "$p" ] && break
    n=$((n + 1)); sleep 1
done
[ -n "$p" ] || { echo "no %(process)s appeared within 60s" >&2; exit 1; }
rm -f %(out)s %(out)s.log
case %(tool)s in
    samply)  %(dir)s/samply record --save-only -o %(out)s -p "$p" </dev/null >%(out)s.log 2>&1 & ;;
    sysprof) sysprof-cli --force --pid "$p" %(out)s </dev/null >%(out)s.log 2>&1 & ;;
    *) echo "no such profiler: %(tool)s" >&2; exit 1 ;;
esac
echo $! > %(out)s.pid
sleep 3
kill -0 "$(cat %(out)s.pid)" 2>/dev/null || {
    echo "%(tool)s died immediately:" >&2; sed "s/^/  /" %(out)s.log >&2; exit 1; }
echo "attached=%(tool)s pid=$p"
'''

_PROFILE_STOP_SH = r'''
[ -f %(out)s.pid ] || { echo "nothing was started for %(out)s" >&2; exit 1; }
p=$(cat %(out)s.pid)
kill -INT "$p" 2>/dev/null || true
n=0
while [ "$n" -lt 60 ] && kill -0 "$p" 2>/dev/null; do n=$((n + 1)); sleep 1; done
kill -9 "$p" 2>/dev/null || true
rm -f %(out)s.pid
[ -s %(out)s ] || { echo "no capture at %(out)s:" >&2; sed "s/^/  /" %(out)s.log >&2; exit 1; }
echo "capture=%(out)s bytes=$(wc -c < %(out)s | tr -d " ")"
'''


class WkBoardDriver(BrowserDriver):
    browser_name = 'wk-board'
    platform = 'linux'
    process_search_list = []

    def __init__(self, browser_args):
        super().__init__(browser_args)
        self._ssh = shlex.split(_need('WK_BOARD_SSH'))
        self._launch = _need('WK_BOARD_LAUNCH')
        self._kill = _need('WK_BOARD_KILL')
        self._reset = _need('WK_BOARD_RESET')
        self._url = _need('WK_BOARD_URL')
        expect = os.environ.get('WK_BOARD_EXPECT', '')
        self._expect = json.loads(expect) if expect else None
        self._evidence = os.environ.get('WK_BOARD_EVIDENCE', '')
        self._warmup = os.environ.get('WK_BOARD_WARMUP', '')
        self._profile = os.environ.get('WK_BOARD_PROFILE', '')
        self._gpu_before = ''
        self._class = os.environ.get('WK_BOARD_CLASS', 'gpu')

    def prepare_initial_env(self, config):
        pass

    def prepare_env(self, config):
        # A launch at a cached URL never starts the benchmark (measured, rpi3 2.38).
        self._remote(self._kill, check=False)
        self._remote(self._reset)

    def restore_env(self):
        pass

    def restore_env_after_all_testing(self):
        self._remote(self._kill, check=False)

    def browser_version(self):
        return None

    def launch_url(self, url, options, browser_build_path=None, browser_path=None):
        url = rewrite_url(url, self._url)
        _log.info('launching on the board: %s' % url)
        cmd = 'nohup sh -c %s </dev/null >/tmp/wk-browser.log 2>&1 &' % shlex.quote(
            self._launch + ' ' + shlex.quote(url))
        self._remote(cmd)
        if self._profile:
            self.start_profiler()
        if self._warmup:
            self._gpu_before = self._remote(_GPU_SH, capture=True, check=False)

    def launch_webdriver(self, url, driver):
        raise ValueError('%s drives a browser on another machine; use --driver webserver' % self.browser_name)

    def add_additional_results(self, test_url, results):
        # Still alive is the only moment its identity can be read off /proc.
        if self._expect:
            self.verify_running_binary()
        if self._warmup:
            self.record_warmup_evidence()
        return results

    def close_browsers(self):
        if self._profile:
            self.stop_profiler()
        self._remote(self._kill, check=False)
        time.sleep(1)

    def verify_running_binary(self):
        out = self._remote(_VERIFY_SH % self._expect, capture=True)
        got = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
        problems = judge(self._expect, got)
        record = {'when': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  'expect': self._expect, 'got': got, 'ok': not problems}
        if self._evidence:
            with open(self._evidence, 'a') as f:
                f.write(json.dumps(record) + '\n')
        if problems:
            raise RuntimeError('the process that produced this result is not the slot '
                               'under test:\n  ' + '\n  '.join(problems))
        _log.info('verified: %s is %s (build-id %s)' % (
            self._expect['process'], got.get('exe'), self._expect.get('build_id')))

    def _profile_fields(self):
        tool, _, out = self._profile.partition(':')
        return {'tool': shlex.quote(tool), 'out': shlex.quote(out), 'dir': PROFILE_DIR,
                'process': self._expect['process']}

    def start_profiler(self):
        got = self._remote(_PROFILE_START_SH % self._profile_fields(), capture=True)
        _log.info('profiler: %s' % got.strip())

    def stop_profiler(self):
        got = self._remote(_PROFILE_STOP_SH % self._profile_fields(),
                           capture=True, check=False)
        _log.info('profiler: %s' % got.strip())

    def record_warmup_evidence(self):
        probed = self._remote(_WARMUP_SH % self._expect, capture=True)
        record = warmup_record(probed)
        record['gpu'] = gpu_delta(self._gpu_before,
                                  self._remote(_GPU_SH, capture=True, check=False))
        record['jit']['tiers'] = tier_counts(self._remote(_TIERS_SH, capture=True, check=False))
        record['when'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        record['expect'] = self._expect
        record['class'] = self._class
        record['problems'] = warmup_problems(record)
        if self._profile:
            tool, _, capture = self._profile.partition(':')
            record['profile'] = {'tool': tool, 'file': os.path.basename(capture)}
        with open(self._warmup, 'w') as f:
            f.write(json.dumps(record, indent=2, sort_keys=True) + '\n')
        _log.info('warmup: %d-bit %s, renderer %s, gpu %d ms, tiers %s, %s' % (
            record['elf']['bits'], record['elf']['machine'],
            record['gl']['driver'] or 'unknown', record['gpu']['busy_ms'],
            record['jit']['tiers'], record['jit']['verdict']))

    def _remote(self, text, check=True, capture=False):
        cp = subprocess.run(self._ssh + [text], stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, text=True)
        if check and cp.returncode != 0:
            raise RuntimeError('board command failed (%d): %s\n%s' % (cp.returncode, text, cp.stderr or ''))
        return cp.stdout if capture else ''


def _need(name):
    value = os.environ.get(name, '')
    if not value:
        raise ValueError('%s is not set; the wk-board browser is driven by wk pi bench' % name)
    return value


def rewrite_url(url, hostport):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, hostport, parts.path, parts.query, parts.fragment))


def judge(expect, got):
    problems = []
    if got.get('pids', '0') == '0':
        problems.append('no %s process is running on the board' % expect['process'])
        return problems
    if got.get('exe') != expect['exe']:
        problems.append('%s is %s, not the slot\'s %s' % (expect['process'], got.get('exe'), expect['exe']))
    if got.get('mapped') != expect['lib']:
        problems.append('the process has not mapped %s (mapped: %s)' % (expect['lib'], got.get('mapped') or 'nothing with that inode'))
    if got.get('other_webkit', '').strip():
        problems.append('the process also maps another WebKit: %s' % got['other_webkit'].strip())
    if got.get('lib_sha256') != expect['lib_sha256']:
        problems.append('the mapped library is not the slot\'s bytes (sha256 %s, expected %s)' % (got.get('lib_sha256'), expect['lib_sha256']))
    return problems


_SOFTWARE_DRI = ('swrast', 'kms_swrast', 'llvmpipe', 'softpipe', 'lavapipe')

_ELF_MACHINES = {3: 'x86', 40: 'ARM', 62: 'x86-64', 183: 'AArch64'}


def warmup_record(out):
    kv = {}
    execmaps = []
    drifds = []
    for line in out.splitlines():
        key, sep, value = line.partition('=')
        if not sep:
            continue
        if key == 'execmap':
            execmaps.append(value)
        elif key == 'drifd':
            drifds.append(value)
        else:
            kv[key] = value

    header = [int(x) for x in kv.get('elf', '').split() if x.isdigit()]
    bits = {1: 32, 2: 64}.get(header[4] if len(header) > 4 else 0, 0)
    machine = _ELF_MACHINES.get(
        (header[19] << 8 | header[18]) if len(header) >= 20 else 0, 'unknown')

    dri = [d for d in kv.get('dri', '').split() if d]
    driver = dri[0] if len(dri) == 1 else (' '.join(dri) if dri else '')
    software = bool(dri) and all(
        any(s in os.path.basename(d) for s in _SOFTWARE_DRI) for d in dri)

    exec_bytes = 0
    for span in execmaps:
        lo, _, hi = span.partition('-')
        try:
            exec_bytes += int(hi, 16) - int(lo, 16)
        except ValueError:
            pass

    return {
        'process': {'pid': kv.get('pid', '0'), 'exe': kv.get('exe', '')},
        'elf': {'bits': bits, 'machine': machine},
        'gl': {'driver': driver, 'software': software,
               'mapped': dri, 'libs': [g for g in kv.get('gl', '').split() if g],
               'render_nodes': sorted(set(drifds))},
        'jit': {'exec_mappings': len(execmaps), 'exec_bytes': exec_bytes,
                'verdict': 'JIT active' if execmaps else 'no executable mapping -- interpreter only'},
    }


def warmup_problems(record):
    problems = []
    gl = record.get('gl', {})
    if not gl.get('mapped'):
        problems.append('the web process mapped no DRI driver at all -- '
                        'the GL path it used cannot be identified')
    elif gl.get('software'):
        problems.append('the web process rendered through %s, a software rasterizer'
                        % gl.get('driver'))
    if not record.get('jit', {}).get('exec_mappings'):
        problems.append('the web process took no executable memory -- nothing was JITted')
    if not record.get('elf', {}).get('bits'):
        problems.append('the width of the measured process could not be read')

    gpu = record.get('gpu') or {}
    if record.get('class', 'gpu') == 'gpu':
        hardware_path = bool(gl.get('render_nodes')) and bool(gl.get('mapped')) \
            and not gl.get('software')
        if gpu.get('measured') and not gpu.get('busy_ms'):
            problems.append('the GPU was billed no engine time for the whole run (driver %s) '
                            '-- the frames came off the CPU'
                            % (gpu.get('driver') or 'unknown'))
        elif not gpu.get('measured'):
            if hardware_path:
                record.setdefault('notes', []).append(
                    'engine time is unreadable on this driver (no fdinfo drm-engine-* '
                    'counters); the hardware path is still evidenced by %s on %s'
                    % (os.path.basename(gl["mapped"][0]), ", ".join(gl["render_nodes"])))
            else:
                problems.append('no DRM engine counters and no render node held by the '
                                'web process -- nothing evidences a GPU path at all')

    tiers = (record.get('jit') or {}).get('tiers') or {}
    want = 'FTL' if record.get('elf', {}).get('bits') == 64 else 'DFG'
    if not tiers:
        problems.append('no JSC compile-time report reached the browser log, so no tier '
                        'can be confirmed')
    elif not tiers.get(want):
        problems.append('a %d-bit build reached no %s compilation (%s) -- the score is not '
                        'this engine at its top tier'
                        % (record['elf']['bits'], want,
                           ', '.join('%s=%d' % kv for kv in sorted(tiers.items()))))
    return problems


def gpu_delta(before, after):
    """Engine nanoseconds each process billed to the GPU across the run."""
    def read(text):
        out, driver = {}, ''
        for line in (text or '').splitlines():
            key, sep, value = line.partition('=')
            if key == 'gpudrv' and sep:
                driver = value.strip()
            elif key == 'gpu' and sep:
                parts = value.split()
                if len(parts) == 4 and parts[3].isdigit():
                    out[(parts[0], parts[1], parts[2])] = int(parts[3])
        return out, driver

    a, _ = read(before)
    b, driver = read(after)
    per_process, total = {}, 0
    for key, end in b.items():
        spent = end - a.get(key, 0)
        if spent <= 0:
            continue
        per_process[key[0]] = per_process.get(key[0], 0) + spent
        total += spent
    return {'driver': driver, 'busy_ms': total // 1000000,
            'by_process_ms': {k: v // 1000000 for k, v in sorted(
                per_process.items(), key=lambda kv: -kv[1])},
            'measured': bool(b)}


def tier_counts(text):
    out = {}
    for line in (text or '').splitlines():
        key, sep, value = line.partition('=')
        if key == 'tier' and sep:
            parts = value.split()
            if len(parts) == 2 and parts[1].isdigit():
                out[parts[0]] = int(parts[1])
    return out
