"""Sign one Spaceborne release on the ground. Requires cryptography (and pywin32 for DPAPI)."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release',type=Path,help='directory containing release.tar and release.json')
    parser.add_argument('images',type=Path,help='Spaceborne deployed-images.json')
    parser.add_argument('output',type=Path)
    keys=parser.add_mutually_exclusive_group(required=True)
    keys.add_argument('--private-pem',type=Path)
    keys.add_argument('--dpapi-key',type=Path)
    args=parser.parse_args()
    if args.dpapi_key:
        import win32crypt
        key=Ed25519PrivateKey.from_private_bytes(win32crypt.CryptUnprotectData(args.dpapi_key.read_bytes(),None,None,None,1)[1])
    else:
        key=serialization.load_pem_private_key(args.private_pem.read_bytes(),password=None)
    if not isinstance(key,Ed25519PrivateKey):
        raise ValueError('Ed25519 signing key required')
    public=key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
    record=json.loads((args.release/'release.json').read_text())
    payload=(args.release/'release.tar').read_bytes()
    if len(payload)!=record['bytes'] or hashlib.sha256(payload).hexdigest()!=record['sha256']:
        raise ValueError('release.tar does not match release.json')
    images=json.loads(args.images.read_text())
    algorithms=[]
    for ident,tech,tag in [(0,'LTE','spaceborne-4gapp:1.2.0-prep.2'),(1,'NR','spaceborne-5gapp:sathex-syncfix-20261112')]:
        image,=[im['Id'] for im in images if im['Architecture']=='arm64' and tag in im['RepoTags']]
        algorithms.append(dict(app_id=ident,technology=tech,decoder_image_id=image,release_image_id=record['image_id']))
    manifest=dict(format='sathex.satpkg',format_version=1,version=record['version'],
                  target=dict(board='WIAISAT-X26',soc='RK3588J',os='linux',architecture='arm64',
                              required_loader_capabilities=['satpkg-v1-ed25519','capture_job']),
                  signature=dict(algorithm='Ed25519',key_id='sha256:'+hashlib.sha256(public).hexdigest()),
                  payload=dict(name='release.tar',media_type='application/x-docker-image',bytes=len(payload),
                               sha256=record['sha256'],image_id=record['image_id']),algorithms=algorithms)
    raw=(json.dumps(manifest,sort_keys=True,separators=(',',':'))+'\n').encode()
    with zipfile.ZipFile(args.output,'x',zipfile.ZIP_STORED) as z:
        for name,body in [('manifest.json',raw),('manifest.sig',key.sign(raw)),('release.tar',payload)]:
            z.writestr(name,body)


if __name__=='__main__':
    main()
