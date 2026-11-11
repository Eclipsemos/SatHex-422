import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from gs422.core import parse
from gs422.storage import filesystem_usage, render


def node(name, kind='disk', mounts=None, fs=None, children=None, dev='8:0'):
    return {'name': name, 'kname': name, 'path': '/dev/' + name, 'type': kind,
            'size': 1024**3, 'fstype': fs, 'mountpoints': mounts or [None],
            'model': 'Test disk', 'maj:min': dev, 'children': children or []}


class StorageTests(unittest.TestCase):
    def test_commands(self):
        for command in ('AT+DISK', 'AT+DISK?', 'AT+DISK=0'):
            self.assertEqual(parse(command.encode()), ('DISK', 0))
        self.assertEqual(parse(b'AT+DISK=2'), ('DISK', 2))
        for command in ('AT+DISK=-1', 'AT+DISK=1000', 'AT+DISK=0;reboot', 'AT+DISK=', 'AT+DISK=LIST'):
            self.assertEqual(parse(command.encode()), ('DISK_ERR', None))

    def test_filters_unmounted_and_bind_mount_deduplication(self):
        usage = Mock(return_value=(1024**3, 512*1024**2, 400*1024**2, '56.1%'))
        tree = {'blockdevices': [node('loop0','loop'), node('zram0'), node('mmcblk0boot0'),
            node('mmcblk0', children=[node('mmcblk0p1','part',dev='179:1'),
                node('mmcblk0p6','part',fs='ext4',dev='179:6'),
                node('mmcblk0p7','part',['/','/bind/root'],'ext4',dev='179:7')]),
            node('nvme0n1', children=[node('nvme0n1p1','part',['/media/ssd'],'ext4',dev='259:1')])]}
        output = render(tree, 0, usage)
        self.assertIn('COUNT=2', output)
        self.assertNotIn('DEVICE=/dev/zram', output)
        self.assertNotIn('boot0', output)
        self.assertNotIn('VOLUME=/dev/mmcblk0p1,', output)
        self.assertIn('MOUNT=-,STATE=UNMOUNTED,TOTAL=-,USED=-,AVAILABLE=-,USE=-', output)
        self.assertIn('MOUNT=/,STATE=MOUNTED', output)
        self.assertIn('USED=0.50GiB', output)
        self.assertEqual(usage.call_count, 2)
        self.assertTrue(output.endswith('+DISK:END,NEXT=-'))

    @unittest.skipUnless(hasattr(os, 'makedev'), 'POSIX device numbers')
    def test_reserved_blocks_are_not_used_data(self):
        stat = Mock(return_value=SimpleNamespace(st_dev=os.makedev(8,1)))
        vfs = Mock(return_value=SimpleNamespace(f_blocks=1000, f_bfree=400, f_bavail=350, f_frsize=4096))
        self.assertEqual(filesystem_usage('/', '8:1', stat, vfs),
                         (4096000, 2457600, 1433600, '63.2%'))
        stat.return_value = SimpleNamespace(st_dev=os.makedev(8,2))
        with self.assertRaises(OSError):
            filesystem_usage('/', '8:1', stat, vfs)
        self.assertEqual(vfs.call_count, 1)

    def test_mount_failure_is_not_zero_usage(self):
        tree={'blockdevices':[node('sda',mounts=['/media/a,\n+STA:fake'],fs='ext4')]}
        out=render(tree,0,Mock(side_effect=OSError()))
        self.assertIn('STATE=UNAVAILABLE,TOTAL=-,USED=-',out)
        self.assertIn('%2C%0A%2BSTA:fake',out)
        self.assertEqual(len(out.splitlines()),4)

    def test_empty_and_page_bounds(self):
        self.assertEqual(render({'blockdevices':[]},0), '+DISK:COUNT=0,PAGE=0,PAGES=1,ROWS=0\n+DISK:END,NEXT=-')
        tree={'blockdevices':[node('sd'+chr(97+i)) for i in range(10)]}
        self.assertIn('PAGES=2,ROWS=8',render(tree,0))
        self.assertTrue(render(tree,0).endswith('NEXT=1'))
        self.assertIn('PAGE=1,PAGES=2,ROWS=2',render(tree,1))
        self.assertEqual(render(tree,2),'+DISK:ERR,NO_PAGE')

    def test_shared_logical_volume_counted_once(self):
        logical=node('dm-0','lvm',['/data'],'ext4',dev='253:0')
        tree={'blockdevices':[node('sda',children=[logical]),node('sdb',children=[logical])]}
        usage=Mock(return_value=(1,1,0,'100.0%'))
        out=render(tree,0,usage)
        self.assertEqual(out.count('VOLUME=/dev/dm-0,'),1)
        self.assertIn('DISKS=sda;sdb',out)
        self.assertEqual(usage.call_count,1)


if __name__ == '__main__':
    unittest.main()
