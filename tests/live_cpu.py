"""Explicit PTY test of real CPU telemetry, including a brief single-core load.

Does not open the physical UART or change governors. --installed tests the DEB.
"""
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='gs422-cpu-check-') as tmp:
        master, slave = pty.openpty()
        cfg = Path(tmp) / 'config'
        cfg.write_text(Path('/etc/gs422.conf').read_text().replace('port=/dev/ttyS6', 'port=' + os.ttyname(slave)))
        command = ['/usr/bin/gs422'] if '--installed' in sys.argv else [sys.executable, '-m', 'gs422.service']
        log = (Path(tmp) / 'log').open('w')
        server = subprocess.Popen(command + ['--config', str(cfg)], cwd=ROOT, stdout=log, stderr=log)
        pending = bytearray()
        load = None
        def ask(command):
            start = time.monotonic()
            os.write(master, command.encode() + b'\r\n')
            while b'\n' not in pending and time.monotonic()-start < 3:
                if select.select([master], [], [], .1)[0]:
                    pending.extend(os.read(master, 8192))
            assert b'\n' in pending, 'missing response'
            line, _, rest = pending.partition(b'\n'); pending[:] = rest
            assert time.monotonic()-start < .5, 'query waits for collection'
            return line.rstrip(b'\r').decode()
        try:
            time.sleep(2.5)
            idle = ask('AT+CPU')
            print('IDLE', idle, flush=True)
            fields = dict(field.split('=', 1) for field in idle[5:].split(','))
            online = len([x for x in Path('/proc/stat').read_text().splitlines() if x.split()[0][3:].isdigit() and x.startswith('cpu')])
            assert int(fields['CORE_COUNT']) == online
            assert fields['USAGE'].endswith('%') and fields['TEMP'].endswith('C')
            core = max(os.sched_getaffinity(0))
            code = ('import os,time; os.sched_setaffinity(0,{' + str(core) + '}); '
                    'end=time.monotonic()+6\nwhile time.monotonic()<end: pass\n')
            load = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
            busy = ask('AT+CPU?')
            print('ONE_CORE_LOAD', busy, flush=True)
            fields = dict(field.split('=', 1) for field in busy[5:].split(','))
            cores = dict(x.split(':',1) for x in fields['CORE_USAGE'].split('|'))
            assert float(cores[str(core)].rstrip('%')) >= 70, 'busy core not detected'
            assert float(fields['USAGE'].rstrip('%')) >= 70 / online
            print('STA', ask('AT+STA'), flush=True)
            load.wait(timeout=5)
            time.sleep(2)
            print('RECOVERED', ask('AT+CPU'), flush=True)
            assert ask('AT+CPU=1') == 'ERROR'
            print('CPU live test PASS', flush=True)
        finally:
            if load is not None and load.poll() is None:
                load.terminate(); load.wait(timeout=3)
            server.terminate()
            try: server.wait(timeout=6)
            except subprocess.TimeoutExpired: server.kill(); server.wait()
            log.close(); os.close(master); os.close(slave)


if __name__ == '__main__':
    main()
