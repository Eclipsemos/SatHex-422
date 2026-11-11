"""Strict, hardware-independent parsers for machine configuration commands."""
import ipaddress
import re


class AdminError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def unicast(value):
    addr = ipaddress.IPv4Address(value)
    if addr.is_unspecified or addr.is_loopback or addr.is_link_local or addr.is_multicast or int(addr) >= 0xf0000000:
        raise ValueError('not a usable unicast address')
    return addr


def ip_arguments(value):
    if value in ('CONFIG', 'DHCP', 'CONFIRM', 'ROLLBACK'):
        return (value,)
    parts = value.split(',')
    if len(parts) != 5 or parts[0] != 'STATIC':
        raise ValueError('invalid IP command')
    _, address, mask, gateway, dns = parts
    addr = unicast(address)
    # Accept dotted netmasks only, avoiding IPv4Network's host-mask interpretation.
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', mask):
        raise ValueError('invalid mask')
    net = ipaddress.IPv4Network((address, mask), strict=False)
    if str(net.netmask) != mask or not 1 <= net.prefixlen <= 30 or addr in (net.network_address, net.broadcast_address):
        raise ValueError('invalid subnet or host address')
    if gateway != '-':
        gw = unicast(gateway)
        if gw not in net or gw in (addr, net.network_address, net.broadcast_address):
            raise ValueError('invalid gateway')
    servers = () if dns == '-' else tuple(dns.split(';'))
    if len(servers) > 3 or len(set(servers)) != len(servers):
        raise ValueError('invalid DNS servers')
    for server in servers:
        unicast(server)
    return ('STATIC', str(addr), net.prefixlen, '' if gateway == '-' else gateway, servers)


def ssh_arguments(value):
    if value in ('ON', 'OFF'):
        return (value,)
    parts = value.split(',')
    if len(parts) == 2 and parts[0] == 'PORT' and re.fullmatch(r'[1-9][0-9]{0,4}', parts[1]):
        port = int(parts[1])
        if port <= 65535:
            return ('PORT', port)
    raise ValueError('invalid SSH command')
