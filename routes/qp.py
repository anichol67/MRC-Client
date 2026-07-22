from flask import Blueprint, render_template, request, jsonify, current_app

qp_bp = Blueprint('qp', __name__)


@qp_bp.route('/qp')
def qp_config():
    qps = current_app.qp_manager.list_qps()
    ev_profiles = current_app.ev_manager.list_profiles()
    return render_template('qp.html', qps=qps, ev_profiles=ev_profiles)


@qp_bp.route('/api/qp')
def api_list_qps():
    return jsonify(current_app.qp_manager.list_qps())


@qp_bp.route('/api/qp', methods=['POST'])
def api_create_qp():
    data = request.json or {}
    qp = current_app.qp_manager.create_qp(
        src_ipv6=data.get('src_ipv6', '::1'),
        src_mac=data.get('src_mac', '00:00:00:00:00:00'),
        max_psn_range=int(data.get('max_psn_range', 128)),
        max_wimm_inflight=int(data.get('max_wimm_inflight', 32)),
    )
    return jsonify(qp.to_dict())


@qp_bp.route('/api/qp/<int:qpn>', methods=['PUT'])
def api_modify_qp(qpn):
    data = request.json or {}
    try:
        qp = current_app.qp_manager.modify_qp(qpn, **data)
        return jsonify(qp.to_dict())
    except (KeyError, ValueError) as e:
        return jsonify({'error': str(e)}), 400


@qp_bp.route('/api/qp/<int:qpn>', methods=['DELETE'])
def api_destroy_qp(qpn):
    current_app.qp_manager.destroy_qp(qpn)
    return jsonify({'success': True})


@qp_bp.route('/api/qp/<int:qpn>')
def api_get_qp(qpn):
    qp = current_app.qp_manager.get_qp(qpn)
    if qp is None:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(qp.to_dict())
