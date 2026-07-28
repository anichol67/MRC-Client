"""
Arista EOS Switch Provisioner for OCP MRC Leaf-Spine Fabric

Provisions Arista EOS (cEOS) switches via eAPI (JSON-RPC over HTTPS) for an
OCP MRC leaf-spine fabric with SRv6.  Supports two deployment models:

  - Model A (offline): generate startup configuration files for each switch
  - Model B (live):    push configuration to running switches via eAPI

eAPI calls use urllib.request with JSON-RPC to avoid an external dependency
on the ``requests`` library.  SSL certificate verification is disabled by
default (suitable for Containerlab / lab environments).
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EOSCredentials:
    """Connection credentials for an Arista EOS switch eAPI endpoint."""

    host: str
    username: str = 'admin'
    password: str = 'admin'
    port: int = 443
    transport: str = 'https'

    @property
    def url(self) -> str:
        return f'{self.transport}://{self.host}:{self.port}/command-api'

    def to_dict(self) -> dict:
        return {
            'host': self.host,
            'username': self.username,
            'port': self.port,
            'transport': self.transport,
        }


@dataclass
class EOSNodeConfig:
    """Configuration parameters for a single EOS switch in the MRC fabric."""

    hostname: str
    role: str                          # 'spine' or 'leaf'
    plane: int
    index: int
    loopback_ipv6: str
    interfaces: dict[str, str] = field(default_factory=dict)  # iface -> ipv6/mask
    static_routes: list[dict] = field(default_factory=list)    # [{prefix, next_hop}]
    srv6_locator: str = ''             # e.g. 'fcbb:0:20::/48'
    srv6_usid: int = 0                 # e.g. 0x0020
    srv6_transit: bool = True
    srv6_locator_routes: list[dict] = field(default_factory=list)  # [{prefix, next_hop}]
    ecn_enabled: bool = True
    ecn_min_thresh: int = 100          # KB, start marking
    ecn_max_thresh: int = 500          # KB, mark all packets
    wred_profile: str = 'MRC-WRED'
    data_tc: int = 0                   # traffic class for data queue

    def to_dict(self) -> dict:
        return {
            'hostname': self.hostname,
            'role': self.role,
            'plane': self.plane,
            'index': self.index,
            'loopback_ipv6': self.loopback_ipv6,
            'interfaces': self.interfaces,
            'static_routes': self.static_routes,
            'srv6_locator': self.srv6_locator,
            'srv6_usid': hex(self.srv6_usid) if self.srv6_usid else '0x0',
            'srv6_transit': self.srv6_transit,
            'ecn_enabled': self.ecn_enabled,
            'ecn_min_thresh': self.ecn_min_thresh,
            'ecn_max_thresh': self.ecn_max_thresh,
        }


# ---------------------------------------------------------------------------
# eAPI helper
# ---------------------------------------------------------------------------


def _eapi_call(
    url: str,
    cmds: list[str],
    username: str,
    password: str,
    fmt: str = 'json',
    timeout: int = 30,
) -> dict:
    """Execute commands on an EOS switch via the eAPI JSON-RPC interface.

    Returns a dict that always contains a ``'success'`` key (bool).
    On failure the dict also contains ``'error'`` with a description.
    """
    payload = json.dumps({
        'jsonrpc': '2.0',
        'method': 'runCmds',
        'params': {
            'version': 1,
            'cmds': cmds,
            'format': fmt,
        },
        'id': 'mrc-provisioner',
    }).encode()

    # HTTP Basic Auth header
    credentials = base64.b64encode(f'{username}:{password}'.encode()).decode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Basic {credentials}',
        },
        method='POST',
    )

    # Disable SSL verification for lab / Containerlab environments.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())

        if 'error' in body:
            err = body['error']
            msg = err.get('message', str(err))
            logger.error('eAPI error from %s: %s', url, msg)
            return {'success': False, 'error': msg, 'detail': err}

        return {'success': True, 'result': body.get('result', [])}

    except urllib.error.HTTPError as exc:
        msg = f'HTTP {exc.code}: {exc.reason}'
        logger.error('eAPI HTTP error from %s: %s', url, msg)
        return {'success': False, 'error': msg}
    except urllib.error.URLError as exc:
        logger.error('eAPI connection error to %s: %s', url, exc.reason)
        return {'success': False, 'error': str(exc.reason)}
    except (json.JSONDecodeError, OSError) as exc:
        logger.error('eAPI request failed for %s: %s', url, exc)
        return {'success': False, 'error': str(exc)}


# ---------------------------------------------------------------------------
# EOSProvisioner
# ---------------------------------------------------------------------------


class EOSProvisioner:
    """Generate and push EOS configuration for a single MRC fabric switch."""

    # -- Config generation (used by both Model A and Model B) ---------------

    @staticmethod
    def generate_config_commands(node_config: EOSNodeConfig) -> list[str]:
        """Return a list of EOS CLI configuration commands for *node_config*."""

        cmds: list[str] = []

        # Hostname
        cmds.append(f'hostname {node_config.hostname}')
        cmds.append('!')

        # IPv6 unicast routing
        cmds.append('ipv6 unicast-routing')
        cmds.append('!')

        # Loopback0
        cmds.append('interface Loopback0')
        cmds.append(f'   ipv6 address {node_config.loopback_ipv6}')
        cmds.append('!')

        # Physical / point-to-point interfaces
        for iface_name, ipv6_addr in sorted(node_config.interfaces.items()):
            cmds.append(f'interface {iface_name}')
            cmds.append('   no switchport')
            cmds.append(f'   ipv6 address {ipv6_addr}')
            cmds.append('!')

        # Static routes
        for route in node_config.static_routes:
            prefix = route.get('prefix', '')
            next_hop = route.get('next_hop', '')
            if prefix and next_hop:
                cmds.append(f'ipv6 route {prefix} {next_hop}')
        if node_config.static_routes:
            cmds.append('!')

        # SRv6 configuration (EOS 4.32+ uSID F3216)
        if node_config.srv6_locator:
            locator_name = f'LOC-{node_config.hostname.upper()}'
            cmds.append('router segment-routing')
            cmds.append('   srv6')
            cmds.append('      encapsulation source-address Loopback0')
            cmds.append(f'      locator {locator_name}')
            cmds.append(f'         prefix {node_config.srv6_locator}')
            cmds.append('         micro-segment behavior uN')
            cmds.append('      !')
            cmds.append('   !')
            cmds.append('!')

        # SRv6 transit (enables uSID pop-and-shift forwarding)
        if node_config.srv6_transit:
            cmds.append('segment-routing')
            cmds.append('   srv6')
            cmds.append('      transit')
            cmds.append('!')

        # Static routes to all remote SRv6 locator prefixes
        for route in node_config.srv6_locator_routes:
            cmds.append(f'ipv6 route {route["prefix"]} {route["next_hop"]}')
        if node_config.srv6_locator_routes:
            cmds.append('!')

        # ECN / WRED configuration
        if node_config.ecn_enabled:
            profile = node_config.wred_profile
            cmds.append(f'qos profile {profile}')
            cmds.append(f'   random-detect ecn minimum-threshold {node_config.ecn_min_thresh} kbytes')
            cmds.append(f'   random-detect ecn maximum-threshold {node_config.ecn_max_thresh} kbytes')
            cmds.append('!')
            cmds.append(f'qos map traffic-class {node_config.data_tc} to cos 0')
            cmds.append(f'interface defaults')
            cmds.append(f'   qos trust dscp')
            cmds.append('!')

        return cmds

    @staticmethod
    def generate_startup_config(node_config: EOSNodeConfig) -> str:
        """Generate a complete EOS startup-config file string (Model A).

        Includes standard boilerplate so that eAPI is available on first boot
        and IPv6 forwarding is enabled.
        """
        lines: list[str] = []

        # Boilerplate header
        lines.append('! Startup configuration generated by MRC Provisioner')
        lines.append(f'! Node: {node_config.hostname}  Role: {node_config.role}')
        lines.append('!')

        # Enable IP routing
        lines.append('ip routing')
        lines.append('!')

        # Management API for eAPI access
        lines.append('management api http-commands')
        lines.append('   no shutdown')
        lines.append('!')

        # Core configuration
        commands = EOSProvisioner.generate_config_commands(node_config)
        lines.extend(commands)

        # Trailing newline
        lines.append('')
        return '\n'.join(lines)

    # -- eAPI connectivity (Model B) ----------------------------------------

    @staticmethod
    def connect(credentials: EOSCredentials) -> bool:
        """Test eAPI connectivity to a switch.  Returns True on success."""
        result = _eapi_call(
            credentials.url,
            ['show hostname'],
            credentials.username,
            credentials.password,
        )
        return result.get('success', False)

    @staticmethod
    def push_config(
        credentials: EOSCredentials,
        commands: list[str],
    ) -> dict:
        """Push configuration commands to a running switch via eAPI.

        Wraps the commands with ``enable``, ``configure terminal``, ``end``,
        and ``write memory`` so they are applied and persisted.
        """
        wrapped = ['enable', 'configure terminal'] + commands + ['end', 'write memory']
        logger.info(
            'Pushing %d config commands to %s',
            len(commands), credentials.host,
        )
        return _eapi_call(
            credentials.url,
            wrapped,
            credentials.username,
            credentials.password,
        )

    @staticmethod
    def push_node_config(
        credentials: EOSCredentials,
        node_config: EOSNodeConfig,
    ) -> dict:
        """Generate configuration commands for *node_config* and push them."""
        commands = EOSProvisioner.generate_config_commands(node_config)
        return EOSProvisioner.push_config(credentials, commands)

    @staticmethod
    def verify_config(credentials: EOSCredentials) -> dict:
        """Verify applied configuration via eAPI show commands.

        Runs ``show ipv6 interface brief``, ``show ipv6 route``, and
        ``show segment-routing srv6 locator`` and returns parsed results.
        """
        result = _eapi_call(
            credentials.url,
            [
                'show ipv6 interface brief',
                'show ipv6 route',
                'show segment-routing srv6 locator',
            ],
            credentials.username,
            credentials.password,
        )
        if not result.get('success'):
            return result

        outputs = result.get('result', [])
        return {
            'success': True,
            'ipv6_interfaces': outputs[0] if len(outputs) > 0 else {},
            'ipv6_routes': outputs[1] if len(outputs) > 1 else {},
            'srv6_locators': outputs[2] if len(outputs) > 2 else {},
        }

    @staticmethod
    def get_switch_info(credentials: EOSCredentials) -> dict:
        """Retrieve basic switch information (version, hostname, model)."""
        result = _eapi_call(
            credentials.url,
            ['show version', 'show hostname'],
            credentials.username,
            credentials.password,
        )
        if not result.get('success'):
            return result

        outputs = result.get('result', [])
        version_info = outputs[0] if len(outputs) > 0 else {}
        hostname_info = outputs[1] if len(outputs) > 1 else {}
        return {
            'success': True,
            'version': version_info.get('version', ''),
            'model': version_info.get('modelName', ''),
            'serial': version_info.get('serialNumber', ''),
            'uptime': version_info.get('uptime', 0),
            'hostname': hostname_info.get('hostname', ''),
            'fqdn': hostname_info.get('fqdn', ''),
        }


# ---------------------------------------------------------------------------
# FabricProvisioner
# ---------------------------------------------------------------------------


class FabricProvisioner:
    """Orchestrate provisioning across all switches in an MRC fabric."""

    def __init__(self, nodes: list[tuple[EOSCredentials, EOSNodeConfig]]):
        self._nodes = nodes
        self._provisioner = EOSProvisioner()
        self._results: dict[str, dict] = {}

    # -- Connectivity -------------------------------------------------------

    def test_connectivity(self) -> dict[str, bool]:
        """Test eAPI connectivity to every switch.  Returns hostname -> bool."""
        status: dict[str, bool] = {}
        for creds, cfg in self._nodes:
            ok = self._provisioner.connect(creds)
            status[cfg.hostname] = ok
            if not ok:
                logger.warning('Cannot reach %s (%s)', cfg.hostname, creds.host)
        return status

    # -- Provisioning -------------------------------------------------------

    def provision_all(self) -> dict[str, dict]:
        """Push configuration to every switch.  Returns hostname -> result."""
        results: dict[str, dict] = {}
        for creds, cfg in self._nodes:
            logger.info('Provisioning %s (%s)', cfg.hostname, creds.host)
            result = self._provisioner.push_node_config(creds, cfg)
            results[cfg.hostname] = result
            self._results[cfg.hostname] = result
        return results

    def provision_node(self, hostname: str) -> dict:
        """Push configuration to a single switch identified by *hostname*."""
        for creds, cfg in self._nodes:
            if cfg.hostname == hostname:
                result = self._provisioner.push_node_config(creds, cfg)
                self._results[hostname] = result
                return result
        return {'success': False, 'error': f'Unknown node: {hostname}'}

    # -- Verification -------------------------------------------------------

    def verify_all(self) -> dict[str, dict]:
        """Verify configuration on every switch.  Returns hostname -> result."""
        results: dict[str, dict] = {}
        for creds, cfg in self._nodes:
            results[cfg.hostname] = self._provisioner.verify_config(creds)
        return results

    # -- Offline config generation (Model A) --------------------------------

    def generate_all_configs(self) -> dict[str, str]:
        """Generate startup-config strings for every switch (Model A).

        Returns a mapping of hostname -> config file content.
        """
        configs: dict[str, str] = {}
        for _creds, cfg in self._nodes:
            configs[cfg.hostname] = self._provisioner.generate_startup_config(cfg)
        return configs

    # -- Serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        """Return a status summary suitable for the web GUI."""
        nodes_summary = []
        for creds, cfg in self._nodes:
            nodes_summary.append({
                'hostname': cfg.hostname,
                'role': cfg.role,
                'plane': cfg.plane,
                'index': cfg.index,
                'management_ip': creds.host,
                'last_result': self._results.get(cfg.hostname),
            })
        return {
            'node_count': len(self._nodes),
            'nodes': nodes_summary,
        }
