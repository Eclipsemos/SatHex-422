import pathlib
import tempfile
import threading
import unittest
from unittest.mock import Mock
from gs422.core import parse
from gs422.admin_protocol import AdminError
from gs422.ip_admin import IPAdmin
from gs422.ssh_admin import SSHAdmin


class AdminProtocolTests(unittest.TestCase):
    def test_valid_commands(self):
        self.assertEqual(parse(b'AT+IP2=STATIC,192.168.5.135,255.255.255.0,192.168.5.1,192.168.5.1;1.1.1.1'),
                         ('IP2_ADMIN', ('STATIC', '192.168.5.135', 24, '192.168.5.1', ('192.168.5.1', '1.1.1.1'))))
        for op in ('DHCP', 'CONFIG', 'CONFIRM', 'ROLLBACK'):
            self.assertEqual(parse(('AT+IP1=' + op).encode()), ('IP1_ADMIN', (op,)))
        for command, value in (('AT+SSH', ('QUERY',)), ('AT+SSH?', ('QUERY',)),
                               ('AT+SSH=ON', ('ON',)), ('AT+SSH=OFF', ('OFF',)),
                               ('AT+SSH=PORT,2222', ('PORT', 2222))):
            self.assertEqual(parse(command.encode()), ('SSH', value))

    def test_network_validation(self):
        for address, mask, gateway, dns in (
            ('192.168.5.0', '255.255.255.0', '-', '-'),
            ('192.168.5.255', '255.255.255.0', '-', '-'),
            ('127.0.0.1', '255.255.255.0', '-', '-'),
            ('224.0.0.1', '255.255.255.0', '-', '-'),
            ('192.168.5.2', '0.0.0.255', '-', '-'),
            ('192.168.5.2', '255.0.255.0', '-', '-'),
            ('192.168.5.2', '255.255.255.0', '192.168.6.1', '-'),
            ('192.168.5.2', '255.255.255.0', '192.168.5.2', '-'),
            ('192.168.5.2', '255.255.255.0', '-', '8.8.8.8;'),
            ('192.168.5.2', '255.255.255.0', '-', '8.8.8.8;8.8.8.8'),
            ('192.168.5.2', '255.255.255.0', '-', '8.8.8.8;reboot'),
        ):
            with self.subTest(address=address, mask=mask, gateway=gateway, dns=dns):
                self.assertEqual(parse(f'AT+IP1=STATIC,{address},{mask},{gateway},{dns}'.encode()), ('IP1_ERR', None))
        for value in ('0', '65536', '-1', '22;reboot', '22\x00', '22,ON', ' 22'):
            self.assertEqual(parse(('AT+SSH=PORT,' + value).encode()), ('SSH_ERR', None))


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.manager = IPAdmin(threading.Event(), factory=lambda *_: self.client, clock=lambda: 100)
        self.pending = dict(label='IP1', checkpoint='/cp/1', expires=200,
                            device='/dev/1', connection='/con/1', settings={}, active=True)

    def test_no_pending_or_wrong_interface(self):
        self.assertEqual(self.manager.execute('IP1', 'eth0', ('CONFIRM',)), '+IP1:ERR,NO_PENDING')
        self.manager.pending = self.pending
        self.assertEqual(self.manager.execute('IP2', 'eth1', ('ROLLBACK',)), '+IP2:ERR,NO_PENDING')
        self.client.call.assert_not_called()

    def test_expired_checkpoint_is_never_saved(self):
        self.manager.pending = self.pending
        self.client.get.return_value = []
        self.assertEqual(self.manager.execute('IP1', 'eth0', ('CONFIRM',)), '+IP1:ERR,EXPIRED')
        self.client.call.assert_not_called()
        self.assertIsNone(self.manager.pending)

    def test_failed_rollback_is_reported(self):
        self.manager.pending = self.pending
        self.client.get.return_value = ['/cp/1']
        self.client.call.return_value = ({'/dev/1': 2},)
        self.assertEqual(self.manager.execute('IP1', 'eth0', ('ROLLBACK',)), '+IP1:ERR,ROLLBACK_FAILED')
        self.assertIsNotNone(self.manager.pending)


class SSHTests(unittest.TestCase):
    def test_conflicting_effective_port_restores_exact_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            admin = SSHAdmin(pathlib.Path(tmp))
            old = b'# existing settings\nPort 2222\n'
            admin.write(old)
            admin.ports = Mock(side_effect=[[2222], [22, 3333]])
            admin.state = Mock(return_value='active')
            admin.run = Mock(return_value='')
            admin.listening = Mock(return_value=False)
            with self.assertRaises(AdminError) as error:
                admin.set_port(3333)
            self.assertEqual(error.exception.code, 'CONFIG_CONFLICT')
            self.assertEqual(admin.path.read_bytes(), old)
            admin.run.assert_any_call('/bin/systemctl', 'reload-or-restart', 'ssh.service')

    def test_busy_port_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            admin = SSHAdmin(pathlib.Path(tmp))
            admin.ports = Mock(return_value=[22])
            admin.listening = Mock(return_value=True)
            with self.assertRaises(AdminError):
                admin.set_port(3333)
            self.assertFalse(admin.path.exists())

    def test_failed_start_restores_autostart_and_state(self):
        admin = SSHAdmin()
        states = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'UnitFileState': 'disabled'}
        admin.state = Mock(side_effect=lambda key: states[key])
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            if args[1:3] == ('enable', '--now'):
                raise AdminError('SYSTEM_FAILED')
            return 'inactive'
        admin.run = run
        self.assertEqual(admin.execute(('ON',)), '+SSH:ERR,SYSTEM_FAILED')
        self.assertIn(('/bin/systemctl', 'disable', 'ssh.service'), calls)
        self.assertIn(('/bin/systemctl', 'stop', 'ssh.service'), calls)


if __name__ == '__main__':
    unittest.main()
