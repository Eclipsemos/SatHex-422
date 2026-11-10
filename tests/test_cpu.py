from pathlib import Path
import tempfile
import unittest
from gs422.cpu import CPUCache, cpu_list, parse_stat, percentages, read_snapshot


class CPUTests(unittest.TestCase):
    def test_guest_not_double_counted_and_wait_not_busy(self):
        old = parse_stat('cpu 100 0 20 800 10 0 10 0 30 0')['cpu']
        new = parse_stat('cpu 120 0 30 850 30 0 10 0 45 0')['cpu']
        self.assertEqual(percentages(old, new), (30, 20))
        self.assertEqual(percentages(old, old), (None, None))
        new[4] = 9
        self.assertEqual(percentages(old, new), (None, None))

    def test_list_and_invalid_counters(self):
        self.assertEqual(cpu_list('0-3,6,8-9'), [0,1,2,3,6,8,9])
        self.assertEqual(cpu_list('4 5'), [4,5])
        for text in ('', '3-1', '-1', '0-9999999', 'x'):
            with self.assertRaises(ValueError): cpu_list(text)
        for text in ('cpu0 1 2 3 4', 'cpu 1 2', 'cpu 1 2 3 -1'):
            with self.assertRaises(ValueError): parse_stat(text)

    def sample(self, stamp, busy=0, idle=100, online=None):
        online = [0,1] if online is None else online
        core = [busy,0,0,idle,0,0,0,0]
        return {'time':stamp,'online':online,'stat':{'cpu':[x*len(online) for x in core],
                **{'cpu'+str(i):core[:] for i in online}},'loads':[.2,.3,.4],
                'frequencies':[[online,1608000]]}

    def test_cold_start_usage_window_and_format(self):
        clock=[10.]; cache=CPUCache(clock=lambda:clock[0])
        self.assertIn('USAGE=-,IOWAIT=-', cache.reply(None))
        cache.update(self.sample(10))
        first=cache.reply(3515)
        self.assertIn('CORE_COUNT=2,CORE_USAGE=0:-|1:-,FREQ=0-1:1608.0MHz,TEMP=35.15C,WINDOW_MS=-',first)
        clock[0]=11.; cache.update(self.sample(11,25,175))
        self.assertEqual(cache.reply(3515), '+CPU:USAGE=25.0%,IOWAIT=0.0%,LOAD1=0.20,LOAD5=0.30,LOAD15=0.40,CORE_COUNT=2,CORE_USAGE=0:25.0%|1:25.0%,FREQ=0-1:1608.0MHz,TEMP=35.15C,WINDOW_MS=1000')

    def test_expiry_failures_do_not_renew_and_recovery_warms(self):
        clock=[10.]; cache=CPUCache(clock=lambda:clock[0])
        cache.update(self.sample(10)); clock[0]=11.;cache.update(self.sample(11,20,180))
        cache.failed(); clock[0]=15.999
        self.assertIn('USAGE=20.0%',cache.reply(None))
        clock[0]=16.
        self.assertIn('CORE_COUNT=-,CORE_USAGE=-,FREQ=-,TEMP=0.00C',cache.reply(0))
        cache.update(self.sample(16,40,200))
        self.assertIn('USAGE=-',cache.reply(None))
        clock[0]=17.;cache.update(self.sample(17,40,300))
        self.assertIn('USAGE=0.0%',cache.reply(None))

    def test_hotplug_and_long_gap_rewarm(self):
        cache=CPUCache(clock=lambda:12.)
        cache.update(self.sample(10)); cache.update(self.sample(11,20,180,online=[0]))
        self.assertIn('USAGE=-',cache.reply(None))
        cache.update(self.sample(12,40,260,online=[0]))
        self.assertIn('USAGE=20.0%',cache.reply(None))
        cache.update(self.sample(20,50,300,online=[0]))
        self.assertIsNone(cache.snapshot[-1])

    def test_read_sources_and_partial_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); proc=root/'proc'; proc.mkdir(); sys=root/'cpu';sys.mkdir()
            (proc/'stat').write_text('cpu 10 0 0 90\ncpu0 5 0 0 45\ncpu1 5 0 0 45\n')
            (proc/'loadavg').write_text('0.1 0.2 0.3 1/20 123')
            (sys/'online').write_text('0-1')
            policy=sys/'cpufreq'/'policy0';policy.mkdir(parents=True)
            (policy/'affected_cpus').write_text('0 1')
            (policy/'scaling_cur_freq').write_text('816000')
            data=read_snapshot(proc,sys)
            self.assertEqual(data['frequencies'], [[[0,1],816000]])
            self.assertEqual(data['loads'], [.1,.2,.3])
            (policy/'scaling_cur_freq').unlink();(proc/'loadavg').write_text('nan 0 0')
            data=read_snapshot(proc,sys)
            self.assertEqual(data['frequencies'], [[[0,1],None]])
            self.assertIsNone(data['loads'])
            (sys/'online').write_text('0-2')
            with self.assertRaises(ValueError):read_snapshot(proc,sys)


if __name__=='__main__':unittest.main()
