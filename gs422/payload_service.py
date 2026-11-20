"""Configured long-running LAB payload service. External drivers are not implied.

Measurement snapshots must explicitly identify real versus fixture data and use
the same host's monotonic clock. Missing/stale sensors suppress telemetry frames,
not control replies. Deployment must provide a measurement producer separately.
"""
import argparse
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time

from . import tmg
from .core import Cache
from .durable_demo import DurableDemo
from .file_services import FileServices
from .payload_backend import PayloadBackend
from .legacy_upload import LegacyUpload
from .ota_staging import Staging
from .revised_runtime import Revised
from .dual_uart import DualUART
from .board_reboot import BoardReboot

LOG=logging.getLogger('gs422.payload')
MEASURED=frozenset(('TMG004','TMG005','TMG006','TMG007','TMG034'))


def load_config(path):
    c=json.loads(Path(path).read_text())
    required={'mode','control_port','upload_port','state_root','records_root','telemetry_file',
              'telemetry_max_age','allow_fixture_telemetry','catalog','ota_capacity','enable_reboot'}
    if not required<=set(c) or set(c)-required-{'fixture_io_root'}:raise ValueError('configuration keys do not match schema')
    if c.get('fixture_io_root') is not None and (not isinstance(c['fixture_io_root'],str) or not Path(c['fixture_io_root']).is_absolute()):
        raise ValueError('fixture IO root must be explicit absolute path')
    if c['mode']!='lab':raise ValueError('only explicitly labelled lab backend is implemented')
    for key in ('control_port','upload_port','state_root','records_root','telemetry_file'):
        if not isinstance(c[key],str) or not Path(c[key]).is_absolute():raise ValueError(key+' must be absolute')
    if os.path.realpath(c['control_port'])==os.path.realpath(c['upload_port']):raise ValueError('UARTs must differ')
    roots=[Path(c[k]).resolve() for k in ('state_root','records_root')]
    if any(x==x.parent for x in roots) or roots[0]==roots[1] or roots[0] in roots[1].parents or roots[1] in roots[0].parents:
        raise ValueError('state and records roots must be distinct nonoverlapping directories')
    if type(c['telemetry_max_age']) not in (int,float) or not math.isfinite(c['telemetry_max_age']) or not .1<=c['telemetry_max_age']<=5:
        raise ValueError('snapshot age must be .1..5 seconds')
    if type(c['allow_fixture_telemetry']) is not bool or type(c['enable_reboot']) is not bool:raise ValueError('boolean settings required')
    if type(c['ota_capacity']) is not int or not 1<=c['ota_capacity']<=256*1024*1024:raise ValueError('OTA capacity out of range')
    import re
    if not isinstance(c['catalog'],dict):raise ValueError('catalog must be object')
    for digest,entry in c['catalog'].items():
        if not re.fullmatch('[0-9a-f]{64}',digest) or set(entry)!={'image_id','version'}:raise ValueError('bad trusted catalog')
        if not re.fullmatch('sha256:[0-9a-f]{64}',entry['image_id']):raise ValueError('immutable image ID required')
        from .ota_wire import version_reply
        version_reply(entry['version'])
    return c


def measurement_snapshot(path, max_age, allow_fixture, clock=time.monotonic):
    p=Path(path)
    if p.stat().st_size>8192:raise ValueError('snapshot too large')
    d=json.loads(p.read_text())
    if set(d)!={'source','monotonic','values'} or set(d['values'])!=MEASURED:raise ValueError('incomplete measured snapshot')
    if d['source'] not in ('measured','fixture') or d['source']=='fixture' and not allow_fixture:
        raise ValueError('fixture measurements not authorized')
    stamp=d['monotonic']
    if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<=clock()-stamp<max_age:
        raise ValueError('snapshot expired or timestamp invalid')
    values={f.id:0 for f in tmg.FIELDS};values.update(d['values'])
    tmg.pack(values)
    return values,stamp+max_age


def serve(c):
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    software=files=staging=legacy=runtime=ports=None
    collector=None
    root=Path(c['state_root'])
    try:
        # Require initial measurements before creating state or taking either UART.
        values,deadline=measurement_snapshot(c['telemetry_file'],c['telemetry_max_age'],c['allow_fixture_telemetry'])
        software=DurableDemo(root/'software',c['catalog'])
        fixture=None
        if c.get('fixture_io_root'):
            from .fixture_io import FixtureIO
            fixture=FixtureIO(c['fixture_io_root'])
            files=FileServices(c['records_root'],sources=fixture.sources,sinks=fixture.sinks,capture_driver=fixture)
            LOG.warning('EXTERNAL_IO=FILE_FIXTURES not physical devices')
            with files.lock:
                if not files.db.execute('SELECT 1 FROM files WHERE type=2 AND complete=1').fetchone():
                    fid=files.add_file([(10,b'fixture-alpha'),(10,b'fixture-beta')])
                    LOG.info('FIXTURE_REPLAY_FILE_ID=%d',fid)
        else:
            files=FileServices(c['records_root'])  # unbound hardware stays unavailable
        reboot=BoardReboot(root/'reboot.json',enabled=True) if c['enable_reboot'] else None
        backend=PayloadBackend(software,files,reboot=reboot)
        staging=Staging(root/'ota',crc_profile='CCITT-FALSE',capacity=c['ota_capacity'])
        legacy=LegacyUpload(root/'legacy')
        runtime=Revised(staging,Cache(5000),values,version=software.version(),adapter=backend,
                        software_backend=backend,algorithms_enabled=True,legacy_upload=legacy)
        runtime.set_telemetry_deadline(deadline)
        def collect():
            previous=None
            while not stop.is_set():
                try:
                    raw,expires=measurement_snapshot(c['telemetry_file'],c['telemetry_max_age'],c['allow_fixture_telemetry'])
                    raw.update(backend.telemetry())
                    runtime.update_telemetry(raw)
                    runtime.set_telemetry_deadline(expires)
                    valid=True
                except Exception:
                    # Fail closed for telemetry; controls remain available.
                    runtime.set_telemetry_deadline(0)
                    valid=False
                if valid!=previous:LOG.info('TELEMETRY_VALID=%s',valid);previous=valid
                stop.wait(.2)
        collector=threading.Thread(target=collect,daemon=True);collector.start()
        runtime.start()
        ports=DualUART(runtime,c['control_port'],c['upload_port'])
        LOG.info('READY lab control=%s upload=%s fixture_telemetry=%s',c['control_port'],c['upload_port'],c['allow_fixture_telemetry'])
        while not stop.is_set():ports.pump()
    finally:
        stop.set()
        if ports:ports.close()
        if collector:
            collector.join(timeout=30)
            if collector.is_alive():raise TimeoutError('measurement worker still running')
        if runtime:runtime.close()
        if files:files.close()
        if staging:staging.close()
        if legacy:legacy.close()
        if software:software.close()  # deployed software persists, not removed on daemon exit


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True)
    p.add_argument('--check-config',action='store_true')
    a=p.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    try:
        c=load_config(a.config)
        if a.check_config:
            print(json.dumps({'configuration':'valid','mode':c['mode'],'trusted_packages':len(c['catalog']),
                              'hardware_drivers':'file_fixtures' if c.get('fixture_io_root') else 'not_bound',
                              'fixture_telemetry':c['allow_fixture_telemetry']}))
            return 0
        serve(c)
        return 0
    except Exception:
        LOG.exception('PAYLOAD_SERVICE_FAILED')
        return 1


if __name__=='__main__':raise SystemExit(main())
