"""
Platform Detection and Offline Mode for MRC End-Host Emulator

Detects the runtime environment (Linux/macOS/Windows, root privileges,
available tools) and provides simulated replacements for network
configuration and packet I/O when real hardware access is unavailable.

On Linux with root and the `ip` command available, the tool runs in LIVE
mode with real packet I/O and network configuration.  On all other
platforms (or when OFFLINE_MODE=1 is set), the tool runs in OFFLINE mode
where all network operations are simulated in-memory.

This module is imported early in app.py to determine the mode before
other modules initialize.
"""

from __future__ import annotations

import os
import random
import shutil
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from core.network_config import (
    InterfaceInfo,
    IPv6Route,
    Neighbor,
    NetworkConfigManager,
)


# ---------------------------------------------------------------------------
# RuntimeMode enum
# ---------------------------------------------------------------------------

class RuntimeMode(Enum):
    """Whether the emulator has real hardware access or is simulating."""
    LIVE = auto()
    OFFLINE = auto()


# ---------------------------------------------------------------------------
# RuntimeInfo — detected platform capabilities
# ---------------------------------------------------------------------------

@dataclass
class RuntimeInfo:
    """Snapshot of the detected runtime environment.

    Populated once at startup by :func:`detect_runtime` and passed to
    factory functions to choose between real and simulated backends.
    """
    mode: RuntimeMode
    platform: str                # e.g. 'linux', 'darwin', 'win32'
    is_root: bool
    has_scapy_raw: bool          # can open raw sockets
    has_ip_command: bool          # Linux `ip` utility available
    reason: str                  # human-readable explanation

    def to_dict(self) -> dict:
        return {
            'mode': self.mode.name,
            'platform': self.platform,
            'is_root': self.is_root,
            'has_scapy_raw': self.has_scapy_raw,
            'has_ip_command': self.has_ip_command,
            'reason': self.reason,
        }


# ---------------------------------------------------------------------------
# detect_runtime() — platform and privilege detection
# ---------------------------------------------------------------------------

def detect_runtime() -> RuntimeInfo:
    """Probe the current environment and return a :class:`RuntimeInfo`.

    Decision logic:

    1. If the ``OFFLINE_MODE`` environment variable is set to ``'1'``,
       force OFFLINE regardless of other checks.
    2. If running on Linux **and** the process has root privileges
       **and** the ``ip`` command is on ``$PATH``, select LIVE mode.
    3. Everything else falls through to OFFLINE with an explanatory
       reason string.
    """
    platform = sys.platform  # 'linux', 'darwin', 'win32', ...

    # Root check — os.geteuid() is only available on Unix-like systems.
    if platform == 'win32':
        is_root = False
    else:
        try:
            is_root = os.geteuid() == 0
        except AttributeError:
            is_root = False

    # `ip` command check — only meaningful on Linux.
    has_ip_command = shutil.which('ip') is not None if platform == 'linux' else False

    # Raw-socket capability mirrors the root check (scapy needs root/cap).
    has_scapy_raw = is_root and platform == 'linux'

    # --- Mode decision ---

    env_offline = os.environ.get('OFFLINE_MODE', '').strip()
    if env_offline == '1':
        return RuntimeInfo(
            mode=RuntimeMode.OFFLINE,
            platform=platform,
            is_root=is_root,
            has_scapy_raw=False,
            has_ip_command=has_ip_command,
            reason='OFFLINE_MODE=1 set',
        )

    if platform != 'linux':
        return RuntimeInfo(
            mode=RuntimeMode.OFFLINE,
            platform=platform,
            is_root=is_root,
            has_scapy_raw=False,
            has_ip_command=has_ip_command,
            reason=f'Non-Linux platform ({platform})',
        )

    if not is_root:
        return RuntimeInfo(
            mode=RuntimeMode.OFFLINE,
            platform=platform,
            is_root=False,
            has_scapy_raw=False,
            has_ip_command=has_ip_command,
            reason='Running without root',
        )

    if not has_ip_command:
        return RuntimeInfo(
            mode=RuntimeMode.OFFLINE,
            platform=platform,
            is_root=True,
            has_scapy_raw=False,
            has_ip_command=False,
            reason='ip command not found on PATH',
        )

    # Linux + root + ip available -> LIVE
    return RuntimeInfo(
        mode=RuntimeMode.LIVE,
        platform=platform,
        is_root=True,
        has_scapy_raw=True,
        has_ip_command=True,
        reason='Linux with root and ip command available',
    )


