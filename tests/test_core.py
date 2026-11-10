import pathlib
import tempfile
import unittest
from gs422.core import Cache, Config, Framer, escape, parse, parse_smi, scaled


class ProtocolTests(unittest.TestCase):
    def test_split_crlf_overflow_and_recovery(self):
        f = Framer()
        self.assertEqual(list(f.feed(b'AT+ST')), [])
        self.assertEqual(list(f.feed(b'A\r')), [b'AT+STA'])
        self.assertEqual(list(f.feed(b'\n\r\nAT+IP1\n')), [b'AT+IP1'])
        self.assertEqual(list(f.feed(b'x'*513+b'\nAT+STA\n')), [None, b'AT+STA'])
        self.assertEqual(list(f.feed(b'x'*512+b'\r')), [b'x'*512])

    def test_queries_and_injection(self):
        for name in ('STA', 'CPU', 'IP1', 'IP2', 'WIFI'):
            for suffix in ('', '?'):
                self.assertEqual(parse(('AT+'+name+suffix).encode()), (name, None))
        for value in (b'AT+STA;reboot', b'AT+STA\x00', b'AT+FOO', b'\xff', None):
            self.assertEqual(parse(value)[0], 'ERROR')

    def test_wifi_escaping_and_boundaries(self):
        self.assertEqual(parse(br'AT+WIFI=a\,b,c\\d12345'), ('SET_WIFI', ('a,b', 'c\\d12345')))
        self.assertEqual(parse(b'AT+WIFI=open,'), ('SET_WIFI', ('open', '')))
        self.assertEqual(parse(('AT+WIFI='+ '中'*10+',12345678').encode())[0], 'SET_WIFI')
        for value in ('a,short', ',12345678', 'a,b,c', 'a,12345678\\', 'a\\z,12345678',
                      '中'*11+',12345678', 'a,'+'z'*64):
            self.assertEqual(parse(('AT+WIFI='+value).encode())[0], 'WIFI_ERR')
        self.assertEqual(parse(('AT+WIFI=a,'+'a'*64).encode())[0], 'SET_WIFI')
        self.assertEqual(escape('a,b\\c'), r'a\,b\\c')

    def test_sensor_units(self):
        self.assertEqual(scaled('37000\n', 10), 3700)
        self.assertEqual(scaled('750000', 1000), 750)
        self.assertEqual(scaled('1262', 1), 1262)
        self.assertEqual(scaled('0', 1), 0)
        for value in ('NaN', 'inf', '', '1\n2', '5 mA', '999999999999'):
            self.assertIsNone(scaled(value, 1))

    def test_cache_expiry_failed_read_and_independent_fields(self):
        clock = [10.]
        cache = Cache(5000, lambda: clock[0])
        self.assertEqual(cache.reply(), '+STA:NPU_TEMP=-,NPU_POWER=-,RK_TEMP=-,VIN12_CURRENT=-')
        cache.update(0, 0)
        cache.update(2, 3700)
        clock[0] = 14.999
        cache.update(0, None)
        cache.update(1, 4920)
        self.assertEqual(cache.reply(), '+STA:NPU_TEMP=0.00C,NPU_POWER=4.920W,RK_TEMP=37.00C,VIN12_CURRENT=-')
        clock[0] = 15.
        self.assertEqual(cache.reply(), '+STA:NPU_TEMP=-,NPU_POWER=4.920W,RK_TEMP=-,VIN12_CURRENT=-')
        cache.update(3, 1200)
        self.assertEqual(cache.reply(), '+STA:NPU_TEMP=-,NPU_POWER=4.920W,RK_TEMP=-,VIN12_CURRENT=1200mA')

    def test_sta_names_units_precision_and_zero(self):
        cache = Cache(5000)
        for i, value in enumerate((3010, 4920, 3515, 1262)):
            cache.update(i, value)
        self.assertEqual(cache.reply(), '+STA:NPU_TEMP=30.10C,NPU_POWER=4.920W,RK_TEMP=35.15C,VIN12_CURRENT=1262mA')
        for i, value in enumerate((-1, 1, -1234, 0)):
            cache.update(i, value)
        self.assertEqual(cache.reply(), '+STA:NPU_TEMP=-0.01C,NPU_POWER=0.001W,RK_TEMP=-12.34C,VIN12_CURRENT=0mA')

    def test_houmo_selected_card_and_ambiguous_power(self):
        text = '''device0 detail infos
 Temperature:
  Core0: 29.8 C
  DDR0: 30.1 C
 Board_Power: 4.92 W
device1 detail infos
 Temperature:
  Core0: 45.5 C
'''
        self.assertEqual(parse_smi(text, 0), (3010, 4920))
        self.assertEqual(parse_smi(text, 1), (4550, 4920))
        self.assertEqual(parse_smi(text, 2), (None, 4920))
        self.assertEqual(parse_smi(text+' Board_Power: 8.0 W\n', 0), (3010, None))

    def test_config_legacy_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp)/'config'
            p.write_text('ethernet1=eth0\nethernet2=eth1\nwifi=wlan0\n')
            c = Config.read(p)
            self.assertEqual((c.ethernet1, c.ethernet2, c.wifi), ('auto',)*3)
            for text in ('soc_divisor=0', 'cache_ttl_ms=99', 'unknown=1', 'card=0\ncard=1'):
                p.write_text(text)
                with self.assertRaises(ValueError):
                    Config.read(p)


if __name__ == '__main__':
    unittest.main()
