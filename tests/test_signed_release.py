import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

from gs422.core import Cache
from gs422.durable_demo import DurableDemo
from gs422.ota_staging import Staging
from gs422.ota_wire import file_packets
from gs422.revised_runtime import Revised
from gs422.space_protocol import Decoder
from gs422.task_scheduler import Scheduler
from gs422 import tmg


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('openssl'),'Linux board loader')
class Admission(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.key=self.root/'private'
        self.pub=self.root/'preinstalled.pem'
        self.openssl('genpkey','-algorithm','Ed25519','-out',self.key)
        self.openssl('pkey','-in',self.key,'-pubout','-out',self.pub)
        der=self.openssl('pkey','-in',self.key,'-pubout','-outform','DER')
        self.kid='sha256:'+hashlib.sha256(der).hexdigest()
        layer=b'example layer'
        config=json.dumps(dict(os='linux',architecture='arm64',rootfs={'diff_ids':['sha256:'+hashlib.sha256(layer).hexdigest()]})).encode()
        self.image='sha256:'+hashlib.sha256(config).hexdigest()
        self.decoder='sha256:'+'a'*64
        out=io.BytesIO()
        with tarfile.open(fileobj=out,mode='w') as t:
            for name,data in [('manifest.json',b'[{"Config":"config","Layers":["layer"]}]'),
                              ('config',config),('layer',layer)]:
                m=tarfile.TarInfo(name);m.size=len(data);t.addfile(m,io.BytesIO(data))
        self.inner=out.getvalue()
        self.digest=hashlib.sha256(self.inner).hexdigest()
        self.profiles={'0':{'capture_dir':str(self.root)}}
        self.stage=Staging(self.root/'stage',crc_profile='XMODEM',capacity=100000)
        self.addCleanup(self.stage.close)
        self.open()
        self.addCleanup(lambda:self.rt.close())
        self.addCleanup(lambda:self.sw.close())

    def openssl(self,*args):
        return subprocess.check_output(['openssl',*args],stderr=subprocess.DEVNULL)

    def open(self):
        def runner(*args):
            if args[:2]==('image','inspect'):return self.decoder+' linux/arm64'
            return ''
        self.sw=DurableDemo(self.root/'sw',{},runner,publisher_key=self.pub,profiles=self.profiles)
        self.rt=Revised(self.stage,Cache(10),dict.fromkeys(tmg.BY_ID,0),version='bench',ota101=True,software_backend=self.sw)

    def bundle(self,issue=''):
        payload=self.inner
        m=dict(format='sathex.satpkg',format_version=1,version='v1',
               signature=dict(algorithm='Ed25519',key_id=self.kid),
               target=dict(board='WIAISAT-X26',soc='RK3588J',architecture='arm64',os='linux',
                           required_loader_capabilities=['satpkg-v1-ed25519']),
               payload=dict(bytes=len(payload),name='release.tar',media_type='application/x-docker-image',
                            sha256=self.digest,image_id=self.image),
               algorithms=[dict(app_id=0,technology='LTE',release_image_id=self.image,decoder_image_id=self.decoder)])
        if issue=='architecture':m['target']['architecture']='amd64'
        if issue=='dependency':m['algorithms'][0]['decoder_image_id']='sha256:'+'b'*64
        raw=json.dumps(m).encode()
        message=self.root/'message';message.write_bytes(raw)
        sig=self.openssl('pkeyutl','-sign','-inkey',self.key,'-rawin','-in',message)
        if issue=='signature':sig=b'x'*64
        if issue=='manifest':raw=raw.replace(b'"v1"',b'"v2"')
        if issue=='payload':payload=payload[:-1]+b'x'
        blob=io.BytesIO()
        with zipfile.ZipFile(blob,'w') as z:
            for n,d in [('manifest.json',raw),('manifest.sig',sig),('release.tar',payload)]:
                z.writestr(n,d)
        return blob.getvalue()

    def upload(self,data):
        self.stage.gate(True)
        self.assertEqual(self.rt.run_ota('begin',dict(component=3,region=1,mode=1,segmented=0,segments=0)),0)
        for msg in file_packets(data,'XMODEM',ota101=True):
            self.assertTrue(self.stage.receive(Decoder().feed(msg)[0]))
        return self.rt.run_ota('end',{'aborted':False})

    def test_reject_before_registration_and_block_load(self):
        for issue in ('signature','manifest','payload','architecture','dependency'):
            with self.subTest(issue=issue):
                self.assertEqual(self.upload(self.bundle(issue)),255)
                self.assertEqual(self.stage.state['status'],0x40)
                self.assertEqual(self.sw.catalog,{})
                self.assertEqual(self.rt.run_ota('load',{'index':0}),255)

    def test_reopen_and_raw_inner_has_no_automatic_trust(self):
        data=self.bundle()
        self.assertEqual(self.upload(data),0)
        self.assertEqual(self.rt.run_ota('end',{'aborted':False}),0)
        self.assertEqual(self.sw.version(),'UNCONFIGURED')
        self.assertEqual(self.sw.package_index.entries,{})
        self.rt.close();self.sw.close();self.open()
        self.assertEqual(self.sw.catalog[self.digest]['version'],'v1')
        self.assertEqual(self.sw.signed_bindings[self.image],{0:self.decoder})
        self.assertIsNone(Scheduler(self.sw,self.profiles)._active_capture(self.profiles['0'],0))
        self.assertEqual(self.upload(self.inner),0)  # raw CRC still valid
        self.assertEqual(self.rt.run_ota('load',{'index':0}),255)