# ---------------------------------------------------------------------------
# SimulatedNetwork — in-memory replacement for NetworkConfigManager
# ---------------------------------------------------------------------------

class SimulatedNetwork:
    """Drop-in replacement for :class:`NetworkConfigManager` in offline mode.

    All operations manipulate in-memory data structures and return dicts
    that match the format of the real implementation.  Pre-populated with
    a realistic set of interfaces on construction.
    """

    def __init__(self) -> None:
        self.interfaces: list[dict] = [
            {
                'name': 'lo', 'index': 1,
                'mac': '00:00:00:00:00:00', 'mtu': 65536,
                'state': 'UP', 'ipv6_addrs': ['::1/128'],
            },
            {
                'name': 'eth0', 'index': 2,
                'mac': '02:42:ac:11:00:02', 'mtu': 9000,
                'state': 'UP', 'ipv6_addrs': ['fd00::1/64'],
            },
            {
                'name': 'eth1', 'index': 3,
                'mac': '02:42:ac:11:00:03', 'mtu': 9000,
                'state': 'UP', 'ipv6_addrs': ['fe80::42:acff:fe11:3/64'],
            },
            {
                'name': 'eth2', 'index': 4,
                'mac': '02:42:ac:11:00:04', 'mtu': 9000,
                'state': 'UP', 'ipv6_addrs': ['fe80::42:acff:fe11:4/64'],
            },
            {
                'name': 'eth3', 'index': 5,
                'mac': '02:42:ac:11:00:05', 'mtu': 9000,
                'state': 'DOWN', 'ipv6_addrs': ['fe80::42:acff:fe11:5/64'],
            },
        ]
        self.routes: list[dict] = []
        self.neighbors: list[dict] = []

    # -- Interface management -----------------------------------------------

    def list_interfaces(self) -> list[InterfaceInfo]:
        """Return simulated interfaces as :class:`InterfaceInfo` objects."""
        return [
            InterfaceInfo(
                name=iface['name'],
                index=iface['index'],
                mac=iface['mac'],
                mtu=iface['mtu'],
                state=iface['state'],
                ipv6_addrs=list(iface['ipv6_addrs']),
            )
            for iface in self.interfaces
        ]

    def add_ipv6_address(self, interface: str, address: str) -> dict:
        """Add an IPv6 address to a simulated interface."""
        for iface in self.interfaces:
            if iface['name'] == interface:
                if address not in iface['ipv6_addrs']:
                    iface['ipv6_addrs'].append(address)
                return {'success': True, 'address': address, 'interface': interface}
        return {'error': f'Interface {interface} not found'}

    def remove_ipv6_address(self, interface: str, address: str) -> dict:
        """Remove an IPv6 address from a simulated interface."""
        for iface in self.interfaces:
            if iface['name'] == interface:
                if address in iface['ipv6_addrs']:
                    iface['ipv6_addrs'].remove(address)
                    return {'success': True}
                return {'error': f'Address {address} not found on {interface}'}
        return {'error': f'Interface {interface} not found'}

    # -- Route management ---------------------------------------------------

    def list_routes(self) -> list[IPv6Route]:
        """Return simulated routes as :class:`IPv6Route` objects."""
        return [
            IPv6Route(
                destination=r['destination'],
                next_hop=r.get('next_hop', ''),
                interface=r.get('interface', ''),
                metric=r.get('metric', 1024),
                protocol=r.get('protocol', 'static'),
            )
            for r in self.routes
        ]

    def add_route(self, destination: str, next_hop: str = '',
                  interface: str = '', metric: int = 1024) -> dict:
        """Add a route to the simulated routing table."""
        route = {
            'destination': destination,
            'next_hop': next_hop,
            'interface': interface,
            'metric': metric,
            'protocol': 'static',
        }
        self.routes.append(route)
        return {'success': True}

    def remove_route(self, destination: str, next_hop: str = '',
                     interface: str = '') -> dict:
        """Remove a matching route from the simulated routing table."""
        for i, r in enumerate(self.routes):
            if r['destination'] == destination:
                if next_hop and r.get('next_hop') != next_hop:
                    continue
                if interface and r.get('interface') != interface:
                    continue
                self.routes.pop(i)
                return {'success': True}
        return {'error': f'Route to {destination} not found'}

    # -- Neighbor management ------------------------------------------------

    def list_neighbors(self) -> list[Neighbor]:
        """Return simulated neighbors as :class:`Neighbor` objects."""
        return [
            Neighbor(
                ipv6=n['ipv6'],
                mac=n.get('mac', ''),
                interface=n.get('interface', ''),
                state=n.get('state', 'REACHABLE'),
            )
            for n in self.neighbors
        ]

    def add_neighbor(self, ipv6: str, mac: str, interface: str) -> dict:
        """Add a neighbor entry to the simulated neighbor table."""
        self.neighbors.append({
            'ipv6': ipv6,
            'mac': mac,
            'interface': interface,
            'state': 'REACHABLE',
        })
        return {'success': True}

    def remove_neighbor(self, ipv6: str, interface: str) -> dict:
        """Remove a neighbor entry from the simulated neighbor table."""
        for i, n in enumerate(self.neighbors):
            if n['ipv6'] == ipv6 and n['interface'] == interface:
                self.neighbors.pop(i)
                return {'success': True}
        return {'error': f'Neighbor {ipv6} on {interface} not found'}

    # -- MTU ----------------------------------------------------------------

    def set_mtu(self, interface: str, mtu: int) -> dict:
        """Update the MTU on a simulated interface."""
        for iface in self.interfaces:
            if iface['name'] == interface:
                iface['mtu'] = mtu
                return {'success': True}
        return {'error': f'Interface {interface} not found'}

    # -- Privilege check ----------------------------------------------------

    def is_privileged(self) -> bool:
        """Always returns True — simulated operations always succeed."""
        return True

    # -- Serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            'simulated': True,
            'interface_count': len(self.interfaces),
            'interfaces': [dict(iface) for iface in self.interfaces],
            'route_count': len(self.routes),
            'routes': [dict(r) for r in self.routes],
            'neighbor_count': len(self.neighbors),
            'neighbors': [dict(n) for n in self.neighbors],
        }


