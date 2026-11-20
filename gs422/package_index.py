"""Stable package indexes owned by the same single-owner software manager.

Wire indexes never become filesystem paths. Initial packages require explicit
local registration; historical indexes are allocated once, never reordered.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile


class PackageIndex:
    def __init__(self,root,catalog):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'package-index.json';self.catalog=catalog
        self.entries=json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(self.entries,dict):raise ValueError('invalid package index')
        for key,digest in self.entries.items():
            index=int(key)
            if str(index)!=key or not self.allowed(index) or digest not in catalog:
                raise ValueError('invalid/untrusted package index')

    @staticmethod
    def allowed(index):return type(index) is int and (1<=index<=0x80 or 0xf0<=index<=0xff)

    def _save(self):
        fd,name=tempfile.mkstemp(prefix='.index-',dir=self.root)
        try:
            with os.fdopen(fd,'w') as f:json.dump(self.entries,f);f.flush();os.fsync(f.fileno())
            os.replace(name,self.path)
            fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:
            if os.path.exists(name):os.unlink(name)

    def resolve_digest(self,digest):
        import re
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest) or digest not in self.catalog:
            raise ValueError('untrusted digest')
        folder=self.root/'packages';p=folder/(digest+'.tar')
        if folder.is_symlink() or p.is_symlink() or not p.is_file():raise ValueError('missing recovery package')
        h=hashlib.sha256()
        with p.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        if h.hexdigest()!=digest:raise ValueError('package digest changed')
        return p

    def resolve(self,index):
        if not self.allowed(index) or str(index) not in self.entries:raise ValueError('unregistered package index')
        return self.resolve_digest(self.entries[str(index)])

    def register_initial(self,index,digest):
        if type(index) is not int or not 0xf0<=index<=0xff:raise ValueError('initial index required')
        self.resolve_digest(digest)
        key=str(index)
        if key in self.entries and self.entries[key]!=digest:raise ValueError('initial index cannot be overwritten')
        self.entries[key]=digest;self._save()

    def register_history(self,digest):
        self.resolve_digest(digest)
        for key,value in self.entries.items():
            if int(key)<=0x80 and value==digest:return int(key)
        index=next((i for i in range(1,0x81) if str(i) not in self.entries),None)
        if index is None:return None  # never evict/reassign an old ground reference
        self.entries[str(index)]=digest;self._save();return index


class ArchivedInput:
    """Adapt a verified registry object to existing inactive-slot install logic."""
    def __init__(self,path,region,telemetry_staging):
        self.path=Path(path);self.original=telemetry_staging
        self.state={'status':0x23,'request':[region+2,region,1,0,0]}

    def export_verified(self,target):
        import shutil
        with self.path.open('rb') as source,open(target,'xb') as dest:
            shutil.copyfileobj(source,dest);dest.flush();os.fsync(dest.fileno())

    def _save(self):
        if self.original is not None:
            self.original.state['status']=self.state['status']
            self.original._save()
