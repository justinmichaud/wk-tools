# A run-benchmark browser driver for a browser on another machine.
#
# Copied into the runner tree's browser_driver/ directory by `wk pi bench`
# (bench_runner_tree, lib/bench.sh), where run-benchmark's plugin loader
# finds every BrowserDriver subclass. run-benchmark then does what it always
# does -- serves the benchmark, launches the browser per iteration, waits for
# the page to POST its result, closes the browser -- and this class is only
# the two verbs that differ for a board: launch over ssh, kill over ssh.
#
# The board reaches the server through a reverse ssh tunnel `wk pi bench`
# opens (the tailnet's ACL lets a board reach nothing but boards), so the URL
# is rewritten to the loopback address and port the tunnel listens on there.
#
# Configuration arrives in the environment, never as browser_args: a
# run-benchmark option list is not the place for an ssh command line.
#
#   WK_BOARD_SSH      the ssh command, space-separated, up to and including the
#                     destination (`ssh -o BatchMode=yes -l root rpi4-bench`)
#   WK_BOARD_LAUNCH   remote shell text that starts the browser; the URL is
#                     appended as one quoted argument
#   WK_BOARD_KILL     remote shell text that ends every browser process
#   WK_BOARD_RESET    remote shell text that leaves the browser's on-disk state
#                     as a first launch finds it, run before every launch
#   WK_BOARD_URL      host:port the board reaches the server at (the tunnel)
#   WK_BOARD_EXPECT   JSON: {"process", "exe", "lib", "lib_sha256",
#                     "build_id"} -- what the process producing a result
#                     must be running. Checked while it is still alive,
#                     from /proc; a mismatch fails the run
#   WK_BOARD_EVIDENCE a file to append each check's evidence to, one JSON
#                     object per line, for the run's env.json
import json
import logging
import os
import shlex
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit

from webkitpy.benchmark_runner.browser_driver.browser_driver import BrowserDriver

_log = logging.getLogger(__name__)

# What runs on the board to prove which binary answered. POSIX sh and
# busybox tools only (pidof, ls -i, awk, readlink, sha256sum): the buildroot
# image has nothing else. Prints key=value lines; the driver judges them.
# The library is identified by the bytes the process mapped: the path whose
# inode appears in its maps, hashed on the board.
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

    # --- what run-benchmark calls -------------------------------------------------
    def prepare_initial_env(self, config):
        pass

    def prepare_env(self, config):
        # A browser left over from a run that died is a second browser
        # fetching the same URL; ended before this one starts. Then a cold
        # cache: a second launch at a URL the browser has cached loads the
        # page from disk and the benchmark never starts (measured on the rpi3
        # 2.38 image, every launch after the first of a session), and a warm
        # cache is a different measurement in any case -- run-benchmark's
        # own drivers give every launch a fresh profile for the same reason.
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

    def launch_webdriver(self, url, driver):
        raise ValueError('%s drives a browser on another machine; use --driver webserver' % self.browser_name)

    def add_additional_results(self, test_url, results):
        # The page has reported; the process that computed the number is still
        # alive, which is the only moment its identity can be read off /proc.
        if self._expect:
            self.verify_running_binary()
        return results

    def close_browsers(self):
        self._remote(self._kill, check=False)
        time.sleep(1)

    # --- the check ------------------------------------------------------------------
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

    # --- ssh ------------------------------------------------------------------------
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
    """The server's URL as the board sees it: same path and query, the
    tunnel's host:port in place of the server's own."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, hostport, parts.path, parts.query, parts.fragment))


def judge(expect, got):
    """Every way the evidence can disagree with the slot under test, as a
    list of sentences; empty means it is that slot."""
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
