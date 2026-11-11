"""IPv4 changes with a NetworkManager-owned rollback timer.

Only the two mapped Ethernet devices are configurable. Unplugged interfaces are
staged in memory in this process and written only on explicit confirmation.
"""
import ipaddress
import socket
import struct
import time
import uuid
from .admin_protocol import AdminError
from .network import NMClient, NM, ROOT, DEVICE, ACTIVE, CONNECTION

SETTINGS = NM + '.Settings'


def typed_settings(client, path):
    value = client.call(path, CONNECTION, 'GetSettings', raw=True).get_child_value(0)
    result = {}
    for i in range(value.n_children()):
        group = value.get_child_value(i)
        entries = group.get_child_value(1)
        result[group.get_child_value(0).get_string()] = {
            entries.get_child_value(j).get_child_value(0).get_string():
            entries.get_child_value(j).get_child_value(1).get_variant()
            for j in range(entries.n_children())}
    return result


def plain(settings):
    return {group: {key: value.unpack() for key, value in entries.items()}
            for group, entries in settings.items()}


def configure(client, original, args):
    settings = {k: dict(v) for k, v in original.items()}
    v = client.GLib.Variant
    # Preserve IPv6 and Ethernet settings; replace IPv4 addressing/DNS/routes.
    ip = settings.setdefault('ipv4', {})
    for key in ('addresses', 'address-data', 'gateway', 'dns', 'dns-data',
                'dns-search', 'dns-options', 'routes', 'route-data', 'routing-rules'):
        ip.pop(key, None)
    manual = args[0] == 'STATIC'
    ip.update({'method': v('s', 'manual' if manual else 'auto'),
               'ignore-auto-dns': v('b', manual), 'ignore-auto-routes': v('b', manual),
               'never-default': v('b', manual and not args[3]), 'may-fail': v('b', False)})
    if manual:
        _, addr, prefix, gateway, dns = args
        ip['address-data'] = v('aa{sv}', [{'address': v('s', addr), 'prefix': v('u', prefix)}])
        if gateway:
            ip['gateway'] = v('s', gateway)
        # NM 1.36 on Ubuntu 22 accepts the legacy native-endian uint array.
        ip['dns'] = v('au', [struct.unpack('=I', socket.inet_aton(server))[0] for server in dns])
    settings['connection']['autoconnect'] = v('b', True)
    return settings


