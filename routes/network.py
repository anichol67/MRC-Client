from flask import Blueprint, render_template, request, jsonify, current_app

network_bp = Blueprint('network', __name__)


@network_bp.route('/network')
def network_config():
    net = current_app.net_manager
    routes = net.list_routes()
    neighbors = net.list_neighbors()
    interfaces = net.list_interfaces()
    return render_template('network.html', routes=routes, neighbors=neighbors,
                           interfaces=interfaces, privileged=net.is_privileged())


@network_bp.route('/api/network/routes')
def api_routes():
    return jsonify([r.to_dict() for r in current_app.net_manager.list_routes()])


@network_bp.route('/api/network/routes', methods=['POST'])
def api_add_route():
    data = request.json
    result = current_app.net_manager.add_route(
        data['destination'], data.get('next_hop', ''),
        data.get('interface', ''), data.get('metric', 1024))
    return jsonify(result)


@network_bp.route('/api/network/routes', methods=['DELETE'])
def api_del_route():
    data = request.json
    result = current_app.net_manager.remove_route(
        data['destination'], data.get('next_hop', ''), data.get('interface', ''))
    return jsonify(result)


@network_bp.route('/api/network/neighbors')
def api_neighbors():
    return jsonify([n.to_dict() for n in current_app.net_manager.list_neighbors()])


@network_bp.route('/api/network/neighbors', methods=['POST'])
def api_add_neighbor():
    data = request.json
    result = current_app.net_manager.add_neighbor(data['ipv6'], data['mac'], data['interface'])
    return jsonify(result)


@network_bp.route('/api/network/neighbors', methods=['DELETE'])
def api_del_neighbor():
    data = request.json
    result = current_app.net_manager.remove_neighbor(data['ipv6'], data['interface'])
    return jsonify(result)


@network_bp.route('/api/network/mtu', methods=['POST'])
def api_set_mtu():
    data = request.json
    result = current_app.net_manager.set_mtu(data['interface'], int(data['mtu']))
    return jsonify(result)
