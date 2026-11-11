"""Manage Ubuntu's existing OpenSSH service; never accepts shell commands."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
from .admin_protocol import AdminError


class SSHAdmin:
    def __init__(self, directory=Path('/etc/ssh/sshd_config.d'), runtime=Path('/run/sshd')):
        self.path = directory / '00-gs422.conf'
        self.runtime = runtime

    def run(self, *argv, check=True):
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=12, text=True,
                                env={**os.environ, 'LC_ALL': 'C'})
        if check and result.returncode:
            raise AdminError('SYSTEM_FAILED')
        return result.stdout.strip()

    def state(self, prop):
        return self.run('/bin/systemctl', 'show', 'ssh.service', '--property=' + prop, '--value')

    def ports(self):
        # Ubuntu removes RuntimeDirectory when ssh.service stops. The validator
        # still requires it even when configuring a stopped server.
        self.runtime.mkdir(mode=0o755, exist_ok=True)
        text = self.run('/usr/sbin/sshd', '-T')
        return sorted({int(line.split()[1]) for line in text.splitlines() if line.startswith('port ')})

    def write(self, content):
        if self.path.is_symlink():
            raise AdminError('CONFIG_CONFLICT')
        if content is None:
            self.path.unlink(missing_ok=True)
            return
        fd, name = tempfile.mkstemp(prefix='.gs422-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def listening(self, port):
        text = self.run('/usr/bin/ss', '-H', '-ltn')
        return any(line.split()[3].rsplit(':', 1)[-1] == str(port)
                   for line in text.splitlines() if len(line.split()) >= 4)

    def set_port(self, port):
        old_ports = self.ports()
        if old_ports == [port]:
            return f'+SSH:OK,PORT={port},UNCHANGED=1'
        if self.listening(port):
            raise AdminError('PORT_IN_USE')
        if self.path.is_symlink():
            raise AdminError('CONFIG_CONFLICT')
        old = self.path.read_bytes() if self.path.exists() else None
        active = self.state('ActiveState') == 'active'
        try:
            self.write(f'# Managed by gs422 AT+SSH\nPort {port}\n'.encode())
            self.run('/usr/sbin/sshd', '-t')
            if self.ports() != [port]:
                raise AdminError('CONFIG_CONFLICT')
            if active:
                self.run('/bin/systemctl', 'reload', 'ssh.service')
                deadline = time.monotonic() + 5
                while not self.listening(port):
                    if time.monotonic() >= deadline:
                        raise AdminError('NOT_LISTENING')
                    time.sleep(.1)
            return f'+SSH:OK,PORT={port},RECONNECT_REQUIRED={int(active)}'
        except Exception:
            self.write(old)
            if active:
                # reload can fail if the new listener exited; restart restores it.
                self.run('/bin/systemctl', 'reload-or-restart', 'ssh.service')
            raise

    def execute(self, args):
        try:
            if self.state('LoadState') != 'loaded':
                raise AdminError('NOT_INSTALLED')
            op = args[0]
            socket_active = self.run('/bin/systemctl', 'show', 'ssh.socket', '--property=ActiveState', '--value', check=False)
            socket_enabled = self.run('/bin/systemctl', 'is-enabled', 'ssh.socket', check=False)
            if op == 'QUERY':
                active = self.state('ActiveState')
                enabled = self.state('UnitFileState')
                ports = ';'.join(map(str, self.ports()))
                return f'+SSH:STATE={active.upper()},AUTOSTART={enabled.upper()},PORT={ports},SOCKET={socket_active.upper() or "UNKNOWN"}'
            if socket_active == 'active' or socket_enabled in ('enabled', 'enabled-runtime'):
                raise AdminError('SOCKET_ACTIVATION')
            if op == 'PORT':
                return self.set_port(args[1])
            old_active = self.state('ActiveState') == 'active'
            old_enabled = self.state('UnitFileState') == 'enabled'
            try:
                self.run('/bin/systemctl', 'enable' if op == 'ON' else 'disable', '--now', 'ssh.service')
                if (self.state('ActiveState') == 'active') != (op == 'ON'):
                    raise AdminError('STATE_MISMATCH')
                return f'+SSH:OK,STATE={"ON" if op == "ON" else "OFF"},AUTOSTART={int(op == "ON")}'
            except Exception:
                self.run('/bin/systemctl', 'enable' if old_enabled else 'disable', 'ssh.service')
                self.run('/bin/systemctl', 'start' if old_active else 'stop', 'ssh.service')
                raise
        except AdminError as error:
            return '+SSH:ERR,' + error.code
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return '+SSH:ERR,FAILED'
