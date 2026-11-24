"""Measured snapshot producer. Houmo NPU only; no RK-NPU substitution.

Voltage/power paths and divisors require explicit verified configuration. Without
them the approved invalid marker is published. No assumed nominal voltage or
conversion from an unverified current rail is used.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from .core import parse_smi
from . import tmg


class Measurements:
    def __init__(self, config):
        required={'soc_temp_path','smi','card','voltage','power'}
        if set(config)-{'utc_verified'} != required:
            raise ValueError('invalid measurement configuration')
        if not isinstance(config['card'],int) or isinstance(config['card'],bool) or config['card']<0:
            raise ValueError('invalid Houmo card index')
        for name in ('soc_temp_path','smi'):
            if not isinstance(config[name],str) or not Path(config[name]).is_absolute():raise ValueError('absolute path required')
        for name in ('voltage','power'):
            source=config[name]
            if source is not None:
                provisional = (name == 'voltage' and source == {
                        'path':'/sys/bus/i2c/devices/4-0040/vin', 'divisor':1000,
                        'verified':False, 'bench_mapping':'is6105a-vin-mV'})
                if not provisional and (set(source)!={'path','divisor','verified'} or source['verified'] is not True):
                    raise ValueError('unverified measurement scaling')
                if not Path(source['path']).is_absolute() or type(source['divisor']) not in (int,float) or not 0<source['divisor']<1e12:
                    raise ValueError('invalid path/divisor')
        self.config=config

    def _scaled(self,path,divisor,field):
        try:
            text=Path(path).read_text()
            if len(text)>128:raise ValueError('sensor text too large')
            return tmg.raw_value(field,float(text.strip())/divisor)
        except (OSError,ValueError,OverflowError):return tmg.INVALID[field]

    def sample(self):
        c=self.config
        values=dict(tmg.INVALID)
        values['TMG006']=self._scaled(c['soc_temp_path'],1000,'TMG006')
        for name,field in (('voltage','TMG034'),('power','TMG005')):
            if c[name] is not None:
                values[field]=self._scaled(c[name]['path'],c[name]['divisor'],field)
        try:
            p=subprocess.run([c['smi'],'-a'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL,timeout=2,text=True)
            if p.returncode==0 and len(p.stdout)<=262144:
                temperature,_=parse_smi(p.stdout,c['card'])
                if temperature is not None:values['TMG007']=tmg.raw_value('TMG007',temperature/100)
        except (OSError,ValueError,subprocess.SubprocessError):pass
        return {'source':'measured','monotonic':time.monotonic(),'values':values}


def write_snapshot(path,snapshot):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.measurement-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:json.dump(snapshot,f);f.flush()
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--output',required=True);p.add_argument('--once',action='store_true')
    args=p.parse_args();reader=Measurements(json.loads(Path(args.config).read_text()))
    while True:
        started=time.monotonic();write_snapshot(args.output,reader.sample())
        if args.once:return
        time.sleep(max(.1,1-(time.monotonic()-started)))


if __name__=='__main__':main()
