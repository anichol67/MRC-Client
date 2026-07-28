"""
Topology Generator for OCP MRC Leaf-Spine Fabric.

Generates Containerlab topology files and IPv6/SRv6 addressing plans
for a multi-plane leaf-spine fabric used with the MRC specification.
Supports configurable planes, leafs, spines, and hosts, with automatic
derivation of all IPv6 interface addresses, SRv6 locators, uSID values,
and EV-to-SRv6 path mappings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def parse_base_prefix(base: str) -> tuple[str, int]:
    """Split an IPv6 prefix string into the address stem and prefix length.

    Examples::

        >>> parse_base_prefix('fd00::/32')
        ('fd00', 32)
        >>> parse_base_prefix('fcbb:abcd::/48')
        ('fcbb:abcd', 48)
    """
    addr_part, length_str = base.split('/')
    prefix_len = int(length_str)
    # Strip the trailing '::' to isolate the meaningful prefix groups
    prefix = addr_part.rstrip(':')
    return prefix, prefix_len


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TopologyConfig:
    """Configuration parameters for the MRC leaf-spine fabric topology."""

    num_planes: int = 2
    leafs_per_plane: int = 4
    spines_per_plane: int = 2
    hosts_per_leaf: int = 1
    ipv6_base: str = 'fd00::/32'
    srv6_base: str = 'fcbb::/32'
    ceos_image: str = 'ceos:4.36.0.1F'
    mrc_image: str = 'mrc-emulator:latest'
    management_network: str = '172.20.0.0/24'
    qps_per_host_pair: int = 1

    def to_dict(self) -> dict:
        return {
            'num_planes': self.num_planes,
            'leafs_per_plane': self.leafs_per_plane,
            'spines_per_plane': self.spines_per_plane,
            'hosts_per_leaf': self.hosts_per_leaf,
            'ipv6_base': self.ipv6_base,
            'srv6_base': self.srv6_base,
            'ceos_image': self.ceos_image,
            'mrc_image': self.mrc_image,
            'management_network': self.management_network,
        }


@dataclass
class NodeInfo:
    """A single node (spine, leaf, or host) in the topology.

    Switch nodes (spine/leaf) carry full IPv6 and SRv6 addressing and
    belong to a single plane.

    Host nodes represent a multi-port XPU connected to all planes
    (Port = Plane per spec §9.3.2, §11.5).  Their ``plane`` is set to
    ``-1`` and per-plane connectivity is stored in ``plane_interfaces``.
    """

    name: str
    role: str  # 'spine', 'leaf', or 'host'
    plane: int
    index: int
    loopback_ipv6: str = ''
    management_ip: str = ''
    srv6_locator: str = ''
    usid: int = 0
    interfaces: dict[str, str] = field(default_factory=dict)
    plane_interfaces: dict[int, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'role': self.role,
            'plane': self.plane,
            'index': self.index,
            'loopback_ipv6': self.loopback_ipv6,
            'management_ip': self.management_ip,
            'srv6_locator': self.srv6_locator,
            'usid': self.usid,
            'interfaces': dict(self.interfaces),
            'plane_interfaces': {
                k: dict(v) for k, v in self.plane_interfaces.items()
            },
        }


@dataclass
class LinkInfo:
    """A single link between two nodes with interface and address details."""

    node_a: str
    node_b: str
    iface_a: str
    iface_b: str
    addr_a: str
    addr_b: str

    def to_dict(self) -> dict:
        return {
            'node_a': self.node_a,
            'node_b': self.node_b,
            'iface_a': self.iface_a,
            'iface_b': self.iface_b,
            'addr_a': self.addr_a,
            'addr_b': self.addr_b,
        }


@dataclass
class PathInfo:
    """A single source-to-destination forwarding path through the fabric.

    Each path traverses exactly one plane and one spine:
    source_host -> source_leaf -> spine -> dest_leaf -> dest_host.

    ``source_ipv6`` and ``dest_ipv6`` are the per-plane IPv6 addresses
    used for SRv6 forwarding on this path's plane.
    """

    source_host: str
    dest_host: str
    plane: int
    spine_index: int
    ev_value: int
    srv6_address: str
    source_ipv6: str = ''
    dest_ipv6: str = ''
    hops: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'source_host': self.source_host,
            'dest_host': self.dest_host,
            'plane': self.plane,
            'spine_index': self.spine_index,
            'ev_value': self.ev_value,
            'srv6_address': self.srv6_address,
            'source_ipv6': self.source_ipv6,
            'dest_ipv6': self.dest_ipv6,
            'hops': list(self.hops),
        }


# ---------------------------------------------------------------------------
# Topology Generator
# ---------------------------------------------------------------------------

class TopologyGenerator:
    """Generate Containerlab topology and addressing for an MRC leaf-spine fabric.

    The generator creates all nodes, links, and host-to-host paths for a
    multi-plane leaf-spine fabric.  Each plane contains a configurable
    number of leaf and spine switches with full-mesh connectivity, plus one
    or more MRC emulator host containers attached to each leaf.

    Usage::

        gen = TopologyGenerator(TopologyConfig(num_planes=2))
        gen.generate()
        print(gen.generate_clab_yaml())
    """

    def __init__(self, config: Optional[TopologyConfig] = None) -> None:
        self.config = config or TopologyConfig()
        self._ipv6_prefix, self._ipv6_len = parse_base_prefix(
            self.config.ipv6_base,
        )
        self._srv6_prefix, self._srv6_len = parse_base_prefix(
            self.config.srv6_base,
        )
        self.nodes: list[NodeInfo] = []
        self.links: list[LinkInfo] = []
        self.paths: dict[str, list[PathInfo]] = {}
        self._node_map: dict[str, NodeInfo] = {}

    # -- public API ----------------------------------------------------------

    def generate(self) -> None:
        """Populate all nodes, links, and paths."""
        self._generate_nodes()
        self._generate_links()
        self._generate_paths()

    def get_node_by_name(self, name: str) -> Optional[NodeInfo]:
        """Look up a node by its topology name.

        Returns ``None`` if no node with the given name exists.
        """
        return self._node_map.get(name)

    def set_management_ips(self, node_ips: dict[str, str]) -> None:
        """Assign management IP addresses for live provisioning (Model B).

        *node_ips* maps node names to their management IP addresses.
        Unknown node names are silently ignored.
        """
        for name, ip in node_ips.items():
            node = self._node_map.get(name)
            if node is not None:
                node.management_ip = ip

    def get_paths_for_host(self, host_name: str) -> list[PathInfo]:
        """Return all paths originating from the given host.

        Accepts physical container names (e.g. ``'p0-host0'``) or logical
        host names (e.g. ``'host0'``).  Logical names return paths across
        all planes.
        """
        result: list[PathInfo] = []
        for path_list in self.paths.values():
            for path in path_list:
                if self._host_matches(path.source_host, host_name):
                    result.append(path)
        return result

    def get_ev_profile_for_host(self, host_name: str) -> dict:
        """Return EV values and SRv6 mappings ready for the EV engine.

        The profile covers all planes and spines for the given host.

        Returns a dict of the form::

            {
                'host': '<host_name>',
                'destinations': {
                    'host1': [
                        {'ev_value': 0, 'srv6_address': '...', ...},
                    ],
                },
            }
        """
        logical = self._to_logical_host(host_name)
        host_paths = self.get_paths_for_host(logical)

        destinations: dict[str, list[dict]] = {}
        for path in host_paths:
            dest = path.dest_host
            if dest not in destinations:
                destinations[dest] = []
            destinations[dest].append({
                'ev_value': path.ev_value,
                'srv6_address': path.srv6_address,
                'source_ipv6': path.source_ipv6,
                'dest_ipv6': path.dest_ipv6,
                'plane': path.plane,
                'spine_index': path.spine_index,
                'hops': list(path.hops),
            })

        return {
            'host': logical,
            'destinations': destinations,
        }

    def generate_clab_yaml(self) -> str:
        """Generate a Containerlab-compatible YAML topology definition.

        Returns a YAML string suitable for ``containerlab deploy``.
        """
        topo: dict = {
            'name': 'mrc-fabric',
            'mgmt': {
                'network': 'mrc-mgmt',
                'ipv4-subnet': self.config.management_network,
            },
            'topology': {
                'nodes': {},
                'links': [],
            },
        }

        nodes_section = topo['topology']['nodes']
        for node in self.nodes:
            if node.role in ('spine', 'leaf'):
                nodes_section[node.name] = {
                    'kind': 'ceos',
                    'image': self.config.ceos_image,
                    'startup-config': f'configs/{node.name}.cfg',
                }
            elif node.role == 'host':
                nodes_section[node.name] = {
                    'kind': 'linux',
                    'image': self.config.mrc_image,
                    'binds': [
                        f'configs/{node.name}-startup.sh:/startup.sh',
                    ],
                    'exec': [
                        'bash /startup.sh',
                    ],
                }

        # Controller node (management only, no data interfaces)
        nodes_section['controller'] = {
            'kind': 'linux',
            'image': self.config.mrc_image,
        }

        links_section = topo['topology']['links']
        for link in self.links:
            links_section.append({
                'endpoints': [
                    f'{link.node_a}:{link.iface_a}',
                    f'{link.node_b}:{link.iface_b}',
                ],
            })

        return yaml.dump(topo, default_flow_style=False, sort_keys=False)

    def generate_addressing_plan(self) -> dict:
        """Return the full addressing plan as a JSON-serializable dict.

        Includes configuration, per-node addressing, link details, and
        all path mappings.
        """
        return {
            'config': self.config.to_dict(),
            'nodes': {n.name: n.to_dict() for n in self.nodes},
            'links': [link.to_dict() for link in self.links],
            'paths': {
                key: [p.to_dict() for p in plist]
                for key, plist in self.paths.items()
            },
        }

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation of the full topology."""
        return {
            'config': self.config.to_dict(),
            'nodes': [n.to_dict() for n in self.nodes],
            'links': [link.to_dict() for link in self.links],
            'paths': {
                key: [p.to_dict() for p in plist]
                for key, plist in self.paths.items()
            },
        }

    # -- addressing helpers --------------------------------------------------

    @staticmethod
    def _role_id(role: str, index: int) -> int:
        """Compute the combined role+index value used in addressing.

        Spine 0 -> 0x10 (16), Leaf 3 -> 0x23 (35), Host 0 -> 0x30 (48).
        The upper nibble encodes role (1=spine, 2=leaf, 3=host) and the
        lower nibble encodes the node index within that role.
        """
        r = {'spine': 1, 'leaf': 2, 'host': 3}.get(role, 2)
        return (r << 4) | index

    def _make_ipv6_addr(self, plane: int, role_id: int,
                        link_id: Optional[int] = None,
                        side: Optional[int] = None) -> str:
        """Derive an IPv6 address from the addressing template.

        Loopback (link_id is None)::

            {base}:{plane}:{role_id}::1/128

        Point-to-point (link_id and side provided)::

            {base}:{plane}:{role_id}:{link_id}::{side}/127
        """
        if link_id is None:
            return f'{self._ipv6_prefix}:{plane:x}:{role_id:x}::1/128'
        return (
            f'{self._ipv6_prefix}:{plane:x}:{role_id:x}'
            f':{link_id:x}::{side}/127'
        )

    def _make_srv6_locator(self, plane: int, role_id: int) -> str:
        """Derive an SRv6 locator prefix.

        Format: ``{srv6_base}:{plane}:{role_id}::/48``
        """
        return f'{self._srv6_prefix}:{plane:x}:{role_id:x}::/48'

    @staticmethod
    def _make_usid(plane: int, role: str, index: int) -> int:
        """Derive a 16-bit uSID value for F3216 encoding.

        Bit layout::

            [15:8] = plane
            [7:4]  = role (1=spine, 2=leaf, 3=host)
            [3:0]  = index
        """
        r = {'spine': 1, 'leaf': 2, 'host': 3}.get(role, 2)
        return (plane << 8) | (r << 4) | index

    @staticmethod
    def _make_ev_value(plane: int, spine_index: int) -> int:
        """Derive a 32-bit EV value encoding plane and spine selection.

        Encoding: ``(plane << 8) | spine_index``.
        """
        return (plane << 8) | spine_index

    def _make_srv6_path(self, src_leaf_usid: int, spine_usid: int,
                        dst_leaf_usid: int) -> str:
        """Build a full SRv6 uSID stack address for a three-hop path.

        Format::

            {srv6_base}:{src_leaf_usid:04x}:{spine_usid:04x}:{dst_leaf_usid:04x}::
        """
        return (
            f'{self._srv6_prefix}:{src_leaf_usid:04x}'
            f':{spine_usid:04x}:{dst_leaf_usid:04x}::'
        )

    # -- host name helpers ---------------------------------------------------

    @staticmethod
    def _to_logical_host(name: str) -> str:
        """Convert a host name to its logical form.

        With the unified host model, host names are already logical
        (e.g. ``'host0'``).  This method is kept for backward
        compatibility with any code that passes plane-prefixed names.
        """
        if name.startswith('p') and '-host' in name:
            return name.split('-', 1)[1]
        return name

    @staticmethod
    def _host_matches(source_host: str, query: str) -> bool:
        """Check whether *source_host* matches *query*.

        With the unified host model, this is typically an exact match.
        Legacy plane-prefixed names are also handled for compatibility.
        """
        if source_host == query:
            return True
        logical_source = source_host
        if source_host.startswith('p') and '-host' in source_host:
            logical_source = source_host.split('-', 1)[1]
        return logical_source == query

    def get_paths_through_element(self, element_name: str) -> list[PathInfo]:
        """Return all paths whose hops include the given link or node.

        For a node name (e.g. ``'p0-spine1'``), returns paths that
        traverse that node.  For a link specified as
        ``'nodeA↔nodeB'``, returns paths that traverse both nodes
        adjacently.
        """
        results: list[PathInfo] = []
        if '↔' in element_name:
            node_a, node_b = element_name.split('↔', 1)
            for path_list in self.paths.values():
                for path in path_list:
                    hops = path.hops
                    for i in range(len(hops) - 1):
                        if ((hops[i] == node_a and hops[i + 1] == node_b) or
                                (hops[i] == node_b and hops[i + 1] == node_a)):
                            results.append(path)
                            break
        else:
            for path_list in self.paths.values():
                for path in path_list:
                    if element_name in path.hops:
                        results.append(path)
        return results

    def get_srv6_locator_routes(self, node_name: str) -> list[dict]:
        """Compute SRv6 locator routes for a switch node.

        Each switch needs static routes to every other switch's SRv6
        locator prefix.  The next-hop is determined by the direct P2P
        link between the two nodes (or via the connected spine/leaf for
        non-adjacent nodes).

        Returns a list of ``{'prefix': ..., 'next_hop': ...}`` dicts.
        """
        node = self._node_map.get(node_name)
        if node is None or node.role == 'host':
            return []

        routes: list[dict] = []
        seen_prefixes: set[str] = set()

        for other in self.nodes:
            if other.name == node_name or other.role == 'host':
                continue
            if not other.srv6_locator:
                continue
            if other.srv6_locator in seen_prefixes:
                continue

            next_hop = self._find_next_hop(node_name, other.name)
            if next_hop:
                routes.append({
                    'prefix': other.srv6_locator,
                    'next_hop': next_hop,
                })
                seen_prefixes.add(other.srv6_locator)

        return routes

    def _find_next_hop(self, from_node: str, to_node: str) -> str:
        """Find the IPv6 next-hop address from *from_node* to *to_node*.

        If the nodes are directly connected, returns the peer's link
        address.  If not directly connected (e.g. leaf to remote spine),
        returns the address of the local spine that connects toward the
        destination (for leaves) or the local leaf (for spines).
        """
        for link in self.links:
            if link.node_a == from_node and link.node_b == to_node:
                addr = link.addr_b
                return addr.split('/')[0] if '/' in addr else addr
            if link.node_b == from_node and link.node_a == to_node:
                addr = link.addr_a
                return addr.split('/')[0] if '/' in addr else addr

        from_n = self._node_map.get(from_node)
        to_n = self._node_map.get(to_node)
        if from_n is None or to_n is None:
            return ''

        if from_n.plane != to_n.plane:
            return ''

        if from_n.role == 'leaf' and to_n.role == 'leaf':
            for link in self.links:
                peer = ''
                if link.node_a == from_node and 'spine' in link.node_b:
                    peer_node = self._node_map.get(link.node_b)
                    if peer_node and peer_node.plane == from_n.plane:
                        addr = link.addr_b
                        return addr.split('/')[0] if '/' in addr else addr
                if link.node_b == from_node and 'spine' in link.node_a:
                    peer_node = self._node_map.get(link.node_a)
                    if peer_node and peer_node.plane == from_n.plane:
                        addr = link.addr_a
                        return addr.split('/')[0] if '/' in addr else addr

        return ''

    def generate_eos_node_configs(self) -> list[dict]:
        """Generate EOSNodeConfig-compatible dicts for all switch nodes.

        Includes SRv6 locator routes computed from the fabric topology.
        Suitable for passing to the EOS provisioner.
        """
        configs = []
        for node in self.nodes:
            if node.role == 'host':
                continue
            srv6_routes = self.get_srv6_locator_routes(node.name)
            configs.append({
                'hostname': node.name,
                'role': node.role,
                'plane': node.plane,
                'index': node.index,
                'loopback_ipv6': node.loopback_ipv6,
                'interfaces': dict(node.interfaces),
                'static_routes': [],
                'srv6_locator': node.srv6_locator,
                'srv6_usid': node.usid,
                'srv6_locator_routes': srv6_routes,
            })
        return configs

    def generate_host_startup_script(self, host_name: str) -> str:
        """Generate a shell script to configure a host container at boot.

        Sets up IPv6 addresses on per-plane interfaces and default
        routes via connected leaf switches.
        """
        node = self._node_map.get(host_name)
        if node is None or node.role != 'host':
            return '#!/bin/bash\n# Unknown host\n'

        lines = [
            '#!/bin/bash',
            f'# Startup script for {host_name}',
            f'# Generated by MRC emu topology generator',
            '',
        ]
        for plane, pi in sorted(node.plane_interfaces.items()):
            iface = pi['iface']
            ipv6 = pi['ipv6']
            leaf = pi['leaf']
            leaf_node = self._node_map.get(leaf)
            lines.append(f'# Plane {plane}: {iface} -> {leaf}')
            lines.append(f'ip -6 addr add {ipv6} dev {iface}')
            lines.append(f'ip link set {iface} up')
            lines.append(f'ip link set {iface} mtu 9216')
            # Find the leaf's address on this link for the default route
            for link in self.links:
                if ((link.node_a == leaf and link.node_b == host_name) or
                        (link.node_b == leaf and link.node_a == host_name)):
                    gw = link.addr_a if link.node_a == leaf else link.addr_b
                    gw_addr = gw.split('/')[0] if '/' in gw else gw
                    lines.append(
                        f'ip -6 route add default via {gw_addr} '
                        f'dev {iface} metric {100 + plane}'
                    )
                    break
            lines.append('')

        lines.append('echo "Host configuration complete"')
        lines.append('')
        return '\n'.join(lines)

    # -- generation internals ------------------------------------------------

    def _generate_nodes(self) -> None:
        """Create all NodeInfo objects with IPv6 and SRv6 addressing.

        Switch nodes (spine/leaf) are created per-plane.  Host XPU nodes
        are created once and connected to all planes via
        ``_generate_links()``, matching the spec's Port = Plane model
        (§9.3.2, §11.5).
        """
        self.nodes.clear()
        self._node_map.clear()
        cfg = self.config

        for plane in range(cfg.num_planes):
            # Spine switches
            for si in range(cfg.spines_per_plane):
                rid = self._role_id('spine', si)
                name = f'p{plane}-spine{si}'
                node = NodeInfo(
                    name=name,
                    role='spine',
                    plane=plane,
                    index=si,
                    loopback_ipv6=self._make_ipv6_addr(plane, rid),
                    srv6_locator=self._make_srv6_locator(plane, rid),
                    usid=self._make_usid(plane, 'spine', si),
                )
                self.nodes.append(node)
                self._node_map[name] = node

            # Leaf switches
            for li in range(cfg.leafs_per_plane):
                rid = self._role_id('leaf', li)
                name = f'p{plane}-leaf{li}'
                node = NodeInfo(
                    name=name,
                    role='leaf',
                    plane=plane,
                    index=li,
                    loopback_ipv6=self._make_ipv6_addr(plane, rid),
                    srv6_locator=self._make_srv6_locator(plane, rid),
                    usid=self._make_usid(plane, 'leaf', li),
                )
                self.nodes.append(node)
                self._node_map[name] = node

        # Host XPUs — one per logical position, connected to all planes.
        # plane=-1 indicates a multi-plane node; per-plane interfaces are
        # populated during _generate_links().
        hosts_per_plane = cfg.leafs_per_plane * cfg.hosts_per_leaf
        for li in range(cfg.leafs_per_plane):
            for hi in range(cfg.hosts_per_leaf):
                host_idx = li * cfg.hosts_per_leaf + hi
                name = f'host{host_idx}'
                node = NodeInfo(
                    name=name,
                    role='host',
                    plane=-1,
                    index=host_idx,
                )
                self.nodes.append(node)
                self._node_map[name] = node

    def _generate_links(self) -> None:
        """Create all LinkInfo objects with IPv6 point-to-point addresses.

        Assigns interface names sequentially per node (eth1, eth2, ...).
        eth0 is reserved for management.

        Each host XPU gets one link per plane to its corresponding leaf,
        with a distinct interface and IPv6 address per plane.  This
        populates ``NodeInfo.plane_interfaces`` for host nodes.
        """
        self.links.clear()
        cfg = self.config

        # Per-node interface counter for sequential eth assignment
        iface_counter: dict[str, int] = {}

        def next_iface(node_name: str) -> str:
            n = iface_counter.get(node_name, 0) + 1
            iface_counter[node_name] = n
            return f'eth{n}'

        for plane in range(cfg.num_planes):
            # Leaf <-> Spine links (full mesh within each plane)
            for li in range(cfg.leafs_per_plane):
                leaf_name = f'p{plane}-leaf{li}'
                leaf_rid = self._role_id('leaf', li)

                for si in range(cfg.spines_per_plane):
                    spine_name = f'p{plane}-spine{si}'
                    spine_rid = self._role_id('spine', si)

                    leaf_iface = next_iface(leaf_name)
                    spine_iface = next_iface(spine_name)

                    addr_leaf = self._make_ipv6_addr(
                        plane, leaf_rid, spine_rid, 0,
                    )
                    addr_spine = self._make_ipv6_addr(
                        plane, leaf_rid, spine_rid, 1,
                    )

                    self.links.append(LinkInfo(
                        node_a=leaf_name, node_b=spine_name,
                        iface_a=leaf_iface, iface_b=spine_iface,
                        addr_a=addr_leaf, addr_b=addr_spine,
                    ))

                    self._node_map[leaf_name].interfaces[leaf_iface] = addr_leaf
                    self._node_map[spine_name].interfaces[spine_iface] = addr_spine

            # Leaf <-> Host links (one per host per plane)
            for li in range(cfg.leafs_per_plane):
                leaf_name = f'p{plane}-leaf{li}'
                leaf_rid = self._role_id('leaf', li)

                for hi in range(cfg.hosts_per_leaf):
                    host_idx = li * cfg.hosts_per_leaf + hi
                    host_name = f'host{host_idx}'
                    host_link_id = 0xff - hi

                    leaf_iface = next_iface(leaf_name)
                    host_iface = next_iface(host_name)

                    addr_leaf = self._make_ipv6_addr(
                        plane, leaf_rid, host_link_id, 0,
                    )
                    addr_host = self._make_ipv6_addr(
                        plane, leaf_rid, host_link_id, 1,
                    )

                    self.links.append(LinkInfo(
                        node_a=leaf_name, node_b=host_name,
                        iface_a=leaf_iface, iface_b=host_iface,
                        addr_a=addr_leaf, addr_b=addr_host,
                    ))

                    host_node = self._node_map[host_name]
                    self._node_map[leaf_name].interfaces[leaf_iface] = addr_leaf
                    host_node.interfaces[host_iface] = addr_host

                    host_rid = self._role_id('host', host_idx)
                    host_node.plane_interfaces[plane] = {
                        'iface': host_iface,
                        'ipv6': addr_host,
                        'leaf': leaf_name,
                        'usid': self._make_usid(plane, 'host', host_idx),
                        'srv6_locator': self._make_srv6_locator(
                            plane, host_rid,
                        ),
                    }

    def _generate_paths(self) -> None:
        """Enumerate all host-to-host paths with EV and SRv6 mappings.

        Paths are keyed by logical host pair (e.g. ``'host0→host1'``)
        and include entries for every plane and spine combination.  Only
        pairs where source and destination are on different leafs are
        included (same-leaf pairs have no spine-traversal path).

        Each path records the per-plane source and destination IPv6
        addresses from the host's ``plane_interfaces``, matching the
        spec's model where each packet's source address corresponds to
        the egress plane's interface.
        """
        self.paths.clear()
        cfg = self.config
        total_hosts = cfg.leafs_per_plane * cfg.hosts_per_leaf

        for src_idx in range(total_hosts):
            src_leaf_idx = src_idx // cfg.hosts_per_leaf
            src_host = f'host{src_idx}'
            src_node = self._node_map[src_host]

            for dst_idx in range(total_hosts):
                dst_leaf_idx = dst_idx // cfg.hosts_per_leaf
                if src_leaf_idx == dst_leaf_idx:
                    continue

                dst_host = f'host{dst_idx}'
                dst_node = self._node_map[dst_host]
                key = f'{src_host}→{dst_host}'
                path_list: list[PathInfo] = []

                for plane in range(cfg.num_planes):
                    src_leaf = f'p{plane}-leaf{src_leaf_idx}'
                    dst_leaf = f'p{plane}-leaf{dst_leaf_idx}'
                    src_leaf_usid = self._make_usid(
                        plane, 'leaf', src_leaf_idx,
                    )
                    dst_leaf_usid = self._make_usid(
                        plane, 'leaf', dst_leaf_idx,
                    )

                    src_ipv6 = src_node.plane_interfaces.get(
                        plane, {},
                    ).get('ipv6', '')
                    dst_ipv6 = dst_node.plane_interfaces.get(
                        plane, {},
                    ).get('ipv6', '')

                    for si in range(cfg.spines_per_plane):
                        spine_name = f'p{plane}-spine{si}'
                        spine_usid = self._make_usid(plane, 'spine', si)
                        ev_value = self._make_ev_value(plane, si)
                        srv6_addr = self._make_srv6_path(
                            src_leaf_usid, spine_usid, dst_leaf_usid,
                        )

                        path_list.append(PathInfo(
                            source_host=src_host,
                            dest_host=dst_host,
                            plane=plane,
                            spine_index=si,
                            ev_value=ev_value,
                            srv6_address=srv6_addr,
                            source_ipv6=src_ipv6,
                            dest_ipv6=dst_ipv6,
                            hops=[
                                src_host, src_leaf, spine_name,
                                dst_leaf, dst_host,
                            ],
                        ))

                self.paths[key] = path_list
