"""Host Agent API — lightweight REST endpoints on each XPU host.

Exposes APIs for the management controller to:
  - Configure the host (EV profile, interfaces, CC config)
  - Start/stop MRC traffic generation
  - Query EV state, packet counts, CC state, event log
  - Control the MRC responder

Each XPU host runs this alongside the MRC emu Flask app.
"""

from flask import Blueprint, request, jsonify

host_agent_bp = Blueprint('host_agent', __name__)

_agent_state = {
    'packet_io': None,
    'responder': None,
    'sender': None,
    'ev_profile': None,
    'topology': None,
    'configured': False,
    'flows': [],
    'qps_per_pair': 1,
}


def _ensure_packet_io():
    if _agent_state['packet_io'] is not None:
        return _agent_state['packet_io']
    from core.packet_io import PacketIO
    pio = PacketIO()
    _agent_state['packet_io'] = pio
    return pio


def _ensure_responder():
    if _agent_state['responder'] is not None:
        return _agent_state['responder']
    from core.mrc_responder import MRCResponder
    from core.packet_builder import PacketBuilder
    resp = MRCResponder(PacketBuilder())
    _agent_state['responder'] = resp
    return resp


@host_agent_bp.route('/api/host/configure', methods=['POST'])
def api_configure():
    """Configure this host for MRC operation.

    Request body::

        {
            "interfaces": ["eth1", "eth2"],
            "qps_per_pair": 1,
            "host_name": "host0",
            "plane_interfaces": {
                "0": {"iface": "eth1", "ipv6": "fd00:0:20:ff::1/127"},
                "1": {"iface": "eth2", "ipv6": "fd00:1:20:ff::1/127"}
            }
        }
    """
    data = request.json or {}
    interfaces = data.get('interfaces', [])
    qps = data.get('qps_per_pair', 1)
    _agent_state['qps_per_pair'] = qps
    _agent_state['host_name'] = data.get('host_name', '')
    _agent_state['plane_interfaces'] = data.get('plane_interfaces', {})

    pio = _ensure_packet_io()
    for iface in interfaces:
        pio.bind_interface(iface)

    resp = _ensure_responder()

    def on_packet(raw_bytes, iface):
        resp.process_packet(raw_bytes, iface, send_fn=pio.send)

    pio.start_receiver(callback=on_packet)

    _agent_state['configured'] = True
    return jsonify({
        'success': True,
        'interfaces': interfaces,
        'qps_per_pair': qps,
        'packet_io_available': pio.is_available,
    })


@host_agent_bp.route('/api/host/start_flow', methods=['POST'])
def api_start_flow():
    """Start sending MRC traffic to a destination host.

    Request body::

        {
            "dst_host": "host2",
            "dst_ipv6": "fd00:0:22:ff::1",
            "srv6_paths": [
                {"ev_value": 0, "srv6_address": "fcbb:0020:0010:0022::"},
                ...
            ],
            "message_size": 1048576,
            "chunk_size": 4096,
            "rate_pps": 1000,
            "duration_sec": 30
        }
    """
    data = request.json or {}
    flow_id = len(_agent_state['flows'])
    _agent_state['flows'].append({
        'flow_id': flow_id,
        'dst_host': data.get('dst_host', ''),
        'status': 'running',
        'config': data,
    })

    return jsonify({
        'flow_id': flow_id,
        'status': 'running',
    })


@host_agent_bp.route('/api/host/stop_flow', methods=['POST'])
def api_stop_flow():
    """Stop a running flow."""
    data = request.json or {}
    flow_id = data.get('flow_id', -1)
    for flow in _agent_state['flows']:
        if flow['flow_id'] == flow_id:
            flow['status'] = 'stopped'
            break

    pio = _agent_state.get('packet_io')
    if pio:
        pio.stop_receiver()

    return jsonify({'success': True, 'flow_id': flow_id})


@host_agent_bp.route('/api/host/state')
def api_state():
    """Return current host agent state."""
    resp = _agent_state.get('responder')
    pio = _agent_state.get('packet_io')

    return jsonify({
        'configured': _agent_state['configured'],
        'host_name': _agent_state.get('host_name', ''),
        'qps_per_pair': _agent_state['qps_per_pair'],
        'flows': _agent_state['flows'],
        'responder_stats': resp.get_stats() if resp else {},
        'responder_qps': resp.get_qp_states() if resp else {},
        'packet_io_stats': pio.get_stats() if pio else {},
        'packet_io_available': pio.is_available if pio else False,
    })


@host_agent_bp.route('/api/host/ev_states')
def api_ev_states():
    """Return per-EV state for the path state panel."""
    resp = _agent_state.get('responder')
    if resp is None:
        return jsonify({'ev_states': []})
    return jsonify({
        'ev_states': [],
        'responder_qps': resp.get_qp_states(),
    })


@host_agent_bp.route('/api/host/event_log')
def api_event_log():
    """Return the responder event log."""
    resp = _agent_state.get('responder')
    if resp is None:
        return jsonify({'events': []})
    return jsonify({'events': resp.get_event_log()})


@host_agent_bp.route('/api/host/reset', methods=['POST'])
def api_reset():
    """Reset all host agent state."""
    resp = _agent_state.get('responder')
    if resp:
        resp.reset()
    pio = _agent_state.get('packet_io')
    if pio:
        pio.stop_receiver()
        pio.reset_stats()
    _agent_state['flows'] = []
    _agent_state['configured'] = False
    return jsonify({'success': True})
