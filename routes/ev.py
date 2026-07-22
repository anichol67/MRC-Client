from flask import Blueprint, render_template, request, jsonify, current_app

from core.ev_engine import EVMode, EVFormat

ev_bp = Blueprint('ev', __name__)


@ev_bp.route('/ev')
def ev_profiles():
    profiles = current_app.ev_manager.list_profiles()
    return render_template('ev_profiles.html', profiles=profiles)


@ev_bp.route('/api/ev/profiles')
def api_list_profiles():
    return jsonify(current_app.ev_manager.list_profiles())


@ev_bp.route('/api/ev/profiles', methods=['POST'])
def api_create_profile():
    data = request.json
    mode = EVMode[data.get('mode', 'EXPLICIT')]
    ev_format = EVFormat[data.get('ev_format', 'ECMP')]
    profile = current_app.ev_manager.create_profile(
        name=data['name'], mode=mode, ev_format=ev_format)
    return jsonify(profile.to_dict())


@ev_bp.route('/api/ev/profiles/<int:profile_id>', methods=['DELETE'])
def api_delete_profile(profile_id):
    current_app.ev_manager.delete_profile(profile_id)
    return jsonify({'success': True})


@ev_bp.route('/api/ev/profiles/<int:profile_id>')
def api_get_profile(profile_id):
    profile = current_app.ev_manager.get_profile(profile_id)
    if profile is None:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(profile.to_dict())


@ev_bp.route('/api/ev/profiles/<int:profile_id>/evs', methods=['POST'])
def api_add_ev(profile_id):
    data = request.json
    profile = current_app.ev_manager.get_profile(profile_id)
    if profile is None:
        return jsonify({'error': 'Not found'}), 404
    profile.add_ev(data['value'])
    return jsonify(profile.to_dict())


@ev_bp.route('/api/ev/profiles/<int:profile_id>/evs/<int:ev_index>', methods=['DELETE'])
def api_remove_ev(profile_id, ev_index):
    profile = current_app.ev_manager.get_profile(profile_id)
    if profile is None:
        return jsonify({'error': 'Not found'}), 404
    if ev_index < len(profile.ev_universe):
        profile.remove_ev(profile.ev_universe[ev_index].value)
    return jsonify(profile.to_dict())


@ev_bp.route('/api/ev/profiles/<int:profile_id>/evs/<int:ev_index>/state', methods=['POST'])
def api_set_ev_state(profile_id, ev_index):
    data = request.json
    profile = current_app.ev_manager.get_profile(profile_id)
    if profile is None:
        return jsonify({'error': 'Not found'}), 404
    action = data.get('action', '')
    if ev_index < len(profile.ev_universe):
        ev = profile.ev_universe[ev_index]
        if action == 'deny':
            ev.mark_denied()
        elif action == 'enable':
            ev.admin_enable()
        elif action == 'skip':
            ev.mark_skip()
        elif action == 'assumed_bad':
            ev.mark_assumed_bad()
    return jsonify(profile.to_dict())


@ev_bp.route('/api/ev/profiles/<int:profile_id>/generate', methods=['POST'])
def api_generate_evs(profile_id):
    data = request.json
    profile = current_app.ev_manager.get_profile(profile_id)
    if profile is None:
        return jsonify({'error': 'Not found'}), 404
    from core.ev_engine import HopField
    hop_fields = [HopField(**hf) for hf in data.get('hop_fields', [])]
    count = data.get('count', 64)
    profile.generate_evs(hop_fields, count)
    return jsonify(profile.to_dict())


@ev_bp.route('/api/ev/profiles/<int:profile_id>/structured', methods=['POST'])
def api_build_structured(profile_id):
    data = request.json
    profile = current_app.ev_manager.get_profile(profile_id)
    if profile is None:
        return jsonify({'error': 'Not found'}), 404
    hop_values = data.get('hop_values', [])
    ev_value = profile.build_structured_ev(hop_values)
    return jsonify({'ev_value': ev_value, 'hex': f'0x{ev_value:08X}'})
