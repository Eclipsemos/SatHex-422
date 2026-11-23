"""Register the five-parameter CPU example on the lab board as HT226 APP1."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

from provision_sample_catalog import atomic_json


def main():
    config = Path('/etc/gs422-payload-lab.json')
    original = config.read_bytes()
    settings = json.loads(original)
    if settings.get('mode') != 'lab':
        raise SystemExit('LAB configuration required')
    apps = settings.setdefault('algorithms', {'1': {'cpu_image': 'active'}})
    entry = apps.setdefault('1', {'cpu_image': 'active'})
    if 'ht226_image' in entry:
        raise SystemExit('APP1 already has an HT226 image; existing registration retained')
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp)/'sample'
        archive = Path(tmp)/'sample.tar'
        subprocess.run(['gcc','-static','-Os','-s',str(Path(__file__).with_name('z83_sample.c')),
                        '-o',str(binary)],check=True)
        tag = 'sathex-z83-lab:' + hashlib.sha256(binary.read_bytes()).hexdigest()[:16]
        with tarfile.open(archive, 'w') as tar:
            tar.add(binary, arcname='sample')
        image = subprocess.check_output(['docker','import',str(archive),tag],text=True).strip()
    if config.read_bytes() != original:
        raise SystemExit('configuration changed during build; registration skipped')
    entry['ht226_image'] = image
    atomic_json(config, settings)
    print('HT226 APP1', image)


if __name__ == '__main__':
    main()