class IPAdmin:
    def __init__(self, stop, timeout=120, factory=NMClient, clock=time.monotonic):
        self.stop, self.timeout, self.factory, self.clock = stop, timeout, factory, clock
        self.pending = None

    def client(self):
        return self.factory(self.stop, 45)

    def expire(self):
        if self.pending and self.clock() >= self.pending['expires']:
            self.rollback()

    def busy(self):
        self.expire()
        return self.pending is not None

    def profile(self, c, device, name):
        # Veth (20) supports Ethernet profiles and enables isolated integration
        # tests. Production auto-mapping selects physical interfaces only.
        if c.get(device, DEVICE, 'DeviceType') not in (1, 20) or not c.get(device, DEVICE, 'Managed'):
            raise AdminError('NOT_MANAGED_ETHERNET')
        active = c.get(device, DEVICE, 'ActiveConnection')
        if active != '/':
            if c.get(device, DEVICE, 'State') != 100:
                raise AdminError('NOT_READY')
            path = c.get(active, ACTIVE, 'Connection')
            settings = typed_settings(c, path)
            return path, settings, True
        candidates, bound = [], []
        available = set(c.get(device, DEVICE, 'AvailableConnections'))
        # AvailableConnections may be empty with no cable, even for the existing
        # saved profile. Search explicitly bound profiles before creating one.
        paths = c.call('/org/freedesktop/NetworkManager/Settings', SETTINGS, 'ListConnections')[0]
        for path in paths:
            settings = typed_settings(c, path)
            connection = plain(settings).get('connection', {})
            if connection.get('type') != '802-3-ethernet':
                continue
            if connection.get('interface-name') == name:
                bound.append((path, settings, False))
            elif path in available and not connection.get('interface-name'):
                candidates.append((path, settings, False))
        candidates = bound or candidates
        if len(candidates) > 1:
            raise AdminError('AMBIGUOUS_PROFILE')
        if candidates:
            return candidates[0]
        v = c.GLib.Variant
        return None, {'connection': {'id': v('s', 'gs422-' + name),
                                    'uuid': v('s', str(uuid.uuid4())),
                                    'type': v('s', '802-3-ethernet'),
                                    'interface-name': v('s', name)},
                      '802-3-ethernet': {}, 'ipv6': {'method': v('s', 'auto')}}, False

    def rollback(self):
        p = self.pending
        if not p:
            return
        if p['checkpoint']:
            c = self.client()
            c.cleaning = True
            checkpoints = c.get(ROOT, NM, 'Checkpoints')
            if p['checkpoint'] in checkpoints:
                results = c.call(ROOT, NM, 'CheckpointRollback', '(o)', (p['checkpoint'],))[0]
                if any(result != 0 for result in results.values()):
                    raise AdminError('ROLLBACK_FAILED')
            # Rollback's D-Bus reply schedules reactivation; it does not mean the
            # kernel already has the restored addresses. Wait before replying OK.
            deadline = self.clock() + 10
            while True:
                try:
                    active = c.get(p['device'], DEVICE, 'ActiveConnection')
                    config = c.get(p['device'], DEVICE, 'Ip4Config')
                    addresses = c.get(config, NM + '.IP4Config', 'AddressData') if config != '/' else []
                    found = {(a['address'], a['prefix']) for a in addresses}
                    expected = p.get('original_addresses', set())
                    if (c.get(p['device'], DEVICE, 'State') == 100 and active != '/'
                            and c.get(active, ACTIVE, 'Connection') == p['connection']
                            and found and (not expected or expected <= found)):
                        break
                except Exception:
                    # ActiveConnection/IP4Config objects are replaced during
                    # reactivation, including between two consecutive reads.
                    pass
                if self.clock() >= deadline:
                    raise AdminError('ROLLBACK_PENDING')
                time.sleep(.1)
        self.pending = None

    def query(self, c, label, name):
        device = c.device(name)
        path, settings, active = self.profile(c, device, name)
        if self.pending and self.pending['label'] == label:
            settings = self.pending['settings']
        data = plain(settings).get('ipv4', {})
        mode = {'auto': 'DHCP', 'manual': 'STATIC'}.get(data.get('method'), 'UNSET')
        addresses = data.get('address-data', [])
        address = addresses[0]['address'] if addresses else '-'
        mask = str(ipaddress.IPv4Network('0.0.0.0/' + str(addresses[0]['prefix'])).netmask) if addresses else '-'
        live = c.get(device, DEVICE, 'Ip4Config')
        live_data = c.get(live, NM + '.IP4Config', 'AddressData') if live != '/' else []
        live_ip = live_data[0]['address'] if live_data else '-'
        dns = data.get('dns-data', [])
        # NM 1.36 returns legacy packed DNS on some profiles.
        if not dns and data.get('dns'):
            dns = [socket.inet_ntoa(struct.pack('=I', value)) for value in data['dns']]
        wait = max(0, int(self.pending['expires'] - self.clock())) if self.pending and self.pending['label'] == label else 0
        return (f'+{label}:CONFIG,IFACE={name},MODE={mode},ADDRESS={address},MASK={mask},'
                f'GATEWAY={data.get("gateway") or "-"},DNS={";".join(dns) or "-"},'
                f'LIVE_IP={live_ip},PENDING={int(wait > 0)},REMAIN_SEC={wait}')

    def execute(self, label, name, args):
        try:
            self.expire()
            if not name:
                raise AdminError('NO_INTERFACE')
            op = args[0]
            c = self.client()
            if op == 'CONFIG':
                return self.query(c, label, name)
            if op in ('CONFIRM', 'ROLLBACK'):
                if not self.pending or self.pending['label'] != label:
                    raise AdminError('NO_PENDING')
                if op == 'ROLLBACK':
                    self.rollback()
                    return f'+{label}:OK,ROLLED_BACK'
                return self.confirm(c, label)
            if self.pending:
                raise AdminError('BUSY')
            device = c.device(name)
            path, original, active = self.profile(c, device, name)
            # Avoid changing authenticated wired profiles without preserving secrets.
            if '802-1x' in original or plain(original).get('connection', {}).get('master'):
                raise AdminError('UNSUPPORTED_PROFILE')
            settings = configure(c, original, args)
            old_ip = plain(original).get('ipv4', {})
            original_addresses = {(a['address'], a['prefix']) for a in old_ip.get('address-data', [])} if old_ip.get('method') == 'manual' else set()
            checkpoint = c.call(ROOT, NM, 'CheckpointCreate', '(aouu)', ([device], self.timeout, 0))[0] if active else None
            self.pending = dict(label=label, name=name, device=device, connection=path,
                                settings=settings, checkpoint=checkpoint,
                                expires=self.clock() + self.timeout, active=active,
                                original_addresses=original_addresses)
            try:
                if active:
                    c.call(path, CONNECTION, 'Update2', '(a{sa{sv}}ua{sv})', (settings, 2, {}))
                    c.call(device, DEVICE, 'Reapply', '(a{sa{sv}}tu)', ({}, 0, 0))
                    while True:
                        config = c.get(device, DEVICE, 'Ip4Config')
                        addrs = c.get(config, NM + '.IP4Config', 'AddressData') if config != '/' else []
                        if addrs and c.get(device, DEVICE, 'State') == 100:
                            if ((op == 'DHCP' and c.get(device, DEVICE, 'Dhcp4Config') != '/')
                                    or (op == 'STATIC' and any(a['address'] == args[1] and a['prefix'] == args[2] for a in addrs))):
                                break
                        c.pause()
                remain = max(0, int(self.pending['expires'] - self.clock()))
                return f'+{label}:PENDING,IFACE={name},APPLIED={int(active)},CONFIRM_SEC={remain}'
            except Exception:
                self.rollback()
                raise
        except AdminError as error:
            return f'+{label}:ERR,{error.code}'
        except Exception:
            return f'+{label}:ERR,FAILED'

    def confirm(self, c, label):
        p = self.pending
        checkpoint = p['checkpoint']
        if checkpoint:
            if checkpoint not in c.get(ROOT, NM, 'Checkpoints'):
                self.pending = None
                raise AdminError('EXPIRED')
            c.call(ROOT, NM, 'CheckpointAdjustRollbackTimeout', '(ou)', (checkpoint, 30))
            active = c.get(p['device'], DEVICE, 'ActiveConnection')
            if active == '/' or c.get(active, ACTIVE, 'Connection') != p['connection']:
                self.rollback()
                raise AdminError('CONNECTION_CHANGED')
        elif c.get(p['device'], DEVICE, 'ActiveConnection') != '/':
            self.pending = None
            raise AdminError('CONNECTION_CHANGED')
        try:
            if p['connection']:
                # Persist exactly the staged settings, not another editor's changes.
                c.call(p['connection'], CONNECTION, 'Update2', '(a{sa{sv}}ua{sv})', (p['settings'], 1, {}))
            else:
                c.call('/org/freedesktop/NetworkManager/Settings', SETTINGS, 'AddConnection2',
                       '(a{sa{sv}}ua{sv})', (p['settings'], 1, {}))
            if checkpoint:
                c.call(ROOT, NM, 'CheckpointDestroy', '(o)', (checkpoint,))
            self.pending = None
            return f'+{label}:OK,SAVED,APPLIED={int(p["active"])}'
        except Exception:
            self.rollback()
            raise
