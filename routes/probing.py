from flask import Blueprint, render_template, request, jsonify, current_app

from core.probing import ProbeType

probing_bp = Blueprint('probing', __name__)


@probing_bp.route('/probing')
def probing_page():
    probe_data = current_app.probe_manager.to_dict()
    ev_profiles = current_app.ev_manager.list_profiles()
    return render_template('probing.html', probe_data=probe_data, ev_profiles=ev_profiles)


@probing_bp.route('/api/probing/sessions')
def api_list_sessions():
    return jsonify(current_app.probe_manager.to_dict())


@probing_bp.route('/api/probing/sessions', methods=['POST'])
def api_create_session():
    data = request.json
    session = current_app.probe_manager.create_session(data['target_ipv6'])
    return jsonify(session.to_dict())


@probing_bp.route('/api/probing/probe', methods=['POST'])
def api_create_probe():
    data = request.json
    target = data['target_ipv6']
    session = current_app.probe_manager.get_session(target)
    probe_type = data.get('probe_type', 'EV_PROBE')
    ev_value = int(data.get('ev_value', 0))

    if probe_type == 'RELIABILITY_PROBE':
        probe = session.create_reliability_probe(
            ev_value=ev_value,
            target_qpn=int(data.get('target_qpn', 3)),
            source_qpn=int(data.get('source_qpn', 3)),
        )
    elif probe_type == 'PORT_STATUS_UPDATE':
        probe = session.create_port_status_update(
            port_mask=int(data.get('port_status_mask', 0xF)),
        )
    else:
        probe = session.create_ev_probe(ev_value=ev_value)

    return jsonify({
        'probe_id': probe.probe_id,
        'probe_type': probe.probe_type.name,
        'ev_value': probe.ev_value,
        'target_ipv6': probe.target_ipv6,
    })


@probing_bp.route('/api/probing/probe_all', methods=['POST'])
def api_probe_all_evs():
    data = request.json
    target = data['target_ipv6']
    profile_id = int(data['profile_id'])
    probe_type_str = data.get('probe_type', 'EV_PROBE')
    probe_type = ProbeType[probe_type_str]

    profile = current_app.ev_manager.get_profile(profile_id)
    if profile is None:
        return jsonify({'error': 'Profile not found'}), 404

    probes = current_app.probe_manager.probe_all_evs(target, profile, probe_type)
    return jsonify({
        'count': len(probes),
        'probes': [{'probe_id': p.probe_id, 'ev_value': p.ev_value} for p in probes],
    })


@probing_bp.route('/api/probing/health/<target_ipv6>')
def api_path_health(target_ipv6):
    session = current_app.probe_manager.get_session(target_ipv6)
    return jsonify(session.get_path_health())


@probing_bp.route('/api/probing/timeout', methods=['POST'])
def api_timeout_probes():
    data = request.json
    target = data['target_ipv6']
    timeout = float(data.get('timeout', 5.0))
    session = current_app.probe_manager.get_session(target)
    timed_out = session.timeout_probes(timeout)
    return jsonify({'timed_out': timed_out})
