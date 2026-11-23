"""Persistent lifecycle for the explicit trusted-image LAB backend.

An interrupted activation rolls back to the last committed software container.
This does not implement arbitrary package signatures or physical power-loss
qualification. Root must be private to the service; one owner holds flock.
"""
import json
import hashlib
import os
from pathlib import Path
import tempfile
import shutil
from .demo_docker import DemoDocker
from .space_commands import CommandError
from .package_index import PackageIndex, ArchivedInput


class DurableDemo(DemoDocker):
    FIELDS=('prefix','owned','slots','active','release','software','task','task_id',
            'latest_completed','last_task_state','retained')

    def __init__(self,root,catalog,runner=None):
        import fcntl
        root=Path(root);root.mkdir(parents=True,exist_ok=True)
        self.owner=(root/'owner.lock').open('a')
        try:
            fcntl.flock(self.owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            super().__init__(root,catalog,runner)
            self.state_path=root/'state.json'
            self.package_index=PackageIndex(root,catalog)
            if self.state_path.exists():
                record=json.loads(self.state_path.read_text())
                if record.get('schema')!=1:raise ValueError('invalid state schema')
                self._restore(record['committed'])
                self._recover(record['phase'])
            self._save('idle')
        except BaseException:
            self.owner.close()
            raise

    def _view(self):
        return {key:getattr(self,key) for key in self.FIELDS}

    def _restore(self,state):
        import re
        if not re.fullmatch(r'sathex-demo-[0-9a-f]{12}',state['prefix']):raise ValueError('bad owner label')
        if any(not re.fullmatch(r'[0-9a-f]{64}',cid) for cid in state['owned']):raise ValueError('bad container id')
        if state['active'] not in (None,1,2) or state['retained'] not in (None,1,2):raise ValueError('bad slot')
        for key in ('software','task'):
            if state[key] is not None and state[key] not in state['owned']:raise ValueError('unowned reference')
        slots={int(k):v for k,v in state['slots'].items()}
        if set(slots)!={1,2}:raise ValueError('bad slots')
        for slot in slots.values():
            if slot is not None and slot not in self.catalog.values():raise ValueError('package no longer trusted')
        for key in self.FIELDS:setattr(self,key,state[key])
        self.slots=slots

    def _save(self,phase):
        record={'schema':1,'phase':phase,'committed':self._view()}
        fd,name=tempfile.mkstemp(prefix='.state-',dir=self.root)
        try:
            with os.fdopen(fd,'w') as f:
                json.dump(record,f);f.flush();os.fsync(f.fileno())
            os.replace(name,self.state_path)
            directory=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(name):os.unlink(name)

    def _inspect_owned(self,cid):
        info=json.loads(self.runner('inspect',cid))[0]
        if info['Id']!=cid or info['Config'].get('Labels',{}).get('sathex.demo')!=self.prefix:
            raise CommandError(7)
        return info

    def _state(self,cid):
        if cid not in self.owned:raise CommandError(7)
        return self._inspect_owned(cid)['State']

    def _recover(self,phase):
        if phase not in ('idle','activation','action','cleanup'):raise ValueError('bad transaction phase')
        found=self.runner('ps','-aq','--no-trunc','--filter','label=sathex.demo='+self.prefix).split()
        infos={cid:self._inspect_owned(cid) for cid in found}
        if phase=='cleanup':
            for cid in found:self.runner('rm','-f',cid)
            self.owned=[];self.software=self.task=None;self.active=self.retained=None
            self.slots={1:None,2:None};self.release='UNCONFIGURED'
            return
        if self.software:
            if self.software not in infos:raise CommandError(8)
            expected=self.slots[self.active]['image_id']
            if infos[self.software]['Image']!=expected:raise CommandError(7)
        # A process can die between docker create and persisting the returned ID.
        # Discover only its own label, never sweep other deployment containers.
        for cid in found:
            if cid not in self.owned:self.runner('rm','-f',cid)
        self.owned=[cid for cid in self.owned if cid in infos]
        if self.task not in self.owned:self.task=None;self.last_task_state=2
        if phase in ('activation','action'):
            # Interrupted work is stopped, never replayed as an algorithm request.
            if self.task:
                if infos[self.task]['State'].get('Paused'):self.runner('unpause',self.task)
                if infos[self.task]['State']['Running']:self.runner('stop','-t','2',self.task)
                self.last_task_state=2
            if self.software:
                self.runner('start',self.software)
                self._health(self.software,self.release)

    def ota(self,action,args,staging):
        with self.lock:
            if self.task and self._state(self.task)['Running']:
                return 1
            if (action == 'load' and args.get('index') == 0 and staging is not None
                    and staging.state.get('status') in (0, 0x31) and self.active is not None
                    and staging.state.get('loaded_image') == self.slots[self.active]['image_id']
                    and staging.state.get('loaded_file') == staging.state.get('file_id') and self.software):
                state = self._state(self.software)
                if state['Running'] and not state.get('Paused'):
                    return 0
            if action=='load' and args.get('index',0)!=0:
                try:
                    path=self.package_index.resolve(args['index'])
                except (ValueError,OSError):return 0xff
                entry=self.catalog[self.package_index.entries[str(args['index'])]]
                if self.active is not None and self.slots[self.active]==entry and self.software:
                    state=self._state(self.software)
                    if state['Running'] and not state.get('Paused'):
                        if staging is not None:
                            staging.state['status']=0x31;staging._save()
                        return 0
                region=2 if self.active==1 else 1
                staging=ArchivedInput(path,region,staging)
                args={'index':0}
            if action=='erase' and args.get('scope')==1:
                region=args['region']
                if region not in (1,2) or region==self.active:return 0xff
                # Scope1 deletes only the service-owned candidate archive, not
                # the installed retained slot/container. No recursive delete.
                directory=self.root/('candidate-'+str(region))
                if directory.is_symlink():return 0xff
                if directory.exists():
                    entries=list(directory.iterdir())
                    if any(p.name not in ('package.tar',) or p.is_symlink() or not p.is_file() for p in entries):
                        return 0xff
                    for p in entries:p.unlink()
                    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(fd)
                    finally:os.close(fd)
                return 0
            if action=='erase' and args.get('scope')==0:
                return self._erase_installed(args['region'])
            if action=='load' and staging.state.get('status')==0x23:
                request=staging.state.get('request')
                if request and (request[0],request[1]) in ((3,1),(4,2)) and request[1]!=self.active:
                    self._cache_candidate(staging,request[1])
            self._save('activation')
            try:
                result=super().ota(action,args,staging)
            except BaseException:
                # Keep the committed pre-operation snapshot for a fresh process.
                raise
            self._save('idle')
            if action=='load' and result==0 and self.active is not None:
                # Register only a health-confirmed release, not every uploaded tar.
                digest=self.archived_package(self.slots[self.active]).stem
                self.package_index.register_history(digest)
                if 'file_id' in staging.state:
                    staging.state.update(loaded_file=staging.state['file_id'], loaded_image=self.slots[self.active]['image_id'])
                    staging._save()
            return result

    def _cache_candidate(self,staging,region):
        directory=self.root/('candidate-'+str(region))
        if directory.is_symlink():raise CommandError(7)
        directory.mkdir(exist_ok=True)
        temp=directory/('upload-'+__import__('uuid').uuid4().hex+'.tar')
        try:
            staging.export_verified(temp)
            h=hashlib.sha256()
            with temp.open('rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
            if h.hexdigest() not in self.catalog:raise CommandError(7)
            archive_dir=self.root/'packages'
            if archive_dir.is_symlink():raise CommandError(7)
            archive_dir.mkdir(exist_ok=True)
            archive=archive_dir/(h.hexdigest()+'.tar')
            if archive.exists():
                if archive.is_symlink() or self._digest(archive)!=h.hexdigest():raise CommandError(7)
            else:
                # Immutable trusted recovery copy independent of replaceable candidate.
                fd,name=tempfile.mkstemp(prefix='.package-',dir=archive_dir)
                try:
                    with os.fdopen(fd,'wb') as output,temp.open('rb') as source:
                        shutil.copyfileobj(source,output);output.flush();os.fsync(output.fileno())
                    os.replace(name,archive)
                    fd=os.open(archive_dir,os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(fd)
                    finally:os.close(fd)
                finally:
                    if os.path.exists(name):os.unlink(name)
            os.replace(temp,directory/'package.tar')
            fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:temp.unlink(missing_ok=True)

    def activate_legacy(self, upload):
        """D0351 supplies bytes only. 04H/00 explicitly activates the verified package."""
        with self.lock, upload.lock:
            if upload.state['status'] != 0x23:
                return 0xff
            with tempfile.TemporaryDirectory(dir=self.root) as temp:
                archive = Path(temp) / 'legacy.tar'
                upload.export_verified(archive)
                entry = self.catalog.get(self._digest(archive))
                if entry is None:
                    return 0xff
                if self.active and self.slots[self.active] == entry and self.software:
                    state = self._state(self.software)
                    if state['Running'] and not state.get('Paused'):
                        return 0
                region = 2 if self.active == 1 else 1
                return self.ota('load', {'index': 0}, ArchivedInput(archive, region, None))

    @staticmethod
    def _digest(path):
        h=hashlib.sha256()
        with Path(path).open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
        return h.hexdigest()

    def archived_package(self, entry):
        """Local trusted resolver, not a wire path or implicit history index."""
        matches=[digest for digest,value in self.catalog.items() if value==entry]
        for digest in matches:
            path=self.root/'packages'/(digest+'.tar')
            if not path.is_symlink() and path.is_file() and self._digest(path)==digest:return path
        raise CommandError(7)

    def _erase_installed(self, region):
        if region not in (1,2) or region==self.active:return 0xff
        entry=self.slots[region]
        if entry is None:return self.ota('erase',{'region':region,'scope':1},None)
        # Never drop the sole old release unless its exact trusted archive survives.
        try:self.archived_package(entry)
        except (CommandError,OSError):return 0xff
        candidates=[]
        for cid in self.owned:
            info=self._inspect_owned(cid)
            if info['Image']==entry['image_id'] and info['Config'].get('Cmd')==['service']:
                if cid in (self.software,self.task) or info['State']['Running']:return 0xff
                candidates.append(cid)
        if self.ota('erase',{'region':region,'scope':1},None):return 0xff
        # Precommit marks slot absent; interrupted deletion leaves only stopped
        # labelled orphan containers, removed by the existing recovery sweep.
        for cid in candidates:self.owned.remove(cid)
        self.slots[region]=None
        if self.retained==region:self.retained=None
        self._save('idle')
        for cid in candidates:self.runner('rm',cid)
        return 0

    def execute(self,action,parameters):
        with self.lock:
            self._save('action')
            result=super().execute(action,parameters)
            self._save('idle')
            return result

    def cleanup(self):
        with self.lock:
            self._save('cleanup')
            for cid in list(self.owned):
                self._inspect_owned(cid)
                self.runner('rm','-f',cid);self.owned.remove(cid)
            self.software=self.task=None;self.active=self.retained=None
            self.slots={1:None,2:None};self.release='UNCONFIGURED'
            self._save('idle')

    def close(self):
        # Explicit close releases process ownership, not deployed containers.
        self.owner.close()
