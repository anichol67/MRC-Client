"""Per-path MRC state API — aggregates EV state, probe results, and CC metrics."""

from flask import Blueprint, jsonify, current_app
import time

pathstate_bp = Blueprint('pathstate', __name__)

_path_state_cache: dict = {}


def _build_path_states() -> list[dict]:
    """Aggregate per-path state from EV engine, probing, and CC modules."""
    states = []
    ev_mgr = current_app.ev_manager
    probe_mgr = current_app.probe_manager

    for profile_dict in ev_mgr.list_profiles():
        profile = ev_mgr.get_profile(profile_dict['profile_id'])
        for ev in profile.ev_universe:
            ev_val = ev.value
            state_entry = {
                'ev_value': f'0x{ev_val:08X}',
                'ev_value_int': ev_val,
                'state': ev.state.name,
                'state_code': ev.state.value,
                'rtt_us': 0.0,
                'ecn_rate': 0.0,
                'loss_rate': 0.0,
                'sack_count': 0,
                'nack_count': 0,
                'probe_count': 0,
                'last_update': time.monotonic(),
            }

            for session_dict in probe_mgr.to_dict().get('sessions', {}).values():
                for result in session_dict.get('results', []):
                    if result.get('ev_value') == ev_val:
                        state_entry['probe_count'] += 1
                        state_entry['rtt_us'] = result.get('rtt_us', 0.0)
                        if result.get('m_flag', 0) > 0:
                            state_entry['ecn_rate'] = min(1.0, state_entry['ecn_rate'] + 0.1)
                        if not result.get('reachable', True):
                            state_entry['loss_rate'] = min(1.0, state_entry['loss_rate'] + 0.1)

            states.append(state_entry)

    return states


@pathstate_bp.route('/api/pathstate')
def api_pathstate():
    return jsonify({'paths': _build_path_states()})


@pathstate_bp.route('/api/pathstate/summary')
def api_pathstate_summary():
    states = _build_path_states()
    summary = {
        'total_paths': len(states),
        'good': sum(1 for s in states if s['state'] == 'GOOD'),
        'skip': sum(1 for s in states if s['state'] == 'SKIP'),
        'denied': sum(1 for s in states if s['state'] == 'DENIED'),
        'assumed_bad': sum(1 for s in states if s['state'] == 'ASSUMED_BAD'),
        'avg_rtt_us': sum(s['rtt_us'] for s in states) / max(len(states), 1),
    }
    return jsonify(summary)
