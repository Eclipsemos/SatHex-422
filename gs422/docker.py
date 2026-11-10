"""Small Docker Engine Unix-socket client. No shell execution or arbitrary API proxy."""
import argparse
import http.client
import json
import re
import socket
from urllib.parse import quote


class DockerError(Exception):
    pass


def target_valid(value):
    return bool(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', value))


def parse_command(line):
    if line in ('AT+DOCKER', 'AT+DOCKER?'):
        return ('STATUS', '')
    if not line.startswith('AT+DOCKER='):
        return None
    parts = line[10:].split(',')
    if len(parts) == 1 and parts[0] in ('LIST', 'IMAGES'):
        return (parts[0], '0')
    if len(parts) == 2:
        operation, target = parts
        if operation in ('LIST', 'IMAGES') and re.fullmatch(r'\d{1,6}', target):
            return operation, target
        if operation in ('INFO', 'STATS', 'START', 'STOP', 'RESTART') and target_valid(target):
            return operation, target
    raise ValueError('invalid Docker command')


def text(value, limit=160):
    # Percent encoding keeps names/tags incapable of injecting extra serial lines.
    return quote(str(value)[:limit], safe='._-/:')


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Docker:
    def __init__(self, path='/var/run/docker.sock', allowed=()):
        self.path, self.allowed = path, set(allowed)
        self.api = None
        self.version = None

    def request(self, method, path, timeout=3):
        connection = UnixHTTP(self.path, timeout)
        try:
            connection.request(method, path, body=b'' if method == 'POST' else None)
            response = connection.getresponse()
            if response.status not in (200, 201, 204, 304):
                raise DockerError({404:'NOT_FOUND', 409:'CONFLICT', 403:'DENIED',
                                   400:'BAD_REQUEST'}.get(response.status, 'ENGINE_ERROR'))
            payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                raise DockerError('TOO_LARGE')
            return json.loads(payload) if payload else None
        finally:
            connection.close()

    def negotiate(self):
        self.version = self.request('GET', '/version')
        def version(value):
            major, minor = value.split('.')
            return int(major), int(minor)
        chosen = min(version(self.version['ApiVersion']), (1, 44))
        if chosen < max(version(self.version.get('MinAPIVersion','1.24')), (1,24)):
            raise DockerError('API_VERSION')
        self.api = '/v%d.%d' % chosen

    def inspect(self, target):
        info = self.request('GET', self.api + '/containers/' + quote(target, safe='') + '/json')
        # Docker accepts ambiguous short IDs; our protocol accepts only full IDs or exact names.
        if target not in (info['Id'], info['Name'].lstrip('/')):
            raise DockerError('EXACT_TARGET_REQUIRED')
        return info

    def execute(self, operation, target):
        self.negotiate()
        if operation == 'STATUS':
            info = self.request('GET', self.api + '/info')
            return (f'+DOCKER:STATE=READY,VERSION={text(self.version["Version"])},'
                    f'CONTAINERS={info["Containers"]},RUNNING={info["ContainersRunning"]},'
                    f'PAUSED={info["ContainersPaused"]},STOPPED={info["ContainersStopped"]},IMAGES={info["Images"]}')
        if operation in ('LIST', 'IMAGES'):
            is_container = operation == 'LIST'
            entries = self.request('GET', self.api + ('/containers/json?all=true' if is_container else '/images/json'))
            entries.sort(key=lambda item:item['Id'])
            index = int(target)
            header = f'+DOCKER:{operation},INDEX={index},TOTAL={len(entries)}'
            if index >= len(entries):
                return header + ',END=1'
            item = entries[index]
            result = header + ',ID=' + text(item['Id'])
            if is_container:
                result += ',NAME=' + text((item.get('Names') or ['-'])[0].lstrip('/'))
                result += ',IMAGE=' + text(item.get('Image','-')) + ',STATE=' + text(item['State'])
            else:
                tags = item.get('RepoTags') or []
                result += ',TAG=' + text(tags[0] if tags else '-') + f',TAG_COUNT={len(tags)},SIZE_BYTES={item["Size"]}'
            return result + f',NEXT={index+1 if index+1<len(entries) else "-"}'
        info = self.inspect(target)
        identity = info['Id']
        state = info['State']
        if operation == 'INFO':
            return (f'+DOCKER:INFO,ID={identity},NAME={text(info["Name"].lstrip("/"))},'
                    f'IMAGE={text(info["Config"]["Image"])},STATE={text(state["Status"])},'
                    f'HEALTH={text(state.get("Health",{}).get("Status","NONE"))},'
                    f'EXIT_CODE={state["ExitCode"]},OOM_KILLED={int(state["OOMKilled"])},'
                    f'RESTART_COUNT={info["RestartCount"]}')
        if operation == 'STATS':
            if not state['Running']:
                raise DockerError('NOT_RUNNING')
            stats = self.request('GET', self.api + f'/containers/{identity}/stats?stream=false', 5)
            cpu = stats.get('cpu_stats', {})
            previous = stats.get('precpu_stats', {})
            usage = cpu.get('cpu_usage',{}).get('total_usage',0)-previous.get('cpu_usage',{}).get('total_usage',0)
            system = cpu.get('system_cpu_usage',0)-previous.get('system_cpu_usage',0)
            cores = cpu.get('online_cpus') or len(cpu.get('cpu_usage',{}).get('percpu_usage',[]))
            percentage = f'{usage/system*cores*100:.1f}%' if previous and system>0 and usage>=0 and cores else '-'
            memory = stats.get('memory_stats', {})
            return (f'+DOCKER:STATS,ID={identity},CPU={percentage},'
                    f'MEM_USAGE_BYTES={memory.get("usage","-")},MEM_LIMIT_BYTES={memory.get("limit","-")}')
        if operation not in ('START','STOP','RESTART'):
            raise DockerError('BAD_COMMAND')
        if identity not in self.allowed and info['Name'].lstrip('/') not in self.allowed:
            raise DockerError('NOT_ALLOWED')
        if state.get('Paused') or state.get('Restarting') or state.get('Dead'):
            raise DockerError('CONFLICT')
        # Capture the immutable ID so an external rename cannot redirect the action.
        path = self.api + '/containers/' + identity + '/' + operation.lower()
        if operation != 'START':
            path += '?t=10'
        try:
            self.request('POST', path, 13)
        except (TimeoutError, OSError, http.client.HTTPException):
            # A timed-out write may already have reached the daemon. Never auto-retry.
            raise DockerError('OUTCOME_UNKNOWN')
        return f'+DOCKER:{operation},ID={identity},OK'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True)
    parser.add_argument('--allow', default='')
    parser.add_argument('operation')
    parser.add_argument('target', nargs='?', default='')
    args = parser.parse_args()
    try:
        result = Docker(args.socket, args.allow.split(';')).execute(args.operation,args.target)
    except DockerError as error:
        result = '+DOCKER:ERR,' + str(error)
    except (OSError, http.client.HTTPException):
        result = '+DOCKER:ERR,UNAVAILABLE'
    except (ValueError, KeyError, TypeError):
        result = '+DOCKER:ERR,INVALID_RESPONSE'
    print(result)


if __name__ == '__main__':
    main()
