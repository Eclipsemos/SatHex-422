"""Build a small APP software release from a verified Spaceborne offline test bundle."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

VERSION='spaceborne-20261125'
HERE=Path(__file__).resolve().parent

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle',type=Path)
    parser.add_argument('destination',type=Path)
    args=parser.parse_args()
    root=args.bundle.resolve()
    dest=args.destination.resolve()
    dest.mkdir(exist_ok=False)
    subprocess.run(['sha256sum','--quiet','-c','SHA256SUMS'],cwd=root,check=True)
    sys.path.insert(0,str(root/'runtime'))
    from sathex_decode import lte_config
    manifest=json.loads((root/'testdata/4g/manifest.json').read_text())
    with tempfile.TemporaryDirectory() as tmp:
        tmp=Path(tmp)
        (tmp/'ready.c').write_text('#include <stdio.h>\n#include <unistd.h>\nint main(void){puts("READY:'+VERSION+'");fflush(stdout);for(;;)pause();}\n')
        subprocess.run(['gcc','-static','-Os','-s',str(tmp/'ready.c'),'-o',str(tmp/'sample')],check=True)
        with tarfile.open(tmp/'layer.tar','w') as t:
            def add(name,data,mode=0o644):
                info=tarfile.TarInfo(name)
                info.size=len(data)
                info.mode=mode
                t.addfile(info,io.BytesIO(data))
            add('sample',(tmp/'sample').read_bytes(),0o755)
            for name in ('lte_output_pipeline.py','spaceborne_nr.py','capture_schema.py'):
                add('sathex/release/'+name,(root/'runtime'/name).read_bytes())
            add('sathex/release/lte.json',json.dumps(lte_config(manifest['segments'][0],'dci',100,1)).encode())
            add('sathex/release/nr.json',(root/'runtime/nr-n1-2130p15.json').read_bytes())
            add('sathex/release/job.py',(HERE/'spaceborne_capture_job.py').read_bytes())
        tag='sathex-release:'+VERSION
        image=subprocess.check_output(['docker','import','--change','VOLUME ["/sathex/release"]',str(tmp/'layer.tar'),tag],text=True).strip()
    archive=dest/'release.tar'
    subprocess.run(['docker','save','-o',str(archive),tag],check=True)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    record=dict(version=VERSION,image_id=image,sha256=digest,bytes=archive.stat().st_size)
    (dest/'release.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(record))

if __name__=='__main__':
    main()
