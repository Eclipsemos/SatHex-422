import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from gs422.core import Config
from gs422.platform import Reader, interfaces

ROOT = Path(__file__).resolve().parents[1]


class LinuxTests(unittest.TestCase):
    def test_sensor_timeout_and_output_limit(self):
        reader = Reader(threading.Event())
        start = time.monotonic()
        self.assertIsNone(reader.read([sys.executable, '-c', 'import time; time.sleep(30)'], .15))
        self.assertLess(time.monotonic()-start, 1)
        self.assertIsNone(reader.read([sys.executable, '-c', 'print("a"*20000)'], 1, 1024))
        self.assertEqual(reader.read(['/bin/echo', '123'], 1), '123\n')

    def test_interface_order_filters_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            net = base/'net'; net.mkdir()
            for name, target, wireless in [('eth9','b',False), ('enP4','a',False), ('wlp0','c',True), ('docker0',None,False)]:
                path=net/name; path.mkdir(); (path/'type').write_text('1')
                if target:
                    (base/target).mkdir(); (path/'device').symlink_to(base/target)
                if wireless: (path/'wireless').mkdir()
            self.assertEqual(interfaces(Config(),net), {'IP1':'enP4','IP2':'eth9','WIFI':'wlp0'})
            self.assertEqual(interfaces(Config(ethernet1='eth9'),net)['IP2'], 'enP4')
            (net/'enP4').rename(net/'renamed')
            self.assertEqual(interfaces(Config(),net)['IP1'], 'renamed')

    def test_pty_slow_collector_expiry_recovery_burst_and_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            current=base/'current'; current.write_text('1200')
            soc=base/'soc'; soc.write_text('37000')
            smi=base/'smi'
            smi.write_text('#!/bin/sh\nsleep 20\n')
            smi.chmod(0o755)
            master,slave=pty.openpty()
            config=base/'config'
            config.write_text(f'port={os.ttyname(slave)}\nsmi={smi}\nsoc_temp_path={soc}\ncurrent_path={current}\ncurrent_divisor=1\ncache_ttl_ms=500\nsample_interval_ms=100\n')
            with (base/'log').open('w+') as log:
                process=subprocess.Popen([sys.executable,'-m','gs422.service','--config',str(config)],cwd=ROOT,stdout=log,stderr=log)
                pending=bytearray()
                def ask(command, count=1):
                    os.write(master,command)
                    deadline=time.monotonic()+3
                    while pending.count(b'\n')<count and time.monotonic()<deadline:
                        if select.select([master],[],[],.1)[0]: pending.extend(os.read(master,8192))
                    result=[]
                    for _ in range(count):
                        self.assertIn(b'\n',pending)
                        line,_,rest=pending.partition(b'\n'); pending[:]=rest
                        result.append(line.rstrip(b'\r').decode())
                    return result
                try:
                    time.sleep(.4)
                    started=time.monotonic()
                    self.assertEqual(ask(b'AT+STA\r\n'), ['+STA:NPU_TEMP=-,NPU_POWER=-,RK_TEMP=37.00C,VIN12_CURRENT=1200mA'])
                    self.assertLess(time.monotonic()-started,.5)
                    other=subprocess.run([sys.executable,'-m','gs422.service','--config',str(config)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3)
                    self.assertNotEqual(other.returncode,0)
                    self.assertEqual(ask(b'AT+STA\n'*40,40), ['+STA:NPU_TEMP=-,NPU_POWER=-,RK_TEMP=37.00C,VIN12_CURRENT=1200mA']*40)
                    self.assertEqual(ask(b'x'*513+b'\nAT+BOGUS\nAT+WIFI=x,short\n',3), ['ERROR','ERROR','+WIFI:ERR'])
                    current.write_text('bad'); time.sleep(.7)
                    self.assertEqual(ask(b'AT+STA\n'), ['+STA:NPU_TEMP=-,NPU_POWER=-,RK_TEMP=37.00C,VIN12_CURRENT=-'])
                    current.write_text('0'); time.sleep(.25)
                    self.assertEqual(ask(b'AT+STA?\n'), ['+STA:NPU_TEMP=-,NPU_POWER=-,RK_TEMP=37.00C,VIN12_CURRENT=0mA'])
                    # CPU query must not sleep for a new usage window or wait for the slow NPU.
                    time.sleep(1.1)
                    # TTL .5s is shorter than the CPU cadence; wait for a fresh snapshot.
                    deadline=time.monotonic()+2
                    while True:
                        started=time.monotonic()
                        cpu=ask(b'AT+CPU\r\n')[0]
                        self.assertLess(time.monotonic()-started,.5)
                        if not cpu.startswith('+CPU:USAGE=-') or time.monotonic()>=deadline:
                            break
                        time.sleep(.1)
                    self.assertRegex(cpu,r'^\+CPU:USAGE=\d+\.\d%,IOWAIT=\d+\.\d%')
                    self.assertIn('TEMP=37.00C',cpu)
                    self.assertIn('WINDOW_MS=',cpu)
                    replies=ask(b'AT+CPU?\nAT+STA\nAT+CPU=1\n',3)
                    self.assertTrue(replies[0].startswith('+CPU:'))
                    self.assertTrue(replies[1].startswith('+STA:'))
                    self.assertEqual(replies[2],'ERROR')
                finally:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()
                    os.close(master); os.close(slave)
                self.assertEqual(process.returncode,0)


if __name__=='__main__': unittest.main()
