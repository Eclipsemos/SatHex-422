"""Real OpenSSH tests using a separate systemd unit and loopback-only ports."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gs422.ssh_admin import SSHAdmin


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def main():
    unit = 'gs422-ssh-test-' + str(os.getpid()) + '.service'
    unitfile = Path('/run/systemd/system') / unit
    with tempfile.TemporaryDirectory(prefix='gs422-ssh-test-') as tmp:
        root = Path(tmp)
        directory = root / 'config.d'; directory.mkdir()
        port1, port2 = free_port(), free_port()
        while port2 == port1:
            port2 = free_port()
        config = root / 'sshd_config'
        config.write_text(f'Include {directory}/*.conf\nListenAddress 127.0.0.1\n'
                          'HostKey /etc/ssh/ssh_host_ed25519_key\nPasswordAuthentication no\n'
                          'UsePAM no\nPermitRootLogin no\n')
        class TestSSH(SSHAdmin):
            def run(self, *args, **kwargs):
                args = tuple(unit if a == 'ssh.service' else unit.replace('.service', '.socket') if a == 'ssh.socket' else a for a in args)
                if args[0] == '/usr/sbin/sshd':
                    args += ('-f', str(config))
                return super().run(*args, **kwargs)
        admin = TestSSH(directory)
        admin.write(f'Port {port1}\n'.encode())
        unitfile.write_text('[Unit]\nDescription=Disposable gs422 SSH integration test\n'
                            '[Service]\nType=simple\n'
                            f'ExecStart=/usr/sbin/sshd -D -f {config}\n'
                            'ExecReload=/bin/kill -HUP $MAINPID\nKillMode=process\n'
                            '[Install]\nWantedBy=multi-user.target\n')
        try:
            run('systemctl', 'daemon-reload')
            reply = admin.execute(('ON',)); print('ON', reply, flush=True)
            assert reply == '+SSH:OK,STATE=ON,AUTOSTART=1'
            reply = admin.execute(('PORT', port2)); print('PORT', reply, flush=True)
            assert reply == f'+SSH:OK,PORT={port2},RECONNECT_REQUIRED=1'
            with socket.create_connection(('127.0.0.1', port2), timeout=3) as client:
                assert client.recv(256).startswith(b'SSH-')
            assert not admin.listening(port1)
            with socket.socket() as busy:
                busy.bind(('127.0.0.1', 0)); busy.listen()
                assert admin.execute(('PORT', busy.getsockname()[1])) == '+SSH:ERR,PORT_IN_USE'
            assert admin.ports() == [port2]
            # An explicit Port elsewhere must be rejected and restored.
            with config.open('a') as stream:
                stream.write(f'Port {port2}\n')
            assert admin.execute(('PORT', port1)) == '+SSH:ERR,CONFIG_CONFLICT'
            assert admin.ports() == [port2]
            reply = admin.execute(('OFF',)); print('OFF', reply, flush=True)
            assert reply == '+SSH:OK,STATE=OFF,AUTOSTART=0'
            assert not admin.listening(port2)
            print('ISOLATED_SSH_LIVE_PASS', flush=True)
        finally:
            subprocess.run(['systemctl', 'disable', '--now', unit], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            unitfile.unlink(missing_ok=True)
            run('systemctl', 'daemon-reload')


if __name__ == '__main__':
    main()
