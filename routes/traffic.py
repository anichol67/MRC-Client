from flask import Blueprint, render_template, request, jsonify, current_app

traffic_bp = Blueprint('traffic', __name__)

_traffic_state = {
    'orchestrator': None,
    'plan': None,
    'simulator': None,
}


@traffic_bp.route('/traffic')
def traffic_page():
    from core.collectives import CollectiveType
    collective_types = [{'value': ct.name, 'label': ct.name.replace('_', ' ').title()} for ct in CollectiveType]
    gen = None
    try:
        from routes.topology import _topology_state
        gen = _topology_state.get('generator')
    except ImportError:
        pass
    hosts = []
    if gen:
        hosts = [n.name for n in gen.nodes if n.role == 'host']
    plan = _traffic_state.get('plan')
    orch = _traffic_state.get('orchestrator')
    return render_template('traffic.html', collective_types=collective_types,
                           hosts=hosts, plan=plan, orchestrator=orch)


def _create_simulator(ev_profile_mode: str = 'auto'):
    """Create a TrafficSimulator with the appropriate EV profile.

    Args:
        ev_profile_mode: ``'auto'`` builds an EV profile from the topology's
            path mappings.  ``'manual'`` uses an existing profile from the
            EV engine page.
    """
    from core.traffic_simulator import TrafficSimulator, create_ev_profile_from_topology
    from core.packet_builder import PacketBuilder
    from core.fault_injection import FaultInjector

    pb = PacketBuilder()
    fi = FaultInjector()

    try:
        from routes.faults import _injector as existing_fi
        fi = existing_fi
    except ImportError:
        pass

    topology = None
    try:
        from routes.topology import _topology_state
        topology = _topology_state.get('generator')
    except (ImportError, AttributeError):
        pass

    ev_profile = None
    if ev_profile_mode == 'manual':
        try:
            from routes.ev import _ev_state
            manager = _ev_state.get('manager')
            if manager is not None:
                profiles = manager.list_profiles()
                if profiles:
                    ev_profile = manager.get_profile(profiles[0]['profile_id'])
        except (ImportError, AttributeError, KeyError):
            pass

    return TrafficSimulator(pb, fi, ev_profile=ev_profile, topology=topology)


@traffic_bp.route('/api/traffic/generate', methods=['POST'])
def api_generate_plan():
    from core.collectives import CollectiveGenerator, CollectiveConfig, CollectiveType

    data = request.json
    ct = CollectiveType[data.get('collective_type', 'ALLREDUCE_RING')]
    hosts = data.get('hosts', [])
    if len(hosts) < 2:
        return jsonify({'error': 'Need at least 2 hosts'}), 400

    config = CollectiveConfig(
        collective_type=ct,
        hosts=hosts,
        message_size=int(data.get('message_size', 1048576)),
        chunk_size=int(data.get('chunk_size', 4096)),
        root_host=data.get('root_host', hosts[0]),
        ring_order=data.get('ring_order', []),
    )
    generator = CollectiveGenerator(config)
    plan = generator.generate()
    _traffic_state['plan'] = plan

    from core.collectives import TrafficOrchestrator
    orch = TrafficOrchestrator()
    orch.load_plan(plan)
    _traffic_state['orchestrator'] = orch

    ev_profile_mode = data.get('ev_profile_mode', 'auto')
    sim = _create_simulator(ev_profile_mode)
    _traffic_state['simulator'] = sim

    return jsonify(plan.to_dict())


@traffic_bp.route('/api/traffic/plan')
def api_get_plan():
    plan = _traffic_state.get('plan')
    if plan is None:
        return jsonify({'error': 'No plan generated'}), 400
    return jsonify(plan.to_dict())


@traffic_bp.route('/api/traffic/step')
def api_current_step():
    orch = _traffic_state.get('orchestrator')
    if orch is None:
        return jsonify({'error': 'No plan loaded'}), 400
    flows = orch.get_current_step_flows()
    return jsonify({
        'progress': orch.get_progress(),
        'flows': [f.__dict__ if not hasattr(f, 'to_dict') else f.to_dict() for f in flows],
    })


@traffic_bp.route('/api/traffic/advance', methods=['POST'])
def api_advance_step():
    orch = _traffic_state.get('orchestrator')
    if orch is None:
        return jsonify({'error': 'No plan loaded'}), 400
    has_more = orch.advance_step()
    return jsonify({
        'has_more': has_more,
        'progress': orch.get_progress(),
    })


@traffic_bp.route('/api/traffic/reset', methods=['POST'])
def api_reset():
    orch = _traffic_state.get('orchestrator')
    if orch is None:
        return jsonify({'error': 'No plan loaded'}), 400
    orch.reset()
    return jsonify({'progress': orch.get_progress()})


# ---------------------------------------------------------------------------
# Simulation endpoints
# ---------------------------------------------------------------------------

def _ensure_simulator():
    """Return the simulator or create one lazily if a plan exists."""
    sim = _traffic_state.get('simulator')
    if sim is not None:
        return sim
    plan = _traffic_state.get('plan')
    if plan is None:
        return None

    sim = _create_simulator('auto')
    _traffic_state['simulator'] = sim
    return sim


@traffic_bp.route('/api/traffic/simulate_step', methods=['POST'])
def api_simulate_step():
    """Run one step of the loaded plan through the TrafficSimulator."""
    sim = _ensure_simulator()
    if sim is None:
        return jsonify({'error': 'No plan loaded. Generate a plan first.'}), 400

    plan = _traffic_state.get('plan')
    if plan is None:
        return jsonify({'error': 'No plan loaded'}), 400

    step_idx = sim._current_step
    if step_idx >= plan.num_steps:
        return jsonify({'error': 'All steps already simulated', 'completed': True}), 400

    step_flows = plan.steps[step_idx]
    result = sim.run_step(step_flows, step_num=step_idx)
    return jsonify({
        'step_result': result.to_dict(),
        'state': sim.get_state(),
        'completed': sim._current_step >= plan.num_steps,
    })


