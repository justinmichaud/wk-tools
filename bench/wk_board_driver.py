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

    def launch_webdriver(self, url, driver):
        raise ValueError('%s drives a browser on another machine; use --driver webserver' % self.browser_name)

    def add_additional_results(self, test_url, results):
        # Still alive is the only moment its identity can be read off /proc.
        if self._expect:
            self.verify_running_binary()
        return results

    def close_browsers(self):
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
