"""Entry point in the OTA release volume, invoked inside the pinned decoder."""
import json
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parent
app,*words=map(int,sys.argv[1:])
if app not in (0,1) or words != [0]*5:
    raise SystemExit(2)
technology=('lte','nr')[app]
script=('lte_output_pipeline.py','spaceborne_nr.py')[app]
out=Path('/data/output')
cmd=[sys.executable,str(root/script),'--manifest','/data/capture/manifest.json',
     '--config',str(root/(technology+'.json')),'--work-dir',str(out/'analysis')]
if app==1:
    cmd+=['--decoder','/usr/local/bin/5g_sniffer']
with (out/'decode.log').open('w') as log:
    subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=600,check=True)
if app==0:
    with (out/'decode.log').open() as log:
        if any(line.startswith('job=') and ' failed:' in line for line in log):
            raise RuntimeError('decoder reported a failed LTE job')
summary=json.loads((out/'analysis/summary.json').read_text())
value=summary['dci_records'] if app==0 else summary['events']['mib']
result=dict(technology=technology,words=words,value=value,summary=summary)
(out/'run.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