# ---------------------------------------------------------------------------
# SimulatedPacketIO — in-memory replacement for scapy raw socket I/O
# ---------------------------------------------------------------------------

class SimulatedPacketIO:
    """Replaces real scapy-based packet I/O in offline mode.

    Instead of sending packets on the wire, logs them in memory.
    Supports injecting simulated responses for probe workflows.
    """

    def __init__(self) -> None:
        self.sent_packets: list[dict] = []
        self.received_packets: list[dict] = []

    def send_packet(self, raw_bytes: bytes, interface: str = '') -> dict:
        """Log a packet instead of transmitting it.

        Returns a success dict in the same format as real packet I/O.
        """
        entry = {
            'hex': raw_bytes.hex(),
            'summary': f'{len(raw_bytes)} bytes on {interface or "default"}',
            'timestamp': time.time(),
            'interface': interface,
            'length': len(raw_bytes),
        }
        self.sent_packets.append(entry)
        return {'success': True, 'simulated': True, 'length': len(raw_bytes)}

    def get_sent_log(self, limit: int = 100) -> list[dict]:
        """Return the most recent *limit* sent packet entries."""
        return self.sent_packets[-limit:]

    def simulate_response(self, packet_dict: dict) -> None:
        """Inject a simulated response into received_packets.

        Used by :class:`SimulatedProbeResponder` and test harnesses to
        feed synthetic responses back into the processing pipeline.
        """
        packet_dict.setdefault('timestamp', time.time())
        self.received_packets.append(packet_dict)

    def to_dict(self) -> dict:
        return {
            'simulated': True,
            'sent_count': len(self.sent_packets),
            'received_count': len(self.received_packets),
            'last_sent': self.sent_packets[-1] if self.sent_packets else None,
            'last_received': self.received_packets[-1] if self.received_packets else None,
        }


# ---------------------------------------------------------------------------
# SimulatedProbeResponder — generates fake probe responses in offline mode
# ---------------------------------------------------------------------------

