import unittest
from gs422.core import parse
from gs422.docker import Docker, DockerError, text

ID = 'a'*64


class FakeDocker(Docker):
    def __init__(self, allowed=()):
        super().__init__(allowed=allowed)
        self.calls=[]
        self.daemon_version={'Version':'29.1.3','ApiVersion':'1.52','MinAPIVersion':'1.44'}
        self.info={'Id':ID,'Name':'/test','Config':{'Image':'test:latest'},'RestartCount':0,
                   'State':{'Status':'running','Running':True,'ExitCode':0,'OOMKilled':False}}
    def request(self, method, path, timeout=3):
        self.calls.append((method,path))
        if path=='/version':return self.daemon_version
        if method=='POST':return None
        if '/stats?' in path:
            return {'cpu_stats':{'cpu_usage':{'total_usage':300},'system_cpu_usage':5000,'online_cpus':8},
                    'precpu_stats':{'cpu_usage':{'total_usage':100},'system_cpu_usage':4000},
                    'memory_stats':{'usage':1024,'limit':4096}}
        if '/containers/json?' in path:
            return [{'Id':ID,'Names':['/test'],'Image':'test:latest','State':'running'}]
        if path.endswith('/images/json'):
            return [{'Id':'sha256:'+ID,'RepoTags':None,'Size':123}]
        if path.endswith('/json'):return self.info
        return {'Containers':1,'ContainersRunning':1,'ContainersPaused':0,'ContainersStopped':0,'Images':1}


class DockerTests(unittest.TestCase):
    def test_parser(self):
        for command in ('AT+DOCKER','AT+DOCKER?'):
            self.assertEqual(parse(command.encode()),('DOCKER',('STATUS','')))
        self.assertEqual(parse(b'AT+DOCKER=LIST'),('DOCKER',('LIST','0')))
        self.assertEqual(parse(b'AT+DOCKER=STOP,test'),('DOCKER_CONTROL',('STOP','test')))
        for command in ('AT+DOCKER=EXEC,test','AT+DOCKER=START,../test','AT+DOCKER=STOP,test;id',
                        'AT+DOCKER=IMAGES,-1','AT+DOCKER=START,test,extra'):
            self.assertEqual(parse(command.encode())[0],'DOCKER_ERR')

    def test_empty_allowlist_never_mutates(self):
        client=FakeDocker()
        with self.assertRaisesRegex(DockerError,'NOT_ALLOWED'):client.execute('STOP','test')
        self.assertFalse(any(method=='POST' for method,_ in client.calls))

    def test_exact_target_and_immutable_id(self):
        client=FakeDocker(['test'])
        with self.assertRaisesRegex(DockerError,'EXACT_TARGET'):client.execute('START','aaaa')
        self.assertTrue(client.execute('START','test').endswith(',OK'))
        self.assertEqual(client.calls[-1],('POST','/v1.44/containers/'+ID+'/start'))
        self.assertTrue(FakeDocker([ID]).execute('STOP',ID).endswith(',OK'))

    def test_api_negotiation_and_conflict(self):
        client=FakeDocker(['test'])
        client.daemon_version['MinAPIVersion']='1.45'
        with self.assertRaisesRegex(DockerError,'API_VERSION'):client.execute('STATUS','')
        client=FakeDocker(['test']); client.info['State']['Paused']=True
        with self.assertRaisesRegex(DockerError,'CONFLICT'):client.execute('RESTART','test')

    def test_paging_stats_and_encoded_output(self):
        client=FakeDocker()
        self.assertIn('STATE=READY',client.execute('STATUS',''))
        self.assertIn('NAME=test',client.execute('LIST','0'))
        self.assertIn('END=1',client.execute('LIST','1'))
        self.assertIn('TAG=-,TAG_COUNT=0,SIZE_BYTES=123',client.execute('IMAGES','0'))
        self.assertIn('CPU=160.0%,MEM_USAGE_BYTES=1024,MEM_LIMIT_BYTES=4096',client.execute('STATS','test'))
        self.assertEqual(text('a,b\r\n'),'a%2Cb%0D%0A')

    def test_mutation_timeout_never_retries(self):
        class TimeoutDocker(FakeDocker):
            def request(self,method,path,timeout=3):
                result=super().request(method,path,timeout)
                if method=='POST':raise TimeoutError()
                return result
        client=TimeoutDocker(['test'])
        with self.assertRaisesRegex(DockerError,'OUTCOME_UNKNOWN'):client.execute('RESTART','test')
        self.assertEqual(sum(method=='POST' for method,_ in client.calls),1)


if __name__=='__main__':unittest.main()
