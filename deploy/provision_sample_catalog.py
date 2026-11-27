#!/usr/bin/env python3
"""Explicitly provision local, offline CPU samples into the installed LAB package.

Refuses an existing nonempty catalog. Does not enable or switch UART services.
Leaves a stopped, verified v1 baseline and registered initial package F0 for later
operator-controlled tests. No Ubuntu update, image pull, mounts or external IO.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import uuid

sys.path.insert(0,'/usr/lib/gs422-payload-lab')
from gs422.durable_demo import DurableDemo
from gs422.ota_staging import Staging

SOURCE=r'''
#include <stdio.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
volatile sig_atomic_t running=1;
void stop(int n){running=0;}
int main(int argc,char **argv){
 setbuf(stdout,NULL);signal(SIGTERM,stop);
 if(argc>1 && !strcmp(argv[1],"algorithm")){
  long sum=0;for(int i=1;i<=100;i++)sum+=i;
  printf("SUM=%ld\n",sum);
  for(int i=0;running && i<100;i++)usleep(100000);
  return 0;
 }
 printf("READY:%s\n",RELEASE);
 while(running)usleep(100000);
 return 0;
}
'''


def atomic_json(path,value):
    fd,name=tempfile.mkstemp(prefix='.catalog-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:json.dump(value,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        os.chmod(name,0o600);os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(name):os.unlink(name)


def main():
    if os.geteuid():raise SystemExit('explicit sudo required')
    config=Path('/etc/gs422-payload-lab.json');original=config.read_bytes();c=json.loads(original)
    if c['mode']!='lab' or c['catalog']:raise SystemExit('refusing to replace existing catalog or non-lab config')
    root=Path(c['state_root']);software_root=root/'software'
    if (software_root/'state.json').exists() or (software_root/'package-index.json').exists():
        raise SystemExit('existing software state requires operator review')
    root.mkdir(parents=True,exist_ok=True)
    proof=root/('provision-'+uuid.uuid4().hex[:10]);proof.mkdir(mode=0o700)
    (proof/'config.before.json').write_bytes(original)
    tags=[];catalog={};digests={};backend=staging=None;configured=False
    def run(*args):return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT,timeout=60).strip()
    try:
        source=proof/'sample.c';source.write_text(SOURCE)
        packages=software_root/'packages';packages.mkdir(parents=True,exist_ok=True)
        for version in ('sample-v1','sample-v2'):
            binary=proof/('binary-'+version)
            run('gcc','-static','-Os','-s','-DRELEASE="'+version+'"',str(source),'-o',str(binary))
            layer=proof/(version+'-root.tar')
            with tarfile.open(layer,'w') as tar:
                data=binary.read_bytes();info=tarfile.TarInfo('sample');info.size=len(data);info.mode=0o755
                tar.addfile(info,io.BytesIO(data))
            tag='sathex-reference-'+proof.name+':'+version
            image=run('docker','import',str(layer),tag);tags.append(tag)
            archive=proof/(version+'.tar');run('docker','save','-o',str(archive),tag)
            data=archive.read_bytes();digest=hashlib.sha256(data).hexdigest()
            target=packages/(digest+'.tar')
            with target.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            catalog[digest]={'image_id':image,'version':version};digests[version]=digest
        fd=os.open(packages,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        backend=DurableDemo(software_root,catalog)
        backend.package_index.register_initial(0xf0,digests['sample-v1'])
        staging=Staging(root/'ota',crc_profile='CCITT-FALSE',capacity=c['ota_capacity'])
        assert backend.ota('load',{'index':0xf0},staging)==0
        assert backend.version()=='sample-v1'
        backend.execute('task_start',dict(task_id=1,priority=0,mode=1,npu=False))
        import time
        deadline=time.monotonic()+5
        while 'SUM=5050' not in run('docker','logs','--tail','2',backend.task):
            if time.monotonic()>deadline:raise RuntimeError('sample algorithm did not compute')
            time.sleep(.05)
        backend.execute('task_stop',{'task_id':1})
        backend.execute('software_stop',{})
        c['catalog']=catalog
        atomic_json(config,c)
        configured=True
        result={'mode':'lab','catalog_entries':len(catalog),'initial_index':'F0','version':backend.version(),
                'software_container':backend.software,'task_container':backend.task,'image_tags':tags,
                'algorithm_result':5050,'containers_running':False,'services_enabled':False,
                'state_root':str(root),'backup':str(proof/'config.before.json')}
        atomic_json(proof/'result.json',result)
        print(json.dumps(result,indent=2))
    except BaseException:
        # Do not erase existing directory trees on failure. Preserve evidence;
        # only stop/remove objects this invocation actually created.
        if configured and json.loads(config.read_text()).get('catalog')==catalog:
            atomic_json(config,json.loads(original))
        if backend:backend.cleanup()
        for tag in tags:
            subprocess.run(['docker','image','rm',tag],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        raise
    finally:
        if staging:staging.close()
        if backend:backend.close()


if __name__=='__main__':main()
