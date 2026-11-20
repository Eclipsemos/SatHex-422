#!/usr/bin/env python3
"""Side-by-side LAB package; installing never starts or switches UART services."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
VERSION='0.1.0~lab3'


def main():
    with tempfile.TemporaryDirectory(prefix='payload-package-') as directory:
        stage=Path(directory)
        for path in ('DEBIAN','usr/bin','usr/lib/gs422-payload-lab','etc','lib/systemd/system','usr/share/doc/gs422-payload-lab'):
            (stage/path).mkdir(parents=True,exist_ok=True)
        shutil.copytree(ROOT/'gs422',stage/'usr/lib/gs422-payload-lab/gs422',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        launchers=[]
        for name,module in [('gs422-payload-lab','payload_service'),('gs422-measurements-lab','measurements')]:
            path=stage/'usr/bin'/name
            path.write_text('#!/usr/bin/python3\nimport sys\nsys.path.insert(0,"/usr/lib/gs422-payload-lab")\nfrom gs422.'+module+' import main\nraise SystemExit(main())\n')
            launchers.append(path)
        configs=[('gs422-payload-lab.example.json','gs422-payload-lab.json'),('gs422-measurements.example.json','gs422-measurements-lab.json')]
        for source,target in configs:shutil.copyfile(ROOT/'config'/source,stage/'etc'/target)
        for name in ('gs422-payload-lab','gs422-measurements-lab'):
            shutil.copyfile(ROOT/'deploy'/(name+'.service'),stage/'lib/systemd/system'/(name+'.service'))
        shutil.copytree(ROOT/'docs',stage/'usr/share/doc/gs422-payload-lab/docs')
        manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted((ROOT/'gs422').glob('*.py'))}
        (stage/'usr/share/doc/gs422-payload-lab/source-sha256.json').write_text(json.dumps(manifest,indent=2)+'\n')
        (stage/'DEBIAN/control').write_text(f'''Package: gs422-payload-lab
Version: {VERSION}
Architecture: all
Maintainer: SatHex Development
Section: misc
Priority: optional
Depends: python3 (>= 3.10), systemd
Description: Explicit laboratory dual-UART payload runtime
 No automatic UART takeover or fixture activation. Configure trusted packages,
 external drivers and telemetry producer before enabling a runtime.
''')
        (stage/'DEBIAN/conffiles').write_text(''.join('/etc/'+target+'\n' for _,target in configs))
        scripts={'postinst':'if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi\n',
                 'prerm':'if [ "$1" = remove ] && [ -d /run/systemd/system ]; then systemctl disable --now gs422-payload-lab.service gs422-measurements-lab.service || true; fi\n',
                 'postrm':'if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi\n'}
        for name,body in scripts.items():
            (stage/'DEBIAN'/name).write_text('#!/bin/sh\nset -e\n'+body)
        stage.chmod(0o755)
        for p in stage.rglob('*'):
            executable=p in launchers or p.parent==stage/'DEBIAN' and p.name in scripts
            p.chmod(0o755 if p.is_dir() or executable else 0o644)
        dist=ROOT/'dist';dist.mkdir(exist_ok=True)
        target=dist/f'gs422-payload-lab_{VERSION}_all.deb'
        subprocess.run(['dpkg-deb','--root-owner-group','--build',str(stage),str(target)],check=True)
        print(target)


if __name__=='__main__':main()
