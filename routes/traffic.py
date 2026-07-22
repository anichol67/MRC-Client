from flask import Blueprint, render_template, request, jsonify, current_app

traffic_bp = Blueprint('traffic', __name__)

_traffic_state = {
    'orchestrator': None,
    'plan': None,
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
