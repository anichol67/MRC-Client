from flask import Blueprint, render_template, request, jsonify, current_app

host_bp = Blueprint('host', __name__)


@host_bp.route('/')
@host_bp.route('/host')
def host_config():
    net = current_app.net_manager
    interfaces = net.list_interfaces()
    return render_template('host.html', interfaces=interfaces, privileged=net.is_privileged())


@host_bp.route('/api/host/interfaces')
def api_interfaces():
    return jsonify([i.to_dict() for i in current_app.net_manager.list_interfaces()])


@host_bp.route('/api/host/address', methods=['POST'])
def api_add_address():
    data = request.json
    result = current_app.net_manager.add_ipv6_address(data['interface'], data['address'])
    return jsonify(result)


@host_bp.route('/api/host/address', methods=['DELETE'])
def api_remove_address():
    data = request.json
    result = current_app.net_manager.remove_ipv6_address(data['interface'], data['address'])
    return jsonify(result)
