"""A run-benchmark BrowserDriver for a browser on a board: lib/wk/bench/board.py copies it into the runner tree as
wk_board_driver.py and sets WK_BOARD_*. It runs here, beside run-benchmark, and reaches the board over wk.machine's Ssh; the URL is
rewritten to the board's own end of the forward the run holds, since the tailnet lets a board reach nothing but boards."""
import json
import logging
import os
import shlex
import sys
import time
from urllib.parse import urlsplit, urlunsplit

from webkitpy.benchmark_runner.browser_driver.browser_driver import BrowserDriver

_log = logging.getLogger(__name__)

PROFILE_DIR = '/tmp/wk-prof'


class WkBoardDriver(BrowserDriver):
    browser_name = 'wk-board'
    platform = 'linux'
    process_search_list = []

    def __init__(self, browser_args):
        super().__init__(browser_args)
        sys.path.insert(0, _need('WK_BOARD_LIB'))
        from wk.machine import Local, Ssh
        from wk.bench.board import Script
        tree = os.path.dirname(_need('WK_BOARD_LIB'))
        self._script = lambda name, **p: Script(tree, name, **p).text()   # bench/onboard/, beside the library copy
        self._here = Local()
        self._board = Ssh(_need('WK_BOARD_DEST'), json.loads(_need('WK_BOARD_OPTS')))
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
        self._jit_tiers = bool(os.environ.get('WK_BOARD_JIT_TIERS', ''))

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
            self._profiler('bench-profile-start.sh', True)
        if self._warmup:
            self._gpu_before = self._remote(self._script('bench-gpu.sh'), capture=True, check=False)

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
            self._profiler('bench-profile-stop.sh', False)
        self._remote(self._kill, check=False)
        time.sleep(1)

    # A profile is written on the board, one file per process at LLVM_PROFILE_FILE
    # (lib/wk/pgo.py), so the two upstream hooks below are a clear over ssh and a pull.
    def _pgo_dir(self):
        return _need('WK_BOARD_PGO')

    def prepare_pgo_profile_collection(self):
        directory = self._pgo_dir()
        self._remote('rm -rf %s && mkdir -p %s' % (shlex.quote(directory), shlex.quote(directory)))

    def collect_pgo_profile(self, destination):
        directory = self._pgo_dir()
        try:
            names = self._board.listdir(directory)
            files = {n: self._board.read_bytes(directory + '/' + n) for n in names
                     if not self._board.isdir(directory + '/' + n)}
        except OSError as e:
            raise RuntimeError('nothing came back from %s on the board (%s): an instrumented '
                               'build writes a .profraw per process as it exits, so a run '
                               'that wrote none either was not instrumented or never '
                               'started the browser' % (directory, e))
        for n, data in files.items():
            self._here.write_own(os.path.join(destination, n), data)
        pulled = [n for n in files if n.endswith('.profraw')]
        if not pulled:
            raise RuntimeError('%s held no .profraw, only %s' % (directory, names))
        _log.info('pgo: pulled %d profile(s) from %s' % (len(pulled), directory))

    def verify_running_binary(self):
        out = self._remote(self._script('bench-verify.sh', WK_PROCESS=self._expect['process'], WK_LIB=self._expect['lib']), capture=True)
        got = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
        problems = judge(self._expect, got)
        record = {'when': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  'expect': self._expect, 'got': got, 'ok': not problems}
        if self._evidence:
            before = self._here.read(self._evidence) if self._here.exists(self._evidence) else ''
            self._here.write_own(self._evidence, before + json.dumps(record) + '\n')
        if problems:
            raise RuntimeError('the process that produced this result is not the slot '
                               'under test:\n  ' + '\n  '.join(problems))
        _log.info('verified: %s is %s (build-id %s)' % (
            self._expect['process'], got.get('exe'), self._expect.get('build_id')))

    def _profiler(self, name, check):
        tool, _, out = self._profile.partition(':')
        text = self._script(name, WK_TOOL=tool, WK_OUT=out, WK_DIR=PROFILE_DIR, WK_PROCESS=self._expect['process'])
        _log.info('profiler: %s' % self._remote(text, capture=True, check=check).strip())

    def record_warmup_evidence(self):
        record = warmup_record(self._remote(self._script('bench-warmup.sh', WK_PROCESS=self._expect['process']), capture=True))
        record['gpu'] = gpu_delta(self._gpu_before, self._remote(self._script('bench-gpu.sh'), capture=True, check=False))
        record['jit']['tiers'] = tier_counts(
            self._remote(self._script('bench-tiers.sh'), capture=True, check=False)) if self._jit_tiers else None
        record.update(when=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), expect=self._expect, **{'class': self._class})
        if self._profile:
            tool, _, capture = self._profile.partition(':')
            record['profile'] = {'tool': tool, 'file': os.path.basename(capture)}
        self._here.write_own(self._warmup, json.dumps(record, indent=2, sort_keys=True) + '\n')
        _log.info('warmup: %d-bit %s, renderer %s, gpu %d ms, tiers %s, %s' % (
            record['elf']['bits'], record['elf']['machine'], record['gl']['driver'] or 'unknown', record['gpu']['busy_ms'],
            record['jit']['tiers'], record['jit']['verdict']))

    def _remote(self, text, check=True, capture=False):
        r = self._board.act_run(['sh', '-c', text])
        if not capture:
            sys.stdout.write(r.out)
            sys.stderr.write(r.err)
        if check and not r.ok:
            raise RuntimeError('board command failed (%d): %s\n%s' % (r.rc, text, r.err))
        return r.out if capture else ''


def _need(name):
    value = os.environ.get(name, '')
    if not value:
        raise ValueError('%s is not set; the wk-board browser is driven by wk bench run --system <board>' % name)
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

    from wk.bench.board import elf
    bits, machine = elf(kv.get('elf', ''))

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
        'elf': {'bits': bits, 'machine': machine or 'unknown'},
        'gl': {'driver': driver, 'software': software,
               'mapped': dri, 'libs': [g for g in kv.get('gl', '').split() if g],
               'render_nodes': sorted(set(drifds))},
        'jit': {'exec_mappings': len(execmaps), 'exec_bytes': exec_bytes,
                'verdict': 'JIT active' if execmaps else 'no executable mapping -- interpreter only'},
    }


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
