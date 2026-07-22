"""
Network Configuration Manager

Manages IPv6 addresses, routes, and neighbor tables on the Linux host.
Uses subprocess calls to `ip` commands for actual network configuration
and provides read-only mode when not running as root.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=10)


def _is_root() -> bool:
    return os.geteuid() == 0


@dataclass
class InterfaceInfo:
    name: str = ''
    index: int = 0
    mac: str = ''
    mtu: int = 1500
    state: str = 'DOWN'
    ipv6_addrs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'name': self.name, 'index': self.index, 'mac': self.mac,
            'mtu': self.mtu, 'state': self.state, 'ipv6_addrs': self.ipv6_addrs,
        }


@dataclass
class IPv6Route:
    destination: str = '::/0'
    next_hop: str = ''
    interface: str = ''
    metric: int = 1024
    protocol: str = ''

    def to_dict(self) -> dict:
        return {
            'destination': self.destination, 'next_hop': self.next_hop,
            'interface': self.interface, 'metric': self.metric,
            'protocol': self.protocol,
        }


@dataclass
class Neighbor:
    ipv6: str = ''
    mac: str = ''
    interface: str = ''
    state: str = ''

    def to_dict(self) -> dict:
        return {
            'ipv6': self.ipv6, 'mac': self.mac,
            'interface': self.interface, 'state': self.state,
        }


class NetworkConfigManager:

    def __init__(self):
        self._is_linux = os.path.exists('/proc/net')

    def list_interfaces(self) -> list[InterfaceInfo]:
        if not self._is_linux:
            return self._mock_interfaces()
        try:
            result = _run(['ip', '-j', '-6', 'addr', 'show'], check=False)
            if result.returncode != 0:
                return self._parse_interfaces_text()
            data = json.loads(result.stdout)
            interfaces = []
            for iface in data:
                info = InterfaceInfo(
                    name=iface.get('ifname', ''),
                    index=iface.get('ifindex', 0),
                    mac=iface.get('address', ''),
                    mtu=iface.get('mtu', 1500),
                    state=iface.get('operstate', 'UNKNOWN'),
                )
                for addr_info in iface.get('addr_info', []):
                    local = addr_info.get('local', '')
                    prefixlen = addr_info.get('prefixlen', 128)
                    if local:
                        info.ipv6_addrs.append(f'{local}/{prefixlen}')
                interfaces.append(info)
            return interfaces
        except (json.JSONDecodeError, FileNotFoundError):
            return self._parse_interfaces_text()

    def _parse_interfaces_text(self) -> list[InterfaceInfo]:
        try:
            result = _run(['ip', '-6', 'addr', 'show'], check=False)
            interfaces = []
            current = None
            for line in result.stdout.splitlines():
                m = re.match(r'^(\d+):\s+(\S+):', line)
                if m:
                    if current:
                        interfaces.append(current)
                    current = InterfaceInfo(index=int(m.group(1)), name=m.group(2))
                    if 'state UP' in line:
                        current.state = 'UP'
                    elif 'state DOWN' in line:
                        current.state = 'DOWN'
                    mtu_m = re.search(r'mtu\s+(\d+)', line)
                    if mtu_m:
                        current.mtu = int(mtu_m.group(1))
                elif current and 'link/ether' in line:
                    mac_m = re.search(r'link/ether\s+([\da-f:]+)', line)
                    if mac_m:
                        current.mac = mac_m.group(1)
                elif current and 'inet6' in line:
                    addr_m = re.search(r'inet6\s+(\S+)', line)
                    if addr_m:
                        current.ipv6_addrs.append(addr_m.group(1))
            if current:
                interfaces.append(current)
            return interfaces
        except FileNotFoundError:
            return self._mock_interfaces()

    def _mock_interfaces(self) -> list[InterfaceInfo]:
        return [InterfaceInfo(
            name='lo', index=1, mac='00:00:00:00:00:00', mtu=65536,
            state='UP', ipv6_addrs=['::1/128'],
        )]

    def add_ipv6_address(self, interface: str, address: str) -> dict:
        if not _is_root():
            return {'error': 'Root privileges required', 'command': f'ip -6 addr add {address} dev {interface}'}
        result = _run(['ip', '-6', 'addr', 'add', address, 'dev', interface], check=False)
        if result.returncode != 0:
            return {'error': result.stderr.strip()}
        return {'success': True, 'address': address, 'interface': interface}

    def remove_ipv6_address(self, interface: str, address: str) -> dict:
        if not _is_root():
            return {'error': 'Root privileges required'}
        result = _run(['ip', '-6', 'addr', 'del', address, 'dev', interface], check=False)
        if result.returncode != 0:
            return {'error': result.stderr.strip()}
        return {'success': True}

    def list_routes(self) -> list[IPv6Route]:
        if not self._is_linux:
            return []
        try:
            result = _run(['ip', '-j', '-6', 'route', 'show'], check=False)
            if result.returncode != 0:
                return self._parse_routes_text()
            routes = []
            for r in json.loads(result.stdout):
                route = IPv6Route(
                    destination=r.get('dst', '::/0'),
                    next_hop=r.get('gateway', ''),
                    interface=r.get('dev', ''),
                    metric=r.get('metric', 0),
                    protocol=r.get('protocol', ''),
                )
                routes.append(route)
            return routes
        except (json.JSONDecodeError, FileNotFoundError):
            return self._parse_routes_text()

    def _parse_routes_text(self) -> list[IPv6Route]:
        try:
            result = _run(['ip', '-6', 'route', 'show'], check=False)
            routes = []
            for line in result.stdout.splitlines():
                parts = line.split()
                if not parts:
                    continue
                route = IPv6Route(destination=parts[0])
                for i, p in enumerate(parts):
                    if p == 'via' and i + 1 < len(parts):
                        route.next_hop = parts[i + 1]
                    elif p == 'dev' and i + 1 < len(parts):
                        route.interface = parts[i + 1]
                    elif p == 'metric' and i + 1 < len(parts):
                        route.metric = int(parts[i + 1])
                    elif p == 'proto' and i + 1 < len(parts):
                        route.protocol = parts[i + 1]
                routes.append(route)
            return routes
        except FileNotFoundError:
            return []

    def add_route(self, destination: str, next_hop: str = '', interface: str = '',
                  metric: int = 1024) -> dict:
        if not _is_root():
            return {'error': 'Root privileges required'}
        cmd = ['ip', '-6', 'route', 'add', destination]
        if next_hop:
            cmd += ['via', next_hop]
        if interface:
            cmd += ['dev', interface]
        cmd += ['metric', str(metric)]
        result = _run(cmd, check=False)
        if result.returncode != 0:
            return {'error': result.stderr.strip()}
        return {'success': True}

    def remove_route(self, destination: str, next_hop: str = '', interface: str = '') -> dict:
        if not _is_root():
            return {'error': 'Root privileges required'}
        cmd = ['ip', '-6', 'route', 'del', destination]
        if next_hop:
            cmd += ['via', next_hop]
        if interface:
            cmd += ['dev', interface]
        result = _run(cmd, check=False)
        if result.returncode != 0:
            return {'error': result.stderr.strip()}
        return {'success': True}

    def list_neighbors(self) -> list[Neighbor]:
        if not self._is_linux:
            return []
        try:
            result = _run(['ip', '-j', '-6', 'neigh', 'show'], check=False)
            if result.returncode != 0:
                return []
            neighbors = []
            for n in json.loads(result.stdout):
                neighbors.append(Neighbor(
                    ipv6=n.get('dst', ''),
                    mac=n.get('lladdr', ''),
                    interface=n.get('dev', ''),
                    state=' '.join(n.get('state', [])) if isinstance(n.get('state'), list) else str(n.get('state', '')),
                ))
            return neighbors
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def add_neighbor(self, ipv6: str, mac: str, interface: str) -> dict:
        if not _is_root():
            return {'error': 'Root privileges required'}
        result = _run(['ip', '-6', 'neigh', 'add', ipv6, 'lladdr', mac, 'dev', interface], check=False)
        if result.returncode != 0:
            return {'error': result.stderr.strip()}
        return {'success': True}

    def remove_neighbor(self, ipv6: str, interface: str) -> dict:
        if not _is_root():
            return {'error': 'Root privileges required'}
        result = _run(['ip', '-6', 'neigh', 'del', ipv6, 'dev', interface], check=False)
        if result.returncode != 0:
            return {'error': result.stderr.strip()}
        return {'success': True}

    def set_mtu(self, interface: str, mtu: int) -> dict:
        if not _is_root():
            return {'error': 'Root privileges required'}
        result = _run(['ip', 'link', 'set', 'dev', interface, 'mtu', str(mtu)], check=False)
        if result.returncode != 0:
            return {'error': result.stderr.strip()}
        return {'success': True}

    def is_privileged(self) -> bool:
        return _is_root()
