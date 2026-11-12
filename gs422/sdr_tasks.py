"""Single-task SDR broker. Runs as the board user, separate from the serial loop."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from .sdr import KEYS, PRESETS, TERMINAL, parse_command, settings

LOG = logging.getLogger('gs422-sdr')


def save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as out:
        json.dump(value, out, indent=2)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)


class TaskError(Exception):
    pass


def result_summary(directory, tech):
    run = json.loads((directory / 'run.json').read_text())
    summary = json.loads((directory / 'analysis/summary.json').read_text())
    if run['status'] != 'completed':
        raise TaskError('DECODE_FAILED')
    cells = set()
    with (directory / 'analysis/results.jsonl').open() as stream:
        for line in stream:
            row = json.loads(line)
            if tech == 'NR' and row.get('event') == 'pbch' and row['nr'].get('crc_ok'):
                cells.add(int(row['nr']['cell_id']))
            if tech == 'LTE' and row.get('event') == 'cell' and row['lte'].get('pbch_decoded'):
                cells.add(int(row['lte']['pci']))
    if tech == 'NR':
        if summary.get('decoder_returncodes') != [0]:
            raise TaskError('DECODE_FAILED')
        pbch, mib, dci = (int(summary['events'][key]) for key in ('pbch', 'mib', 'dci'))
    else:
        # LTE reports confirmed cells, not a count of individual PBCH receptions.
        pbch, mib, dci = '-', '-', int(summary['dci_records'])
    return {'DETECTED': int(bool(cells)), 'CELLS': len(cells),
            'PCI': ';'.join(map(str, sorted(cells)[:8])) or '-',
            'PCI_MORE': int(len(cells) > 8), 'PBCH': pbch, 'MIB': mib, 'DCI': dci}


class Manager:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['output_root'])
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.thread = None
        self.blocked = False
        self.params = settings(PRESETS['NR'])
        if (self.root / 'settings.json').exists():
            stored = json.loads((self.root / 'settings.json').read_text())
            self.params = settings([stored[k] for k in KEYS])
        self.latest = None
        if (self.root / 'latest.json').exists():
            self.latest = json.loads((self.root / 'latest.json').read_text())['id']
            task = self.load(self.latest)
            if task['state'] not in TERMINAL or task.get('error') == 'CLEANUP_FAILED':
                self.blocked = not self.cleanup(task)
                task.update(state='INTERRUPTED', error='SERVICE_RESTART' if not self.blocked else 'CLEANUP_FAILED',
                            finished=time.time())
                self.store(task)

    def load(self, identity):
        if not identity or not re.fullmatch(r'[0-9a-f]{12}', identity):
            raise TaskError('NO_TASK')
        try:
            return json.loads((self.root / identity / 'task.json').read_text())
        except FileNotFoundError:
            raise TaskError('NO_TASK')

    def store(self, task):
        save(self.root / task['id'] / 'task.json', task)

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def execute(self, args):
        # The socket is local, but still revalidate every argument at this boundary.
        try:
            args = parse_command('AT+SDR=' + ','.join(args))
        except (ValueError, TypeError):
            return '+SDR:ERR,BAD_COMMAND'
        with self.lock:
            try:
                op = args[0]
                if op == 'PRESET' or (op == 'CONFIG' and len(args) > 1):
                    if self.busy():
                        raise TaskError('BUSY')
                    params = settings(PRESETS[args[1]] if op == 'PRESET' else args[1:])
                    save(self.root / 'settings.json', params)
                    self.params = params
                    return '+SDR:CONFIG,' + ','.join(f'{k}={v}' for k, v in params.items())
                if op == 'CONFIG':
                    return '+SDR:CONFIG,' + ','.join(f'{k}={v}' for k, v in self.params.items())
                if op == 'START':
                    if self.blocked:
                        raise TaskError('CLEANUP_FAILED')
                    if self.busy():
                        raise TaskError('BUSY')
                    identity = uuid.uuid4().hex[:12]
                    (self.root / identity).mkdir()
                    task = {'id': identity, 'state': 'CHECKING', 'params': dict(self.params),
                            'created': time.time(), 'error': '-', 'result': None}
                    self.store(task)
                    save(self.root / 'latest.json', {'id': identity})
                    self.latest = identity
                    self.cancel.clear()
                    self.thread = threading.Thread(target=self.run, args=(task,), daemon=True)
                    self.thread.start()
                    return f'+SDR:START,ID={identity},STATE=CHECKING'
                identity = args[1] if len(args) == 2 else self.latest
                if identity is None and op == 'STATUS':
                    return '+SDR:STATUS,ID=-,STATE=IDLE'
                task = self.load(identity)
                if op == 'STOP':
                    if task['state'] in TERMINAL:
                        return f'+SDR:STOP,ID={identity},STATE={task["state"]}'
                    if identity != self.latest:
                        raise TaskError('NOT_ACTIVE')
                    self.cancel.set()
                    task['state'] = 'STOPPING'
                    self.store(task)
                    return f'+SDR:STOP,ID={identity},STATE=STOPPING'
                if op == 'RESULT':
                    if task['state'] != 'DONE':
                        return f'+SDR:RESULT,ID={identity},STATE={task["state"]},ERROR={task["error"]}'
                    return (f'+SDR:RESULT,ID={identity},STATE=DONE,TECH={task["params"]["TECH"]},' +
                            ','.join(f'{k}={v}' for k, v in task['result'].items()))
                elapsed = max(0, int(task.get('finished', time.time()) - task['created']))
                return (f'+SDR:STATUS,ID={identity},STATE={task["state"]},TECH={task["params"]["TECH"]},'
                        f'ELAPSED_SEC={elapsed},ERROR={task["error"]}')
            except TaskError as exc:
                return '+SDR:ERR,' + str(exc)
            except (OSError, ValueError, KeyError):
                LOG.exception('task request failed')
                return '+SDR:ERR,STORAGE'

    def transition(self, task, state):
        with self.lock:
            if self.cancel.is_set():
                raise TaskError('CANCELLED')
            task['state'] = state
            self.store(task)

    def preflight(self, task):
        c = self.config
        mount = Path(c['mount'])
        if not mount.is_mount() or not self.root.resolve().is_relative_to(mount.resolve()):
            raise TaskError('SSD_NOT_MOUNTED')
        if shutil.disk_usage(self.root).free < 2 * 1024**3:
            raise TaskError('NO_SPACE')
        repo = Path(c['repo'])
        for relative in ('tools/sathex_capture.py', 'tools/sathex_decode.py', 'config/nr-n1-2130p15.json'):
            if not (repo / relative).is_file():
                raise TaskError('SOFTWARE_MISSING')
        if not Path(c['uhd_root'], 'lib/python3.10/site-packages/uhd').is_dir():
            raise TaskError('UHD_MISSING')
        if not Path('/dev/FPGA').exists():
            raise TaskError('DEVICE_MISSING')
        image = c['images'][task['params']['TECH']]
        info = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, timeout=5)
        if info.returncode:
            raise TaskError('IMAGE_MISSING')
        metadata = json.loads(info.stdout)[0]
        if metadata['Architecture'] != 'arm64':
            raise TaskError('IMAGE_ARCH')
        task['image_id'] = metadata['Id']

    def cleanup(self, task):
        """Remove only the exact container named in this task's recorded command."""
        run = self.root / task['id'] / 'decode/run.json'
        if not run.exists():
            return True
        try:
            record = json.loads(run.read_text())
            cmd = record['command']
            name = cmd[cmd.index('--name') + 1]
            if not re.fullmatch(r'sathex-decode-[0-9a-f]{12}', name):
                return False
            # Probe separately: a missing container is already cleaned up.
            probe = subprocess.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'],
                                   capture_output=True, timeout=5)
            if probe.returncode:
                return False
            if not probe.stdout.strip():
                return True
            return subprocess.run(['docker', 'rm', '-f', name], capture_output=True,
                                  timeout=10).returncode == 0
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            return False

    def process(self, task, argv, env, timeout, log):
        with log.open('wb') as out:
            proc = subprocess.Popen(argv, env=env, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            deadline = time.monotonic() + timeout
            reason = None
            try:
                while proc.poll() is None:
                    if self.cancel.wait(.1):
                        reason = 'CANCELLED'
                        break
                    if time.monotonic() > deadline:
                        reason = 'TIMEOUT'
                        break
            finally:
                if reason or proc.poll() is None:
                    # SIGINT lets the Python adapter clean up its Docker container.
                    try:
                        os.killpg(proc.pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        pass
                # Also stop capture children if the parent exited first.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
            if reason:
                raise TaskError(reason)
            if proc.returncode:
                raise TaskError('CAPTURE_FAILED' if task['state'] == 'CAPTURING' else 'DECODE_FAILED')

    def run(self, task):
        folder = self.root / task['id']
        try:
            self.preflight(task)
            p, c = task['params'], self.config
            env = os.environ.copy()
            uhd = c['uhd_root']
            env.update(PYTHONPATH=uhd + '/lib/python3.10/site-packages',
                       LD_LIBRARY_PATH=uhd + '/lib', PYTHONUNBUFFERED='1',
                       PATH=uhd + '/bin:/usr/local/bin:/usr/bin:/bin')
            tools = Path(c['repo']) / 'tools'
            self.transition(task, 'CAPTURING')
            self.process(task, ['/usr/bin/python3', str(tools / 'sathex_capture.py'),
                         '--output', str(folder / 'capture'), '--frequency', str(p['FREQ_HZ']),
                         '--rate', str(p['RATE_SPS']), '--bandwidth', str(p['BW_HZ']),
                         '--seconds', str(p['DURATION_MS'] / 1000), '--gain', str(p['GAIN_DB']),
                         '--channel', str(p['CHANNEL']), '--antenna', 'RX2'],
                         env, 75, folder / 'capture.log')
            self.transition(task, 'DECODING')
            argv = ['/usr/bin/python3', str(tools / 'sathex_decode.py'),
                    '--capture', str(folder / 'capture'), '--output', str(folder / 'decode'),
                    '--technology', p['TECH'].lower(), '--stage', p['STAGE'].lower(),
                    '--prb', str(p['PRB']), '--seconds', str(p['DURATION_MS'] / 1000),
                    '--image', task['image_id'], '--timeout', '300']
            if p['TECH'] == 'NR':
                nr = json.loads((Path(c['repo']) / 'config/nr-n1-2130p15.json').read_text())
                nr['decoder']['carrier_frequency_hz'] = p['FREQ_HZ']
                nr['decoder']['duration_seconds'] = p['DURATION_MS'] / 1000
                save(folder / 'nr-config.json', nr)
                argv += ['--config', str(folder / 'nr-config.json')]
            self.process(task, argv, env, 320, folder / 'decode.log')
            result = result_summary(folder / 'decode', p['TECH'])
            with self.lock:
                if self.cancel.is_set():
                    raise TaskError('CANCELLED')
                task.update(state='DONE', result=result)
        except TaskError as exc:
            task.update(state='CANCELLED' if str(exc) == 'CANCELLED' else 'FAILED', error=str(exc))
        except Exception:
            LOG.exception('task failed id=%s', task['id'])
            task.update(state='FAILED', error='INTERNAL')
        finally:
            if not self.cleanup(task):
                self.blocked = True
                task.update(state='FAILED', error='CLEANUP_FAILED', result=None)
            with self.lock:
                task['finished'] = time.time()
                self.store(task)

    def close(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(timeout=40)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/gs422-sdr.json')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = json.loads(Path(args.config).read_text())
    # Refuse to create task directories on eMMC if the SSD mount is absent.
    if not Path(config['mount']).is_mount():
        raise RuntimeError('SSD not mounted')
    path = Path(config['socket'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manager = Manager(config)
        stopped = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stopped.set())
        path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            path.chmod(0o660)
            server.listen(8)
            server.settimeout(.5)
            try:
                while not stopped.is_set():
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        continue
                    with conn:
                        conn.settimeout(1)
                        try:
                            data = bytearray()
                            while b'\n' not in data and len(data) <= 1024:
                                block = conn.recv(1024)
                                if not block:
                                    break
                                data.extend(block)
                            values = json.loads(data)
                            if len(data) > 1024 or not isinstance(values, list):
                                raise ValueError('request')
                            reply = manager.execute(values)
                            conn.sendall(reply.encode() + b'\n')
                        except (OSError, ValueError, TypeError):
                            try:
                                conn.sendall(b'+SDR:ERR,BAD_COMMAND\n')
                            except OSError:
                                pass
            finally:
                manager.close()
                path.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
