"""Controller API — orchestrates flows across remote XPU hosts.

The controller is a management-only node that:
  - Discovers and manages remote XPU hosts via management IPs
  - Sends configuration and flow commands to host agents
  - Aggregates state (EV, CC, packet counts, events) from all hosts
  - Serves the unified GUI view
"""

from flask import Blueprint, request, jsonify
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

controller_bp = Blueprint('controller', __name__)

_controller_state = {
    'hosts': {
        'host0': {'mgmt_ip': '172.20.0.51', 'status': 'registered', 'port': 5001},
        'host1': {'mgmt_ip': '172.20.0.52', 'status': 'registered', 'port': 5001},
        'host2': {'mgmt_ip': '172.20.0.53', 'status': 'registered', 'port': 5001},
        'host3': {'mgmt_ip': '172.20.0.54', 'status': 'registered', 'port': 5001},
    },
    'mode': 'offline',
}


def _host_api_call(host_ip: str, path: str, method: str = 'GET',
                   body: dict = None, port: int = 8080,
                   timeout: int = 5) -> dict:
    """Call a remote host agent API endpoint."""
    url = f'http://{host_ip}:{port}{path}'
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={'Content-Type': 'application/json'} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.debug('Host API call failed: %s %s: %s', method, url, exc)
        return {'error': str(exc)}


@controller_bp.route('/api/controller/hosts', methods=['GET'])
def api_list_hosts():
    """List registered remote hosts."""
    return jsonify({'hosts': _controller_state['hosts'],
                    'mode': _controller_state['mode']})


@controller_bp.route('/api/controller/hosts', methods=['POST'])
def api_register_hosts():
    """Register remote hosts for orchestration.

    Request body::

        {
            "hosts": {
                "host0": {"mgmt_ip": "172.20.0.10"},
                "host1": {"mgmt_ip": "172.20.0.11"},
                ...
            }
        }
    """
    data = request.json or {}
    hosts = data.get('hosts', {})
    for name, info in hosts.items():
        _controller_state['hosts'][name] = {
            'mgmt_ip': info.get('mgmt_ip', ''),
            'status': 'registered',
            'port': info.get('port', 8080),
        }
    return jsonify({'success': True, 'host_count': len(_controller_state['hosts'])})


@controller_bp.route('/api/controller/discover', methods=['POST'])
def api_discover():
    """Test connectivity to all registered hosts and update status."""
    results = {}
    for name, info in _controller_state['hosts'].items():
        resp = _host_api_call(info['mgmt_ip'], '/api/host/state',
                              port=info.get('port', 8080))
        if 'error' not in resp:
            info['status'] = 'reachable'
            results[name] = 'reachable'
        else:
            info['status'] = 'unreachable'
            results[name] = 'unreachable'

    reachable = sum(1 for v in results.values() if v == 'reachable')
    _controller_state['mode'] = 'live' if reachable > 0 else 'offline'

    return jsonify({
        'results': results,
        'mode': _controller_state['mode'],
    })


@controller_bp.route('/api/controller/configure_hosts', methods=['POST'])
def api_configure_hosts():
    """Push configuration to all registered hosts."""
    data = request.json or {}
    qps_per_pair = data.get('qps_per_pair', 1)

    try:
        from routes.topology import _topology_state
        gen = _topology_state.get('generator')
    except (ImportError, AttributeError):
        gen = None

    results = {}
    for name, info in _controller_state['hosts'].items():
        if info['status'] != 'reachable':
            results[name] = {'error': 'host unreachable'}
            continue

        host_config = {'interfaces': [], 'qps_per_pair': qps_per_pair,
                       'host_name': name}

        if gen:
            node = gen.get_node_by_name(name)
            if node and node.plane_interfaces:
                ifaces = []
                pi_dict = {}
                for plane, pi in sorted(node.plane_interfaces.items()):
                    ifaces.append(pi['iface'])
                    pi_dict[str(plane)] = pi
                host_config['interfaces'] = ifaces
                host_config['plane_interfaces'] = pi_dict

        resp = _host_api_call(
            info['mgmt_ip'], '/api/host/configure', 'POST',
            body=host_config, port=info.get('port', 8080),
        )
        results[name] = resp

    return jsonify({'results': results})


