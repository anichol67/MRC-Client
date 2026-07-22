from flask import Blueprint, render_template, request, jsonify, current_app

from core.congestion import NSCCConfig

cc_bp = Blueprint('cc', __name__)


@cc_bp.route('/cc')
def cc_dashboard():
    qpccs = current_app.cc_manager.list_qpccs()
    return render_template('cc.html', qpccs=qpccs)


@cc_bp.route('/api/cc/qpccs')
def api_list_qpccs():
    return jsonify(current_app.cc_manager.list_qpccs())


@cc_bp.route('/api/cc/qpccs', methods=['POST'])
def api_create_qpcc():
    data = request.json or {}
    config = NSCCConfig(
        target_qdelay=float(data.get('target_qdelay', 10.0)),
        min_cwnd=int(data.get('min_cwnd', 4096)),
        max_cwnd=int(data.get('max_cwnd', 16 * 1024 * 1024)),
        initial_cwnd=int(data.get('initial_cwnd', 65536)),
        ai_increment=int(data.get('ai_increment', 4096)),
        md_factor=float(data.get('md_factor', 0.5)),
    )
    qpcc = current_app.cc_manager.create_qpcc(config)
    return jsonify(qpcc.to_dict())


@cc_bp.route('/api/cc/qpccs/<int:qpcc_id>')
def api_get_qpcc(qpcc_id):
    qpcc = current_app.cc_manager.get_qpcc(qpcc_id)
    if qpcc is None:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(qpcc.to_dict())


@cc_bp.route('/api/cc/qpccs/<int:qpcc_id>/map', methods=['POST'])
def api_map_qp(qpcc_id):
    data = request.json
    current_app.cc_manager.map_qp(qpcc_id, int(data['qp_id']))
    return jsonify({'success': True})


@cc_bp.route('/api/cc/qpccs/<int:qpcc_id>/simulate_ack', methods=['POST'])
def api_simulate_ack(qpcc_id):
    """Simulate receiving a SACK for CC testing."""
    data = request.json
    qpcc = current_app.cc_manager.get_qpcc(qpcc_id)
    if qpcc is None:
        return jsonify({'error': 'Not found'}), 404
    import time
    qpcc.on_ack(
        sack_rcvd_bytes=int(data.get('rcvd_bytes', 0)),
        sack_tx_timestamp=int(data.get('tx_timestamp', 0)),
        ecn_marked=data.get('ecn_marked', False),
        rcv_cwnd_pen=int(data.get('rcv_cwnd_pen', 0)),
        restore_cwnd=data.get('restore_cwnd', False),
        rtx_flag=data.get('rtx_flag', False),
        arrival_time=time.monotonic(),
    )
    return jsonify(qpcc.to_dict())
