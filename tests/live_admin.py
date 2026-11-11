"""Explicit root-only test on a disposable veth; leaves management IP/SSH intact."""
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gs422.ip_admin import IPAdmin
from gs422.admin_protocol import ip_arguments
from gs422.network import NMClient


class DiagnosticClient(NMClient):
    def call(self, *args, **kwargs):
        try:
            return super().call(*args, **kwargs)
        except Exception as error:
            print('TEST_DBUS_FAILURE', args[:3], str(error), flush=True)
            raise


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def remove_test_profiles(interface, keep=None):
    for uid in run('nmcli', '-g', 'UUID', 'con', 'show').splitlines():
        if uid != keep and run('nmcli', '-g', 'connection.interface-name', 'con', 'show', uid) == interface:
            run('nmcli', 'con', 'delete', uid)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root; creates and removes a disposable veth.')
    name = 'g422t' + str(os.getpid())
    peer = 'g422p' + str(os.getpid())
    profile = 'gs422-test-' + str(os.getpid())
    created = False
    server = None
    dnsmasq = None
    namespace = 'gs422-test-' + str(os.getpid())
    run('ip', 'netns', 'add', namespace)
    manager = IPAdmin(threading.Event(), timeout=4, factory=DiagnosticClient)
    try:
        run('ip', 'link', 'add', name, 'type', 'veth', 'peer', 'name', peer)
        created = True
        run('ip', 'link', 'set', peer, 'netns', namespace)
        run('ip', 'netns', 'exec', namespace, 'ip', 'link', 'set', peer, 'up')
        run('ip', 'netns', 'exec', namespace, 'ip', 'addr', 'add', '198.18.42.254/24', 'dev', peer)
        run('nmcli', 'device', 'set', name, 'managed', 'yes')
        run('nmcli', 'connection', 'add', 'type', 'ethernet', 'ifname', name, 'con-name', profile,
            'ipv4.method', 'manual', 'ipv4.addresses', '198.18.42.1/24', 'ipv4.never-default', 'yes',
            'ipv6.method', 'disabled', 'connection.autoconnect', 'no')
        run('nmcli', 'connection', 'up', profile)
        # Real NM automatic rollback survives loss of the client object/process.
        value = manager.execute('IP1', name, ip_arguments('STATIC,198.18.42.2,255.255.255.0,-,-'))
        print('AUTO_ROLLBACK_STAGE', value, flush=True)
        assert ':PENDING,' in value
        manager.pending = None  # Simulate losing all process-local transaction state.
        time.sleep(6)
        assert '198.18.42.1/24' in run('ip', '-4', 'addr', 'show', 'dev', name)
        assert run('nmcli', '-g', 'ipv4.addresses', 'con', 'show', profile) == '198.18.42.1/24'
        print('NM_AUTOMATIC_ROLLBACK_PASS', flush=True)
        with tempfile.TemporaryDirectory(prefix='gs422-admin-') as tmp:
            dnsmasq = subprocess.Popen(['ip', 'netns', 'exec', namespace, 'dnsmasq',
                '--no-daemon', '--conf-file=/dev/null', '--port=0', '--bind-interfaces',
                '--interface=' + peer, '--dhcp-authoritative',
                '--dhcp-range=198.18.42.10,198.18.42.20,255.255.255.0,2m',
                '--dhcp-option=3', '--dhcp-option=6,198.18.42.254',
                '--dhcp-leasefile=' + tmp + '/leases', '--pid-file=' + tmp + '/dnsmasq.pid'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            master, slave = pty.openpty()
            config = Path(tmp) / 'config'
            config.write_text(f'port={os.ttyname(slave)}\nethernet1={name}\nethernet2=eth0\n')
            log = open(Path(tmp) / 'service.log', 'w+')
            server = subprocess.Popen([sys.executable, '-m', 'gs422.service', '--config', str(config)],
                                      cwd=ROOT, stdout=log, stderr=log)
            pending = bytearray()
            def ask(command):
                os.write(master, command.encode() + b'\r\n')
                deadline = time.monotonic() + 50
                while b'\n' not in pending and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        pending.extend(os.read(master, 8192))
                assert b'\n' in pending, command + ': no reply'
                line, _, rest = pending.partition(b'\n')
                pending[:] = rest
                result = line.rstrip(b'\r').decode()
                print(command, '=>', result, flush=True)
                return result
            try:
                time.sleep(2)
                assert 'IFACE=' + name in ask('AT+IP1=CONFIG')
                assert ':PENDING,' in ask('AT+IP1=STATIC,198.18.42.2,255.255.255.0,-,1.1.1.1')
                assert '198.18.42.2' in ask('AT+IP1')
                assert ask('AT+IP2=DHCP') == '+IP2:ERR,BUSY'
                assert ask('AT+IP2=CONFIRM') == '+IP2:ERR,NO_PENDING'
                assert ask('AT+IP1=CONFIRM') == '+IP1:OK,SAVED,APPLIED=1'
                assert run('nmcli', '-g', 'ipv4.addresses', 'con', 'show', profile) == '198.18.42.2/24'
                assert run('nmcli', '-g', 'ipv4.dns', 'con', 'show', profile) == '1.1.1.1'
                assert ':PENDING,' in ask('AT+IP1=STATIC,198.18.42.3,255.255.255.0,-,-')
                assert ask('AT+IP1=ROLLBACK') == '+IP1:OK,ROLLED_BACK'
                assert '198.18.42.2' in ask('AT+IP1')
                assert ask('AT+IP1=STATIC,198.18.42.0,255.255.255.0,-,-') == '+IP1:ERR,BAD_COMMAND'
                assert 'PORT=22' in ask('AT+SSH')
                assert ask('AT+SSH=PORT,22') == '+SSH:OK,PORT=22,UNCHANGED=1'
                assert ask('AT+SSH=PORT,0') == '+SSH:ERR,BAD_COMMAND'
                assert ask('AT+CPU').startswith('+CPU:')
                assert ask('AT+STA').startswith('+STA:')
                assert dnsmasq.poll() is None, 'test DHCP server failed'
                assert ':PENDING,' in ask('AT+IP1=DHCP')
                assert 'MODE=DHCP' in ask('AT+IP1=CONFIG')
                assert '198.18.42.' in ask('AT+IP1')
                assert ask('AT+IP1=CONFIRM') == '+IP1:OK,SAVED,APPLIED=1'
                assert run('nmcli', '-g', 'ipv4.method', 'con', 'show', profile) == 'auto'
                # No carrier: save for the next cable connection, without changing eth0.
                run('nmcli', 'connection', 'modify', profile, 'connection.autoconnect', 'no')
                run('nmcli', 'device', 'disconnect', name)
                run('ip', 'netns', 'exec', namespace, 'ip', 'link', 'set', peer, 'down')
                time.sleep(3)
                # NM can auto-generate another Ethernet profile for a new veth.
                remove_test_profiles(name, run('nmcli', '-g', 'connection.uuid', 'con', 'show', profile))
                assert 'APPLIED=0' in ask('AT+IP1=STATIC,198.18.42.4,255.255.255.0,-,-')
                assert ask('AT+IP1=CONFIRM') == '+IP1:OK,SAVED,APPLIED=0'
                assert run('nmcli', '-g', 'ipv4.addresses', 'con', 'show', profile) == '198.18.42.4/24'
                print('ADMIN_PTY_LIVE_PASS', flush=True)
            except Exception:
                log.flush(); log.seek(0); print(log.read()[-6000:], flush=True)
                raise
            finally:
                server.terminate()
                server.wait(timeout=20)
                server = None
                os.close(master); os.close(slave); log.close()
    finally:
        if server:
            server.terminate(); server.wait(timeout=20)
        if dnsmasq:
            dnsmasq.terminate(); dnsmasq.wait(timeout=5)
        manager.rollback()
        subprocess.run(['nmcli', 'con', 'delete', profile], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['nmcli', 'con', 'delete', 'gs422-' + name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if created:
            subprocess.run(['ip', 'link', 'delete', name], check=True)
            remove_test_profiles(name)
        run('ip', 'netns', 'delete', namespace)


if __name__ == '__main__':
    main()
