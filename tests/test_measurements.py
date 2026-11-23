import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from gs422 import tmg
from gs422.measurements import Measurements,write_snapshot


class MeasurementsTests(unittest.TestCase):
    def config(self,root):
        return dict(soc_temp_path=str(Path(root)/'soc'),smi=str(Path(root)/'missing-hm-smi'),card=0,
                    voltage=None,power=None,utc_verified=True)

    def test_invalid_markers_and_physical_boundaries(self):
        for field,marker in tmg.INVALID.items():
            self.assertIsNone(tmg.physical_value(field,marker))
            with self.assertRaises(ValueError):tmg.raw_value(field,255 if marker==255 else 655.35)
        self.assertEqual(tmg.raw_value('TMG006',254),254)
        self.assertEqual(tmg.physical_value('TMG034',1250),12.5)

    def test_missing_houmo_is_invalid_not_rk_npu_or_zero(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/'soc').write_text('48076')
            value=Measurements(self.config(root)).sample()
            self.assertEqual(value['values']['TMG006'],48)
            self.assertEqual(value['values']['TMG007'],255)
            self.assertEqual(value['values']['TMG005'],65535)
            self.assertEqual(value['values']['TMG034'],65535)
            target=Path(root)/'snapshot.json';write_snapshot(target,value)
            self.assertEqual(json.loads(target.read_text()),value)

    def test_houmo_sample_voltage_and_power_explicit_scaling(self):
        with tempfile.TemporaryDirectory() as root:
            c=self.config(root)
            for name,value in [('soc','49000'),('voltage','12531'),('power','10000000')]:
                (Path(root)/name).write_text(value)
            c['voltage']={'path':str(Path(root)/'voltage'),'divisor':1000,'verified':True}
            c['power']={'path':str(Path(root)/'power'),'divisor':1000000,'verified':True}
            with patch('gs422.measurements.subprocess.run',return_value=subprocess.CompletedProcess([],0,'device0 detail infos\nTemperature:\n core: 52.25 C\nBoard_Power: 8 W\n')):
                values=Measurements(c).sample()['values']
            self.assertEqual((values['TMG006'],values['TMG007'],values['TMG034'],values['TMG005']),(49,52,1253,1000))

    def test_unverified_clock_scale_and_invalid_measurement_refused(self):
        with tempfile.TemporaryDirectory() as root:
            c=self.config(root);c['utc_verified']=False
            self.assertNotIn('TMG004',Measurements(c).sample()['values'])
            c['utc_verified']=True;c['voltage']={'path':str(Path(root)/'v'),'divisor':1000,'verified':False}
            with self.assertRaises(ValueError):Measurements(c)
            c['voltage']=None;(Path(root)/'soc').write_text('nan')
            self.assertEqual(Measurements(c).sample()['values']['TMG006'],255)