@traffic_bp.route('/api/traffic/simulate_all', methods=['POST'])
def api_simulate_all():
    """Run the full plan through the TrafficSimulator."""
    sim = _ensure_simulator()
    if sim is None:
        return jsonify({'error': 'No plan loaded. Generate a plan first.'}), 400

    plan = _traffic_state.get('plan')
    if plan is None:
        return jsonify({'error': 'No plan loaded'}), 400

    # Reset the simulator before a full run
    sim.reset()
    result = sim.run_full_plan(plan)
    return jsonify({
        'simulation_result': result.to_dict(),
        'state': sim.get_state(),
    })


@traffic_bp.route('/api/traffic/simulation_state')
def api_simulation_state():
    """Get current simulator state."""
    sim = _traffic_state.get('simulator')
    if sim is None:
        return jsonify({'error': 'No simulator initialized'}), 400
    return jsonify(sim.get_state())


@traffic_bp.route('/api/traffic/simulation_reset', methods=['POST'])
def api_simulation_reset():
    """Reset the simulator state."""
    sim = _traffic_state.get('simulator')
    if sim is None:
        return jsonify({'error': 'No simulator initialized'}), 400
    sim.reset()
    return jsonify({'state': sim.get_state()})


@traffic_bp.route('/api/traffic/set_probe_mode', methods=['POST'])
def api_set_probe_mode():
    """Set the probe mode on the simulator."""
    sim = _traffic_state.get('simulator')
    if sim is None:
        return jsonify({'error': 'No simulator initialized'}), 400
    data = request.json or {}
    mode = data.get('probe_mode', 'auto')
    sim.auto_probe = (mode == 'auto')
    return jsonify({'auto_probe': sim.auto_probe})


@traffic_bp.route('/api/traffic/clear_counters', methods=['POST'])
def api_clear_counters():
    """Clear packet counters while preserving EV state and simulation position."""
    sim = _traffic_state.get('simulator')
    if sim is None:
        return jsonify({'error': 'No simulator initialized'}), 400
    sim._ev_packet_counts = {}
    sim._total_packets = 0
    sim._total_dropped = 0
    sim._total_trimmed = 0
    sim._total_ecn = 0
    sim._total_bytes = 0
    return jsonify({'success': True})


@traffic_bp.route('/api/traffic/restart_plan', methods=['POST'])
def api_restart_plan():
    """Restart the plan from step 0, preserving EV state and counters."""
    sim = _traffic_state.get('simulator')
    if sim is None:
        return jsonify({'error': 'No simulator initialized'}), 400
    sim.restart_plan()
    return jsonify({'state': sim.get_state()})


@traffic_bp.route('/api/traffic/event_log')
def api_event_log():
    """Return the spec-aligned MRC event log from the simulator."""
    sim = _traffic_state.get('simulator')
    if sim is None:
        return jsonify({'events': []})
    return jsonify({'events': sim.get_event_log()})


# ---------------------------------------------------------------------------
# Uni/bi-directional flow generation
# ---------------------------------------------------------------------------

@traffic_bp.route('/api/traffic/generate_pair', methods=['POST'])
def api_generate_pair():
    """Generate a flow plan for a uni- or bi-directional XPU pair.

    Request body::

        {
            "src_host": "host0",
            "dst_host": "host2",
            "bidirectional": false,
            "message_size": 1048576,
            "chunk_size": 4096
        }
    """
    from core.collectives import (
        FlowStep, FlowPlan, CollectiveType, TrafficOrchestrator,
    )
    from core.traffic_simulator import TrafficSimulator
    from core.packet_builder import PacketBuilder
    from core.fault_injection import FaultInjector

    data = request.json or {}
    src = data.get('src_host', '')
    dst = data.get('dst_host', '')
    if not src or not dst:
        return jsonify({'error': 'src_host and dst_host required'}), 400

    bidir = data.get('bidirectional', False)
    msg_size = int(data.get('message_size', 1048576))
    chunk_size = int(data.get('chunk_size', 4096))

    num_chunks = max(1, msg_size // chunk_size)
    steps: list[list[FlowStep]] = []
    for i in range(num_chunks):
        desc_fwd = f'{src}→{dst} chunk {i}'
        step_flows = [FlowStep(
            step=i, src_host=src, dst_host=dst,
            chunk_id=i, data_size=chunk_size,
            description=desc_fwd,
        )]
        if bidir:
            desc_rev = f'{dst}→{src} chunk {i}'
            step_flows.append(FlowStep(
                step=i, src_host=dst, dst_host=src,
                chunk_id=i, data_size=chunk_size,
                description=desc_rev,
            ))
        steps.append(step_flows)

    total_flows = sum(len(s) for s in steps)
    plan = FlowPlan(
        collective_type=CollectiveType.POINT_TO_POINT,
        hosts=[src, dst],
        steps=steps,
        num_steps=len(steps),
        message_size=msg_size,
        chunk_size=chunk_size,
        total_bytes=msg_size * (2 if bidir else 1),
        total_flows=total_flows,
    )
    _traffic_state['plan'] = plan

    orch = TrafficOrchestrator()
    orch.load_plan(plan)
    _traffic_state['orchestrator'] = orch

    ev_profile_mode = data.get('ev_profile_mode', 'auto')
    sim = _create_simulator(ev_profile_mode)
    _traffic_state['simulator'] = sim

    return jsonify(plan.to_dict())
