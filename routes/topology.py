import json
from flask import Blueprint, render_template, request, jsonify, current_app, Response

topology_bp = Blueprint('topology', __name__)

_topology_state = {
    'generator': None,
    'management_ips': {},
    'provisioner': None,
}


def _get_or_create_generator(config_data=None):
    from core.topology_generator import TopologyGenerator, TopologyConfig
    if config_data:
        config = TopologyConfig(
            num_planes=int(config_data.get('num_planes', 2)),
            leafs_per_plane=int(config_data.get('leafs_per_plane', 4)),
            spines_per_plane=int(config_data.get('spines_per_plane', 2)),
            hosts_per_leaf=int(config_data.get('hosts_per_leaf', 1)),
            ipv6_base=config_data.get('ipv6_base', 'fd00::/32'),
            srv6_base=config_data.get('srv6_base', 'fcbb::/32'),
            ceos_image=config_data.get('ceos_image', 'ceos:latest'),
            mrc_image=config_data.get('mrc_image', 'mrc-emulator:latest'),
        )
        gen = TopologyGenerator(config)
        gen.generate()
        _topology_state['generator'] = gen
    return _topology_state['generator']


@topology_bp.route('/topology_builder')
def topology_builder_page():
    return render_template('topology_builder.html')


@topology_bp.route('/topology')
def topology_page():
    gen = _topology_state.get('generator')
    topo_data = gen.to_dict() if gen else None
    mgmt_ips = _topology_state.get('management_ips', {})
    return render_template('topology.html', topo=topo_data, mgmt_ips=mgmt_ips)


@topology_bp.route('/topology_viz')
def topology_viz_page():
    gen = _topology_state.get('generator')
    topo_data = gen.to_dict() if gen else None
    return render_template('topology_viz.html', topo=topo_data, path_states=None)


@topology_bp.route('/simulation')
def simulation_page():
    from core.collectives import CollectiveType
    collective_types = [{'value': ct.name, 'label': ct.name.replace('_', ' ').title()} for ct in CollectiveType]
    gen = _topology_state.get('generator')
    hosts = [n.name for n in gen.nodes if n.role == 'host'] if gen else []
    topo_data = gen.to_dict() if gen else None
    return render_template('simulation.html', collective_types=collective_types,
                           hosts=hosts, topo=topo_data)


@topology_bp.route('/api/topology/generate', methods=['POST'])
def api_generate():
    data = request.json
    gen = _get_or_create_generator(data)
    return jsonify(gen.to_dict())


@topology_bp.route('/api/topology/clab_yaml')
def api_clab_yaml():
    gen = _topology_state.get('generator')
    if gen is None:
        return jsonify({'error': 'Generate topology first'}), 400
    yaml_content = gen.generate_clab_yaml()
    return Response(yaml_content, mimetype='text/yaml',
                    headers={'Content-Disposition': 'attachment; filename=topology.clab.yml'})


@topology_bp.route('/api/topology/addressing')
def api_addressing():
    gen = _topology_state.get('generator')
    if gen is None:
        return jsonify({'error': 'Generate topology first'}), 400
    return jsonify(gen.generate_addressing_plan())


@topology_bp.route('/api/topology/paths/<host_name>')
def api_paths(host_name):
    gen = _topology_state.get('generator')
    if gen is None:
        return jsonify({'error': 'Generate topology first'}), 400
    paths = gen.get_paths_for_host(host_name)
    return jsonify([p.to_dict() if hasattr(p, 'to_dict') else p.__dict__ for p in paths])


@topology_bp.route('/api/topology/ev_profile/<host_name>')
def api_ev_profile(host_name):
    gen = _topology_state.get('generator')
    if gen is None:
        return jsonify({'error': 'Generate topology first'}), 400
    return jsonify(gen.get_ev_profile_for_host(host_name))


@topology_bp.route('/api/topology/management_ips', methods=['POST'])
def api_set_management_ips():
    data = request.json
    _topology_state['management_ips'] = data.get('nodes', {})
    gen = _topology_state.get('generator')
    if gen:
        gen.set_management_ips(data.get('nodes', {}))
    return jsonify({'success': True, 'count': len(data.get('nodes', {}))})


@topology_bp.route('/api/topology/provision', methods=['POST'])
def api_provision():
    data = request.json
    gen = _topology_state.get('generator')
    if gen is None:
        return jsonify({'error': 'Generate topology first'}), 400

    from core.eos_provisioner import EOSProvisioner, EOSCredentials, EOSNodeConfig, FabricProvisioner

    target = data.get('target', 'all')
    username = data.get('username', 'admin')
    password = data.get('password', 'admin')
    mgmt_ips = _topology_state.get('management_ips', {})

    nodes_to_provision = []
    for node in gen.nodes:
        if node.role == 'host':
            continue
        mgmt_ip = mgmt_ips.get(node.name, node.management_ip)
        if not mgmt_ip:
            continue
        if target != 'all' and node.name != target:
            continue

        creds = EOSCredentials(host=mgmt_ip, username=username, password=password)
        node_config = EOSNodeConfig(
            hostname=node.name,
            role=node.role,
            plane=node.plane,
            index=node.index,
            loopback_ipv6=node.loopback_ipv6,
            interfaces=node.interfaces,
            static_routes=[],
            srv6_locator=node.srv6_locator,
            srv6_usid=node.usid,
        )
        nodes_to_provision.append((creds, node_config))

    if not nodes_to_provision:
        return jsonify({'error': 'No nodes to provision. Set management IPs first.'})

    fabric = FabricProvisioner(nodes_to_provision)

    if data.get('action') == 'test':
        results = fabric.test_connectivity()
    elif data.get('action') == 'verify':
        results = fabric.verify_all()
    elif data.get('action') == 'generate_configs':
        results = fabric.generate_all_configs()
    else:
        results = fabric.provision_all()

    return jsonify(results)


@topology_bp.route('/api/topology/configs')
def api_download_configs():
    gen = _topology_state.get('generator')
    if gen is None:
        return jsonify({'error': 'Generate topology first'}), 400

    from core.eos_provisioner import EOSProvisioner, EOSNodeConfig

    provisioner = EOSProvisioner()
    configs = {}
    for node in gen.nodes:
        if node.role == 'host':
            continue
        node_config = EOSNodeConfig(
            hostname=node.name,
            role=node.role,
            plane=node.plane,
            index=node.index,
            loopback_ipv6=node.loopback_ipv6,
            interfaces=node.interfaces,
            static_routes=[],
            srv6_locator=node.srv6_locator,
            srv6_usid=node.usid,
        )
        configs[node.name] = provisioner.generate_startup_config(node_config)

    return jsonify(configs)
