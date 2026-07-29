#!/usr/bin/env python3
"""Generate default Containerlab deployment files.

Creates topology.clab.yml and configs/ directory with all EOS switch
startup configs and host startup scripts for the default 2-plane,
4-leaf, 2-spine fabric with 4 hosts and a management controller.

Usage::

    python3 generate_deployment.py [--output-dir deploy]
"""

import argparse
import os
import sys

from core.topology_generator import TopologyGenerator, TopologyConfig
from core.eos_provisioner import EOSProvisioner, EOSNodeConfig


def generate(output_dir: str = '.', config: dict = None) -> None:
    cfg_args = config or {}
    topo_config = TopologyConfig(
        num_planes=cfg_args.get('num_planes', 2),
        leafs_per_plane=cfg_args.get('leafs_per_plane', 4),
        spines_per_plane=cfg_args.get('spines_per_plane', 2),
        hosts_per_leaf=cfg_args.get('hosts_per_leaf', 1),
        ipv6_base=cfg_args.get('ipv6_base', 'fd00::/32'),
        srv6_base=cfg_args.get('srv6_base', 'fcbb::/32'),
        ceos_image=cfg_args.get('ceos_image', 'arista/ceos:latest'),
        mrc_image=cfg_args.get('mrc_image', 'ghcr.io/anichol67/mrc-emu:latest'),
    )

    gen = TopologyGenerator(topo_config)
    gen.generate()

    configs_dir = os.path.join(output_dir, 'configs')
    os.makedirs(configs_dir, exist_ok=True)

    # Generate .clab.yml
    clab_yaml = gen.generate_clab_yaml()
    clab_path = os.path.join(output_dir, 'topology.clab.yml')
    with open(clab_path, 'w') as f:
        f.write(clab_yaml)
    print(f'  {clab_path}')

    # Generate EOS switch configs
    eos_configs = gen.generate_eos_node_configs()
    for cfg_dict in eos_configs:
        node_config = EOSNodeConfig(**cfg_dict)
        startup = EOSProvisioner.generate_startup_config(node_config)
        cfg_path = os.path.join(configs_dir, f'{cfg_dict["hostname"]}.cfg')
        with open(cfg_path, 'w') as f:
            f.write(startup)
        print(f'  {cfg_path}')

    # Generate host startup scripts
    hosts = [n for n in gen.nodes if n.role == 'host']
    for host in hosts:
        script = gen.generate_host_startup_script(host.name)
        script_path = os.path.join(configs_dir, f'{host.name}startup.sh')
        with open(script_path, 'w') as f:
            f.write(script)
        os.chmod(script_path, 0o755)
        print(f'  {script_path}')

    # Summary
    num_switches = len(eos_configs)
    num_hosts = len(hosts)
    print(f'\nGenerated: {num_switches} switch configs, '
          f'{num_hosts} host scripts, 1 topology file')
    print(f'Fabric: {topo_config.num_planes} planes, '
          f'{topo_config.leafs_per_plane} leafs/plane, '
          f'{topo_config.spines_per_plane} spines/plane, '
          f'{num_hosts} hosts')
    print(f'\nDeploy:')
    print(f'  docker build -t mrc-emu .')
    print(f'  cd {output_dir} && containerlab deploy -t topology.clab.yml')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Generate Containerlab deployment files for MRC fabric',
    )
    parser.add_argument(
        '--output-dir', default='.',
        help='Output directory (default: current directory)',
    )
    parser.add_argument('--planes', type=int, default=2)
    parser.add_argument('--leafs', type=int, default=4)
    parser.add_argument('--spines', type=int, default=2)
    parser.add_argument('--hosts-per-leaf', type=int, default=1)
    parser.add_argument('--ceos-image', default='arista/ceos:latest')
    parser.add_argument('--ipv6-base', default='fd00::/32')
    parser.add_argument('--srv6-base', default='fcbb::/32')
    args = parser.parse_args()

    print('Generating MRC fabric deployment files...')
    generate(
        output_dir=args.output_dir,
        config={
            'num_planes': args.planes,
            'leafs_per_plane': args.leafs,
            'spines_per_plane': args.spines,
            'hosts_per_leaf': args.hosts_per_leaf,
            'ceos_image': args.ceos_image,
            'ipv6_base': args.ipv6_base,
            'srv6_base': args.srv6_base,
        },
    )
