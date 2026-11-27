"""Isolated ARM64 Docker integration. Temporary test key, empty preapproved catalog."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import zipfile
from contextlib import ExitStack

from live_v101_deployment import archive
from test_signed_release import Admission
from gs422.ota_wire import file_packets
from gs422.space_protocol import Decoder


def main():
    with ExitStack() as stack:
        t=Admission()
        stack.callback(t.doCleanups)
        t.setUp()
        for number,version in enumerate(('good','BAD'),1):
            d,entry=archive(t.root,stack,version)
            inner=gzip.decompress(d)
            digest=hashlib.sha256(inner).hexdigest()
            m=dict(format='sathex.satpkg',format_version=1,version=version,
                   signature=dict(algorithm='Ed25519',key_id=t.kid),
                   target=dict(board='WIAISAT-X26',soc='RK3588J',os='linux',architecture='arm64',
                               required_loader_capabilities=['satpkg-v1-ed25519']),
                   payload=dict(bytes=len(inner),name='release.tar',media_type='application/x-docker-image',
                                sha256=digest,image_id=entry['image_id']),algorithms=[])
            raw=json.dumps(m).encode()
            (t.root/'m').write_bytes(raw)
            sig=t.openssl('pkeyutl','-sign','-inkey',t.key,'-rawin','-in',t.root/'m')
            path=t.root/'signed.zip'
            with zipfile.ZipFile(path,'w') as z:
                for n,b in [('manifest.json',raw),('manifest.sig',sig),('release.tar',inner)]:z.writestr(n,b)
            t.sw.runner=t.sw._run
            t.stage.gate(True)
            assert t.rt.run_ota('begin',dict(component=number+2,region=number,mode=1,segmented=0,segments=0))==0
            for f in file_packets(path.read_bytes(),'XMODEM',ota101=True):
                assert t.stage.receive(Decoder().feed(f)[0])
            assert digest not in t.sw.catalog
            assert t.rt.run_ota('end',{'aborted':False})==0
            before=t.sw.version()
            assert before==('UNCONFIGURED' if number==1 else 'good')
            ack=t.rt.run_ota('load',{'index':0})
            assert ack==(0 if number==1 else 255)
            assert t.sw.version()=='good' and t.sw._state(t.sw.software)['Running']
            assert len(t.sw.package_index.entries)==1
            print('PASS signed',version,'Z54 candidate only; Z04',ack,'active good',flush=True)
        t.sw.cleanup()


if __name__=='__main__':
    main()
