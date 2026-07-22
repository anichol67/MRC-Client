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
    ceos_image: str = 'ceos:latest'
    mrc_image: str = 'mrc-emulator:latest'
    management_network: str = '172.20.0.0/24'

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

    Switch nodes (spine/leaf) carry full IPv6 and SRv6 addressing.
    Host nodes only carry interface addresses assigned during link
    generation.
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
    """

    source_host: str
    dest_host: str
    plane: int
    spine_index: int
    ev_value: int
    srv6_address: str
    hops: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'source_host': self.source_host,
            'dest_host': self.dest_host,
            'plane': self.plane,
            'spine_index': self.spine_index,
            'ev_value': self.ev_value,
            'srv6_address': self.srv6_address,
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

        The profile covers all planes and spines.  Accepts both physical
        and logical host names; a physical name is promoted to logical so
        the returned profile includes all plane paths.

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
        # Always use the logical name for a complete cross-plane profile
        logical = self._to_logical_host(host_name)
        host_paths = self.get_paths_for_host(logical)

        destinations: dict[str, list[dict]] = {}
        for path in host_paths:
            dest_logical = self._to_logical_host(path.dest_host)
            if dest_logical not in destinations:
                destinations[dest_logical] = []
            destinations[dest_logical].append({
                'ev_value': path.ev_value,
                'srv6_address': path.srv6_address,
                'plane': path.plane,
                'spine_index': path.spine_index,
                'hops': list(path.hops),
            })

        return {
            'host': host_name,
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
                }
            elif node.role == 'host':
                nodes_section[node.name] = {
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

        Spine 0 -> 0x10 (16), Leaf 3 -> 0x23 (35), etc.
        The upper nibble encodes role (1=spine, 2=leaf) and the lower
        nibble encodes the node index within that role.
        """
        r = 1 if role == 'spine' else 2
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
            [7:4]  = role (1=spine, 2=leaf)
            [3:0]  = index
        """
        r = 1 if role == 'spine' else 2
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
        """Convert a physical host name to its logical form.

        ``'p0-host3'`` becomes ``'host3'``; ``'host3'`` is unchanged.
        """
        if '-' in name:
            return name.split('-', 1)[1]
        return name

    @staticmethod
    def _host_matches(source_host: str, query: str) -> bool:
        """Check whether *source_host* matches *query*.

        Exact match always works.  A logical name like ``'host0'`` matches
        any physical name ending with ``'-host0'``.
        """
        if source_host == query:
            return True
        if '-' not in query and source_host.endswith(f'-{query}'):
            return True
        return False

    # -- generation internals ------------------------------------------------

    def _generate_nodes(self) -> None:
        """Create all NodeInfo objects with IPv6 and SRv6 addressing."""
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

            # Host containers (one or more per leaf)
            for li in range(cfg.leafs_per_plane):
                for hi in range(cfg.hosts_per_leaf):
                    host_idx = li * cfg.hosts_per_leaf + hi
                    name = f'p{plane}-host{host_idx}'
                    node = NodeInfo(
                        name=name,
                        role='host',
                        plane=plane,
                        index=host_idx,
                    )
                    self.nodes.append(node)
                    self._node_map[name] = node

    def _generate_links(self) -> None:
        """Create all LinkInfo objects with IPv6 point-to-point addresses.

        Assigns interface names sequentially per node (eth1, eth2, ...).
        eth0 is reserved for management.
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

            # Leaf <-> Host links
            for li in range(cfg.leafs_per_plane):
                leaf_name = f'p{plane}-leaf{li}'
                leaf_rid = self._role_id('leaf', li)

                for hi in range(cfg.hosts_per_leaf):
                    host_idx = li * cfg.hosts_per_leaf + hi
                    host_name = f'p{plane}-host{host_idx}'
                    # Link ID: 0xff for first host, 0xfe for second, etc.
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

                    self._node_map[leaf_name].interfaces[leaf_iface] = addr_leaf
                    self._node_map[host_name].interfaces[host_iface] = addr_host

    def _generate_paths(self) -> None:
        """Enumerate all host-to-host paths with EV and SRv6 mappings.

        Paths are keyed by logical host pair (e.g. ``'host0→host1'``)
        and include entries for every plane and spine combination.  Only
        pairs where source and destination are on different leafs are
        included (same-leaf pairs have no spine-traversal path).
        """
        self.paths.clear()
        cfg = self.config
        hosts_per_plane = cfg.leafs_per_plane * cfg.hosts_per_leaf

        for src_idx in range(hosts_per_plane):
            src_leaf_idx = src_idx // cfg.hosts_per_leaf

            for dst_idx in range(hosts_per_plane):
                dst_leaf_idx = dst_idx // cfg.hosts_per_leaf
                if src_leaf_idx == dst_leaf_idx:
                    continue  # same leaf -- no spine-traversal path

                key = f'host{src_idx}→host{dst_idx}'
                path_list: list[PathInfo] = []

                for plane in range(cfg.num_planes):
                    src_host = f'p{plane}-host{src_idx}'
                    dst_host = f'p{plane}-host{dst_idx}'
                    src_leaf = f'p{plane}-leaf{src_leaf_idx}'
                    dst_leaf = f'p{plane}-leaf{dst_leaf_idx}'
                    src_leaf_usid = self._make_usid(
                        plane, 'leaf', src_leaf_idx,
                    )
                    dst_leaf_usid = self._make_usid(
                        plane, 'leaf', dst_leaf_idx,
                    )

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
                            hops=[
                                src_host, src_leaf, spine_name,
                                dst_leaf, dst_host,
                            ],
                        ))

                self.paths[key] = path_list
