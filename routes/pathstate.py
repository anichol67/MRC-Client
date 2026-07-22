"""Per-path MRC state API — aggregates EV state, fault injection, simulation, and probe data."""

from flask import Blueprint, jsonify, current_app
import time

pathstate_bp = Blueprint('pathstate', __name__)


def _get_fault_injector():
    try:
        from routes.faults import _injector
        return _injector
    except ImportError:
        return None


def _get_simulator():
    try:
        from routes.traffic import _traffic_state
        return _traffic_state.get('simulator')
    except ImportError:
        return None


def _build_path_states() -> list[dict]:
    """Aggregate per-path state from EV engine, fault rules, simulation, and probing."""
    states = []
    ev_mgr = current_app.ev_manager
    probe_mgr = current_app.probe_manager
    fault_injector = _get_fault_injector()
    simulator = _get_simulator()

    # Collect dropped EVs from fault injector rules
    dropped_evs = set()
    ecn_evs = set()
    if fault_injector:
        for rule in fault_injector.rules:
            if not rule.enabled:
                continue
            if rule.drop_evs:
                dropped_evs.update(rule.drop_evs)
            if rule.ecn_evs:
                ecn_evs.update(rule.ecn_evs)

    # Get simulation EV state if available
    sim_ev_states = {}
    if simulator:
        try:
            sim_state = simulator.get_state()
            if sim_state.get('ev_states'):
                for ev_entry in sim_state['ev_states']:
                    sim_ev_states[ev_entry.get('value', ev_entry.get('ev_value', -1))] = ev_entry.get('state', 'GOOD')
        except (AttributeError, TypeError):
            pass
        # Also check the ev_profile directly from the simulator
        try:
            if simulator.ev_profile:
                for ev in simulator.ev_profile.ev_universe:
                    sim_ev_states[ev.value] = ev.state.name
        except (AttributeError, TypeError):
            pass

    for profile_dict in ev_mgr.list_profiles():
        profile = ev_mgr.get_profile(profile_dict['profile_id'])
        for ev in profile.ev_universe:
            ev_val = ev.value

            # Determine state: fault rules override, then simulation state, then EV engine state
            if ev_val in dropped_evs:
                state_name = 'ASSUMED_BAD'
            elif ev_val in sim_ev_states:
                state_name = sim_ev_states[ev_val]
            else:
                state_name = ev.state.name

            ecn_rate = 0.0
            if ev_val in ecn_evs:
                ecn_rate = 1.0

            state_entry = {
                'ev_value': f'0x{ev_val:08X}',
                'ev_value_int': ev_val,
                'state': state_name,
                'state_code': {'GOOD': 0, 'DENIED': 1, 'SKIP': 2, 'ASSUMED_BAD': 3}.get(state_name, 0),
                'rtt_us': 0.0,
                'ecn_rate': ecn_rate,
                'loss_rate': 1.0 if ev_val in dropped_evs else 0.0,
                'sack_count': 0,
                'nack_count': 0,
                'probe_count': 0,
                'last_update': time.monotonic(),
            }

            # Enrich from probe results
            for session_dict in probe_mgr.to_dict().get('sessions', {}).values():
                for result in session_dict.get('results', []):
                    if result.get('ev_value') == ev_val:
                        state_entry['probe_count'] += 1
                        state_entry['rtt_us'] = result.get('rtt_us', 0.0)
                        if result.get('m_flag', 0) > 0:
                            state_entry['ecn_rate'] = min(1.0, state_entry['ecn_rate'] + 0.1)
                        if not result.get('reachable', True):
                            state_entry['loss_rate'] = min(1.0, state_entry['loss_rate'] + 0.1)

            # Enrich from simulator stats
            if simulator:
                try:
                    stats = simulator.get_state()
                    state_entry['rtt_us'] = stats.get('rtt_estimate', state_entry['rtt_us']) or state_entry['rtt_us']
                except (AttributeError, TypeError):
                    pass

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
