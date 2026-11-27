"""X26 satpkg v1. Trust is rooted in a locally provisioned Ed25519 SPKI."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import zipfile



def require(condition):
    if not condition:
        raise ValueError('invalid signed release')


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def unique(fields):
    obj = dict(fields)
    require(len(obj) == len(fields))
    return obj


def verify_manifest(raw, signature, key, work):
    require(len(signature) == 64 and len(raw) <= 32768)
    der = subprocess.check_output(['openssl','pkey','-pubin','-in',str(key),'-outform','DER'],
                                  stderr=subprocess.DEVNULL,timeout=5)
    require(len(der)==44 and der.startswith(bytes.fromhex('302a300506032b6570032100')))
    message, sig = work/'manifest', work/'signature'
    message.write_bytes(raw)
    sig.write_bytes(signature)
    subprocess.run(['openssl','pkeyutl','-verify','-pubin','-inkey',str(key),'-rawin',
                    '-in',str(message),'-sigfile',str(sig)],check=True,timeout=5,
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    m=json.loads(raw,object_pairs_hook=unique)
    require(m['format']=='sathex.satpkg' and type(m['format_version']) is int and m['format_version']==1)
    require(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}',m['version']))
    require(m['signature']=={'algorithm':'Ed25519','key_id':'sha256:'+hashlib.sha256(der).hexdigest()})
    target=m['target']
    require((target['board'],target['soc'],target['architecture'],target['os'])==
            ('WIAISAT-X26','RK3588J','arm64','linux'))
    caps=target['required_loader_capabilities']
    require(isinstance(caps,list) and set(caps)<={'satpkg-v1-ed25519','capture_job'})
    p=m['payload']
    require(p['name']=='release.tar' and p['media_type']=='application/x-docker-image' and
            re.fullmatch('[0-9a-f]{64}',p['sha256']) and re.fullmatch('sha256:[0-9a-f]{64}',p['image_id']))
    require(type(p['bytes']) is int and p['bytes']>0)
    apps=m['algorithms']
    require(isinstance(apps,list) and len(apps)<=2)
    require(len({a['app_id'] for a in apps})==len(apps))
    for a in apps:
        require(type(a['app_id']) is int and a['app_id'] in (0,1)
                and a['technology']==('LTE','NR')[a['app_id']]
                and a['release_image_id']==p['image_id']
                and re.fullmatch('sha256:[0-9a-f]{64}',a['decoder_image_id']))
    return m


def validate_image(path, image_id):
    # Read only Docker-save metadata. Docker also checks layers on load.
    with tarfile.open(path,'r:') as archive:
        records=archive.getmembers()
        require(len(records)<=4096 and len({r.name for r in records})==len(records))
        def read(name):
            item=archive.getmember(name)
            require(item.isfile() and item.size<=1024*1024)
            return archive.extractfile(item).read()
        descriptor,=json.loads(read('manifest.json'))
        config=read(descriptor['Config'])
        require('sha256:'+hashlib.sha256(config).hexdigest()==image_id)
        info=json.loads(config)
        require(info['architecture']=='arm64' and info['os']=='linux')
        layers=descriptor['Layers']
        expected=info['rootfs']['diff_ids']
        require(layers and len(layers)==len(expected))
        for name,digest in zip(layers,expected):
            item=archive.getmember(name)
            require(item.isfile())
            h=hashlib.sha256()
            with archive.extractfile(item) as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    h.update(chunk)
            require('sha256:'+h.hexdigest()==digest)


class SignedReleases:
    def __init__(self, root, key, profiles, catalog):
        self.root,self.key,self.profiles,self.catalog=root,Path(key),profiles,catalog
        self.bindings={}
        self.db=sqlite3.connect(root/'signed-releases.db',check_same_thread=False)
        try:
            self.db.execute('CREATE TABLE IF NOT EXISTS releases(digest TEXT PRIMARY KEY, manifest BLOB, signature BLOB)')
            with tempfile.TemporaryDirectory(dir=root) as work:
                for digest,raw,sig in self.db.execute('SELECT digest,manifest,signature FROM releases'):
                    m=verify_manifest(raw,sig,self.key,Path(work))
                    require(m['payload']['sha256']==digest)
                    self._remember(m)
        except BaseException:
            self.db.close()
            raise

    def _remember(self,m):
        p=m['payload']
        self.catalog[p['sha256']]={'version':m['version'],'image_id':p['image_id']}
        self.bindings[p['image_id']]={a['app_id']:a['decoder_image_id'] for a in m['algorithms']}

    def admit(self, bundle, work, capacity, runner):
        with zipfile.ZipFile(bundle) as z:
            require(sorted(z.namelist())==['manifest.json','manifest.sig','release.tar'])
            for name,limit in [('manifest.json',32768),('manifest.sig',64),('release.tar',capacity)]:
                item=z.getinfo(name)
                require(item.compress_type==zipfile.ZIP_STORED and 0<item.file_size<=limit)
            raw,sig=z.read('manifest.json'),z.read('manifest.sig')
            m=verify_manifest(raw,sig,self.key,work)
            p=m['payload']
            require(z.getinfo('release.tar').file_size==p['bytes'])
            for a in m['algorithms']:
                entry=self.profiles.get(str(a['app_id']),{})
                capture=entry.get('capture_dir') or entry.get('capture_job',{}).get('capture')
                require(capture and Path(capture).is_dir())
                identity=runner('image','inspect','--format','{{.Id}} {{.Os}}/{{.Architecture}}',a['decoder_image_id'])
                require(identity==a['decoder_image_id']+' linux/arm64')
            payload=work/'release.tar'
            with z.open('release.tar') as src,payload.open('xb') as dst:
                shutil.copyfileobj(src,dst)
                dst.flush()
                os.fsync(dst.fileno())
        digest=digest_file(payload)
        require(digest==p['sha256'])
        validate_image(payload,p['image_id'])
        for d,entry in self.catalog.items():
            if entry['version']==m['version'] or entry['image_id']==p['image_id'] or d==digest:
                require(d==digest and entry=={'version':m['version'],'image_id':p['image_id']})
        prior=self.db.execute('SELECT manifest FROM releases WHERE digest=?',(digest,)).fetchone()
        require(not prior or json.loads(prior[0])==m)
        directory=self.root/'packages'
        directory.mkdir(exist_ok=True)
        target=directory/(digest+'.tar')
        if target.exists():
            require(not target.is_symlink() and digest_file(target)==digest)
        else:
            os.replace(payload,target)
        for folder in (directory,self.root):
            fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO releases VALUES(?,?,?)',(digest,raw,sig))
        self._remember(m)
        return digest
