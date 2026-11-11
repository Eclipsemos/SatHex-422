"""Read-only PTY acceptance test; never opens the production UART."""
import json
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
    with tempfile.TemporaryDirectory(prefix='gs422-disk-', dir='/run') as tmp:
        master, slave = pty.openpty()
        config = Path(tmp) / 'config'
        config.write_text(Path('/etc/gs422.conf').read_text().replace('port=/dev/ttyS6', 'port=' + os.ttyname(slave)))
        installed = '--installed' in sys.argv
        command = ['/usr/bin/gs422'] if installed else [sys.executable, '-m', 'gs422.service']
        unit = 'gs422-disk-check-' + str(os.getpid())
        # Same mount visibility as the production systemd service.
        base = ['systemd-run', '--quiet', '--collect', '--unit=' + unit,
                '--property=ProtectHome=true', '--property=ProtectSystem=strict',
                '--property=PrivateTmp=true', '--property=NoNewPrivileges=true',
                '--property=WorkingDirectory=' + str(ROOT)]
        if not installed:
            # ProtectHome hides the development source; read-only acceptance of
            # source is run directly, installed acceptance exercises sandboxing.
            process = subprocess.Popen(command + ['--config', str(config)], cwd=ROOT,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # Installed package has no dependency on the home source directory.
            base = [x for x in base if not x.startswith('--property=WorkingDirectory=')]
            subprocess.run(base + command + ['--config', str(config)], check=True)
            process = None
        pending = bytearray()
        def ask(command, multi=False):
            os.write(master, command.encode() + b'\r\n')
            deadline = time.monotonic() + 12
            lines = []
            while time.monotonic() < deadline:
                if b'\n' not in pending:
                    if select.select([master], [], [], .1)[0]:
                        pending.extend(os.read(master, 8192))
                    continue
                line, _, rest = pending.partition(b'\n'); pending[:] = rest
                value = line.rstrip(b'\r').decode()
                lines.append(value)
                if not multi or value.startswith(('+DISK:END', '+DISK:ERR')):
                    return '\n'.join(lines)
            raise AssertionError('no complete response: ' + command)
        try:
            time.sleep(2)
            result = ask('AT+DISK', True)
            print(result, flush=True)
            assert '+DISK:COUNT=2,' in result
            assert 'DEVICE=/dev/mmcblk0,' in result and 'DEVICE=/dev/nvme0n1,' in result
            assert 'VOLUME=/dev/mmcblk0p7,' in result and 'VOLUME=/dev/nvme0n1p1,' in result
            for line in result.splitlines():
                if not line.startswith('+DISK:VOLUME=') or 'STATE=MOUNTED' not in line:
                    continue
                fields = dict(p.split('=', 1) for p in line[6:].split(','))
                from urllib.parse import unquote
                stat = os.statvfs(unquote(fields['MOUNT']))
                expected = {'TOTAL': stat.f_blocks * stat.f_frsize,
                            'USED': (stat.f_blocks-stat.f_bfree)*stat.f_frsize,
                            'AVAILABLE': max(0,stat.f_bavail)*stat.f_frsize}
                for key, amount in expected.items():
                    assert abs(float(fields[key][:-3])-amount/(1024**3)) < .02, (key,fields)
            assert 'mmcblk0boot' not in result and '/dev/loop' not in result and '/dev/zram' not in result
            assert ask('AT+DISK=999') == '+DISK:ERR,NO_PAGE'
            assert ask('AT+DISK=-1') == '+DISK:ERR,BAD_COMMAND'
            assert '+DISK:END,NEXT=-' in ask('AT+DISK?', True)
            for command in ('AT+STA','AT+CPU','AT+IP2','AT+SSH'):
                reply=ask(command)
                print(command, '=>', reply, flush=True)
                assert reply.startswith('+' + command[3:] + ':') and ':ERR' not in reply
            print('STORAGE_INSTALLED_PASS' if installed else 'STORAGE_SOURCE_PASS', flush=True)
        finally:
            if process:
                process.terminate(); process.wait(timeout=20)
            else:
                subprocess.run(['systemctl', 'stop', unit], check=True)
            os.close(master); os.close(slave)


if __name__ == '__main__':
    main()
