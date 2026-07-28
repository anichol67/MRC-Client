from flask import Blueprint, render_template, request, jsonify

from core.fault_injection import FaultInjector, FaultRule, FaultType, ScenarioRunner

faults_bp = Blueprint('faults', __name__)

_injector = FaultInjector()
_scenario_runner = ScenarioRunner(_injector)


@faults_bp.route('/faults')
def faults_page():
    return render_template('faults.html', injector=_injector.to_dict(),
                           scenario=_scenario_runner.to_dict())


@faults_bp.route('/api/faults/rules')
def api_list_rules():
    return jsonify(_injector.to_dict())


@faults_bp.route('/api/faults/rules', methods=['POST'])
def api_add_rule():
    data = request.json
    rule = FaultRule(
        fault_type=FaultType[data.get('fault_type', 'DROP_RATE')],
        enabled=data.get('enabled', True),
        drop_rate=float(data.get('drop_rate', 0)),
        drop_psns=data.get('drop_psns', []),
        drop_evs=[int(x, 16) if isinstance(x, str) and x.startswith('0x') else int(x)
                  for x in data.get('drop_evs', [])],
        ecn_rate=float(data.get('ecn_rate', 0)),
        ecn_evs=[int(x, 16) if isinstance(x, str) and x.startswith('0x') else int(x)
                 for x in data.get('ecn_evs', [])],
        ecn_m_flag=int(data.get('ecn_m_flag', 1)),
        trim_rate=float(data.get('trim_rate', 0)),
        trim_lasthop=data.get('trim_lasthop', False),
        delay_us=float(data.get('delay_us', 0)),
        description=data.get('description', ''),
    )
    fault_id = _injector.add_rule(rule)
    return jsonify({'fault_id': fault_id, 'rules': _injector.list_rules()})


@faults_bp.route('/api/faults/rules/<int:fault_id>', methods=['DELETE'])
def api_remove_rule(fault_id):
    _injector.remove_rule(fault_id)
    return jsonify({'success': True})


@faults_bp.route('/api/faults/rules/<int:fault_id>/enable', methods=['POST'])
def api_enable_rule(fault_id):
    _injector.enable_rule(fault_id)
    return jsonify({'success': True})


@faults_bp.route('/api/faults/rules/<int:fault_id>/disable', methods=['POST'])
def api_disable_rule(fault_id):
    _injector.disable_rule(fault_id)
    return jsonify({'success': True})


@faults_bp.route('/api/faults/clear', methods=['POST'])
def api_clear_rules():
    _injector.clear_rules()
    return jsonify({'success': True})


@faults_bp.route('/api/faults/evaluate', methods=['POST'])
def api_evaluate():
    data = request.json
    decision = _injector.evaluate(
        psn=int(data.get('psn', 0)),
        ev_value=int(data.get('ev_value', 0)),
    )
    return jsonify(decision.to_dict())


@faults_bp.route('/api/faults/stats')
def api_stats():
    return jsonify(_injector.get_stats())


@faults_bp.route('/api/faults/stats/reset', methods=['POST'])
def api_reset_stats():
    _injector.reset_stats()
    return jsonify({'success': True})


@faults_bp.route('/api/faults/scenarios/<scenario_id>/run', methods=['POST'])
def api_run_scenario(scenario_id):
    data = request.json or {}
    ev_value = int(data.get('ev_value', 0))
    ev_values = [int(x) for x in data.get('ev_values', [])]

    if scenario_id == 'single_link':
        scenario = ScenarioRunner.scenario_single_link_failure(ev_value)
    elif scenario_id == 'spine_failure':
        scenario = ScenarioRunner.scenario_spine_failure(ev_values)
    elif scenario_id == 'plane_failure':
        scenario = ScenarioRunner.scenario_plane_failure(ev_values)
    elif scenario_id == 'partial_degradation':
        scenario = ScenarioRunner.scenario_partial_degradation(ev_value)
    elif scenario_id == 'link_flap':
        scenario = ScenarioRunner.scenario_link_flap(
            ev_value, flap_count=int(data.get('flap_count', 5)),
            interval_ms=int(data.get('interval_ms', 2000)))
    else:
        return jsonify({'error': f'Unknown scenario: {scenario_id}'}), 400

    _scenario_runner.load_scenario(scenario)
    return jsonify(_scenario_runner.to_dict())


