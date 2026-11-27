"""One-time X26 provisioning. Accept a publisher key obtained outside the OTA channel."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('public_key',type=Path)
    p.add_argument('--fingerprint',required=True,help='approved SHA-256 of DER SubjectPublicKeyInfo')
    p.add_argument('--app0-capture',type=Path)
    p.add_argument('--app1-capture',type=Path)
    args=p.parse_args()
    config=Path('/etc/gs422-payload-lab.json')
    c=json.loads(config.read_text())
    if c['mode']!='lab' or os.uname().machine!='aarch64':
        raise SystemExit('requires an ARM64 X26 payload lab installation')
    # Validate and install exactly the same bytes.
    pem=args.public_key.read_bytes()
    der=subprocess.check_output(['openssl','pkey','-pubin','-outform','DER'],input=pem)
    if len(der)!=44 or not der.startswith(bytes.fromhex('302a300506032b6570032100')) or hashlib.sha256(der).hexdigest()!=args.fingerprint:
        raise SystemExit('publisher fingerprint or key type mismatch')
    key=Path('/etc/gs422-publisher-ed25519.pem')
    if c.get('publisher_key',str(key))!=str(key) or key.exists() and key.read_bytes()!=pem:
        raise SystemExit('another trust root is installed')
    apps=c.setdefault('algorithms',{'1':{'cpu_image':'active'}})
    for i,folder in enumerate((args.app0_capture,args.app1_capture)):
        if folder is not None:
            if not folder.is_absolute() or not folder.is_dir():
                raise SystemExit('provide a local absolute capture directory')
            apps.setdefault(str(i),{'cpu_image':'active'})['capture_dir']=str(folder)
    c['publisher_key']=str(key)
    with tempfile.TemporaryDirectory(dir=config.parent) as tmp:
        proposed=Path(tmp)/'config.json'
        proposed.write_text(json.dumps(c,indent=2)+'\n')
        proposed.chmod(0o600)
        subprocess.run(['gs422-payload-lab','--config',str(proposed),'--check-config'],check=True)
        if not key.exists():
            with key.open('xb') as f:f.write(pem)
        os.replace(proposed,config)
    subprocess.run(['systemctl','restart','gs422-payload-lab'],check=True)


if __name__=='__main__':
    main()