@controller_bp.route('/api/controller/start_flow', methods=['POST'])
def api_start_flow():
    """Start a flow between two hosts.

    Request body::

        {
            "src_host": "host0",
            "dst_host": "host2",
            "bidirectional": false,
            "message_size": 1048576,
            "chunk_size": 4096,
            "rate_pps": 1000,
            "duration_sec": 30
        }
    """
    data = request.json or {}
    src = data.get('src_host', '')
    dst = data.get('dst_host', '')

    # Auto-discover hosts if not yet reachable
    for name, info in _controller_state['hosts'].items():
        if info['status'] != 'reachable':
            resp = _host_api_call(info['mgmt_ip'], '/api/host/state',
                                  port=info.get('port', 5001))
            if 'error' not in resp:
                info['status'] = 'reachable'

    src_info = _controller_state['hosts'].get(src)
    if not src_info or src_info['status'] != 'reachable':
        return jsonify({'error': f'Source host {src} not reachable'}), 400

    flow_config = dict(data)

    try:
        from routes.topology import _topology_state
        gen = _topology_state.get('generator')
        if gen:
            profile = gen.get_ev_profile_for_host(src)
            if dst in profile.get('destinations', {}):
                flow_config['srv6_paths'] = profile['destinations'][dst]
    except (ImportError, AttributeError):
        pass

    resp = _host_api_call(
        src_info['mgmt_ip'], '/api/host/start_flow', 'POST',
        body=flow_config, port=src_info.get('port', 8080),
    )

    result = {'src': src, 'dst': dst, 'response': resp}

    if data.get('bidirectional'):
        dst_info = _controller_state['hosts'].get(dst)
        if dst_info and dst_info['status'] == 'reachable':
            rev_config = dict(data)
            rev_config['src_host'] = dst
            rev_config['dst_host'] = src
            rev_resp = _host_api_call(
                dst_info['mgmt_ip'], '/api/host/start_flow', 'POST',
                body=rev_config, port=dst_info.get('port', 8080),
            )
            result['reverse'] = rev_resp

    return jsonify(result)


@controller_bp.route('/api/controller/stop_flow', methods=['POST'])
@controller_bp.route('/api/controller/stop_all', methods=['POST'])
def api_stop_all():
    """Stop all flows on all hosts."""
    results = {}
    for name, info in _controller_state['hosts'].items():
        if info['status'] != 'reachable':
            continue
        resp = _host_api_call(
            info['mgmt_ip'], '/api/host/stop_flow', 'POST',
            body={'flow_id': -1}, port=info.get('port', 8080),
        )
        results[name] = resp
    return jsonify({'results': results})


@controller_bp.route('/api/controller/aggregate_state')
def api_aggregate_state():
    """Aggregate state from all hosts for the unified GUI."""
    states = {}
    for name, info in _controller_state['hosts'].items():
        if info['status'] != 'reachable':
            states[name] = {'error': 'unreachable'}
            continue
        resp = _host_api_call(
            info['mgmt_ip'], '/api/host/state',
            port=info.get('port', 8080),
        )
        states[name] = resp

    return jsonify({
        'mode': _controller_state['mode'],
        'hosts': states,
    })


@controller_bp.route('/api/controller/aggregate_events')
def api_aggregate_events():
    """Aggregate event logs from all hosts."""
    all_events = []
    for name, info in _controller_state['hosts'].items():
        if info['status'] != 'reachable':
            continue
        resp = _host_api_call(
            info['mgmt_ip'], '/api/host/event_log',
            port=info.get('port', 8080),
        )
        events = resp.get('events', [])
        for evt in events:
            evt['host'] = name
        all_events.extend(events)

    all_events.sort(key=lambda e: e.get('time', 0))
    return jsonify({'events': all_events})


@controller_bp.route('/api/controller/mode')
def api_mode():
    """Return current operating mode (live or offline)."""
    return jsonify({'mode': _controller_state['mode']})