@faults_bp.route('/api/faults/scenarios/step', methods=['POST'])
def api_scenario_step():
    has_more = _scenario_runner.advance_step()
    return jsonify({'has_more': has_more, **_scenario_runner.to_dict()})


@faults_bp.route('/api/faults/scenarios/reset', methods=['POST'])
def api_scenario_reset():
    _scenario_runner.reset()
    return jsonify(_scenario_runner.to_dict())


# ---------------------------------------------------------------------------
# Topology-aware click-to-fail
# ---------------------------------------------------------------------------

_topo_fault_map: dict[str, int] = {}


@faults_bp.route('/api/faults/topology_fail', methods=['POST'])
def api_topology_fail():
    """Inject or clear a failure on a topology link or node.

    The backend resolves which EVs/paths traverse the element using the
    topology path data, so the user never needs to know EV values.

    Request body::

        {
            "element_type": "link" | "node",
            "element_id": "p0-leaf0↔p0-spine1" | "p0-spine1",
            "failure_mode": "fail" | "ecn" | "partial_loss" | "restore"
        }
    """
    try:
        from routes.topology import _topology_state
        gen = _topology_state.get('generator')
    except ImportError:
        gen = None
    if gen is None:
        return jsonify({'error': 'No topology generated'}), 400

    data = request.json or {}
    element_type = data.get('element_type', 'node')
    element_id = data.get('element_id', '')
    failure_mode = data.get('failure_mode', 'fail')

    if element_type == 'link':
        lookup = element_id
    else:
        lookup = element_id

    affected_paths = gen.get_paths_through_element(lookup)
    if not affected_paths:
        return jsonify({'error': f'No paths traverse {element_id}'}), 404

    affected_evs = sorted(set(p.ev_value for p in affected_paths))

    if failure_mode == 'restore':
        fault_id = _topo_fault_map.pop(element_id, None)
        if fault_id is not None:
            try:
                _injector.remove_rule(fault_id)
            except ValueError:
                pass
        # Recover affected EVs in the simulator's profile (simulates probe response)
        try:
            from routes.traffic import _traffic_state
            sim = _traffic_state.get('simulator')
            if sim and sim.ev_profile:
                for ev in sim.ev_profile.ev_universe:
                    if ev.value in affected_evs:
                        ev.probe_resolved()
                        sim._log_event(
                            'PROBE_RESPONSE',
                            f'EV=0x{ev.value:04X} probe response m=NONE '
                            f'→ ASSUMED_BAD → GOOD (path restored)',
                            '§9.3.1, Fig 5',
                            ev_value=ev.value,
                        )
        except (ImportError, AttributeError):
            pass
        return jsonify({
            'action': 'restored',
            'element_id': element_id,
            'affected_evs': [f'0x{ev:04X}' for ev in affected_evs],
        })

    if element_id in _topo_fault_map:
        try:
            _injector.remove_rule(_topo_fault_map[element_id])
        except ValueError:
            pass

    if failure_mode == 'fail':
        rule = FaultRule(
            fault_type=FaultType.DROP_EV,
            drop_evs=list(affected_evs),
            description=f'Topology fail: {element_type} {element_id}',
        )
    elif failure_mode == 'ecn':
        rule = FaultRule(
            fault_type=FaultType.ECN_MARK,
            ecn_evs=list(affected_evs),
            ecn_m_flag=1,
            description=f'Topology ECN degrade: {element_type} {element_id}',
        )
    elif failure_mode == 'partial_loss':
        rule = FaultRule(
            fault_type=FaultType.DROP_RATE,
            drop_rate=0.30,
            drop_evs=list(affected_evs),
            description=f'Topology partial loss: {element_type} {element_id}',
        )
    else:
        return jsonify({'error': f'Unknown failure_mode: {failure_mode}'}), 400

    fault_id = _injector.add_rule(rule)
    _topo_fault_map[element_id] = fault_id

    return jsonify({
        'action': failure_mode,
        'element_id': element_id,
        'fault_id': fault_id,
        'affected_evs': [f'0x{ev:04X}' for ev in affected_evs],
        'affected_paths': len(affected_paths),
    })


@faults_bp.route('/api/faults/topology_state')
def api_topology_fault_state():
    """Return currently active topology-level failures."""
    return jsonify({
        'active_failures': {
            element_id: fault_id
            for element_id, fault_id in _topo_fault_map.items()
        },
    })