class SimulatedProbeResponder:
    """Generates synthetic probe responses for offline mode.

    Produces :class:`ProbeResult`-compatible dicts with randomized RTTs
    and configurable reachability / congestion fault injection, enabling
    the full probe workflow to run without real network hardware.
    """

    def __init__(self) -> None:
        self._unreachable_evs: set[int] = set()
        self._congested_evs: set[int] = set()
        self._next_probe_id: int = 0

    def generate_response(self, probe_type: str, ev_value: int,
                          base_rtt_us: float = 100.0) -> dict:
        """Generate a synthetic probe response.

        Parameters
        ----------
        probe_type : str
            One of ``'EV_PROBE'``, ``'RELIABILITY_PROBE'``, or
            ``'PORT_STATUS_UPDATE'``.
        ev_value : int
            The entropy value being probed.
        base_rtt_us : float
            Baseline RTT in microseconds; jitter is added on top.

        Returns
        -------
        dict
            Fields matching :class:`ProbeResult.to_dict()` output.
        """
        reachable = ev_value not in self._unreachable_evs
        congested = ev_value in self._congested_evs

        if reachable:
            rtt = base_rtt_us + random.uniform(-20.0, 50.0)
            rtt = max(rtt, 1.0)  # floor at 1 us
        else:
            rtt = 0.0

        # m_flag: 0=NONE, 1=SKIP_ONCE (congested), 2=ALWAYS_SKIP
        if congested:
            m_flag = 1  # SKIP_ONCE
        else:
            m_flag = 0  # NONE

        m_flag_names = {0: 'NONE', 1: 'SKIP_ONCE', 2: 'ALWAYS_SKIP'}

        probe_id = self._next_probe_id
        self._next_probe_id = (self._next_probe_id + 1) & 0xFFFF

        now = time.time()
        return {
            'probe_id': probe_id,
            'probe_type': probe_type,
            'ev_value': ev_value,
            'ev_hex': f'0x{ev_value:08X}',
            'rtt_us': round(rtt, 3),
            'reachable': reachable,
            'response_timestamp': int(now * 1e6) & 0xFFFF,
            'ecn_marked': congested,
            'm_flag': m_flag,
            'm_flag_name': m_flag_names.get(m_flag, f'UNKNOWN({m_flag})'),
            'received_time': now,
            'simulated': True,
        }

    def set_unreachable_evs(self, ev_values: list[int]) -> None:
        """Mark certain EV values as unreachable.

        Probes to these EVs will return ``reachable=False`` and
        ``rtt_us=0.0``.
        """
        self._unreachable_evs = set(ev_values)

    def set_congested_evs(self, ev_values: list[int]) -> None:
        """Mark certain EV values as congested.

        Probes to these EVs will return ``m_flag=SKIP_ONCE`` and
        ``ecn_marked=True``.
        """
        self._congested_evs = set(ev_values)

    def to_dict(self) -> dict:
        return {
            'unreachable_evs': sorted(self._unreachable_evs),
            'congested_evs': sorted(self._congested_evs),
            'next_probe_id': self._next_probe_id,
        }


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def get_network_manager(runtime: RuntimeInfo):
    """Return the appropriate network manager for the detected runtime.

    In LIVE mode, returns a real :class:`NetworkConfigManager` that
    talks to the kernel via ``ip`` commands.  In OFFLINE mode, returns
    a :class:`SimulatedNetwork` with in-memory state.
    """
    if runtime.mode is RuntimeMode.LIVE:
        return NetworkConfigManager()
    return SimulatedNetwork()


def get_packet_io(runtime: RuntimeInfo):
    """Return the appropriate packet I/O backend for the detected runtime.

    In LIVE mode, returns a real scapy-based packet sender (imported
    lazily to avoid import errors on platforms without scapy).  In
    OFFLINE mode, returns a :class:`SimulatedPacketIO` that logs
    packets in memory.
    """
    if runtime.mode is RuntimeMode.LIVE:
        # Lazy import — scapy may not be installed on non-Linux hosts.
        from core.packet_builder import PacketBuilder  # noqa: F811
        return PacketBuilder()
    return SimulatedPacketIO()
