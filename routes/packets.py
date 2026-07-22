from flask import Blueprint, render_template, request, jsonify, current_app

from core.mrc_headers import MrcOpcode
from core.packet_builder import PacketBuilder, MRCPacket
from core.packet_parser import parse_hex_string, format_packet_summary

packets_bp = Blueprint('packets', __name__)

_packet_history: list[dict] = []


@packets_bp.route('/packets')
def packet_builder_page():
    opcodes = [{'value': op.value, 'name': op.name, 'hex': f'0x{op.value:02X}'} for op in MrcOpcode]
    qps = current_app.qp_manager.list_qps()
    ev_profiles = current_app.ev_manager.list_profiles()
    return render_template('packet_builder.html', opcodes=opcodes, qps=qps,
                           ev_profiles=ev_profiles, history=_packet_history[-50:])


@packets_bp.route('/api/packets/build', methods=['POST'])
def api_build_packet():
    data = request.json
    builder = PacketBuilder()

    opcode = int(data.get('opcode', MrcOpcode.RDMA_WRITE_ONLY))
    src_ipv6 = data.get('src_ipv6', '::1')
    dst_ipv6 = data.get('dst_ipv6', '::1')
    src_mac = data.get('src_mac', '00:00:00:00:00:00')
    dst_mac = data.get('dst_mac', '00:00:00:00:00:00')
    src_qpn = int(data.get('src_qpn', 3))
    dst_qpn = int(data.get('dst_qpn', 3))
    ev_value = int(data.get('ev_value', 0))
    ev_format = data.get('ev_format', 'ECMP')

    if opcode in (MrcOpcode.RDMA_WRITE_ONLY, MrcOpcode.RDMA_WRITE_ONLY_IMM,
                  MrcOpcode.RDMA_WRITE_FIRST, MrcOpcode.RDMA_WRITE_MIDDLE,
                  MrcOpcode.RDMA_WRITE_LAST, MrcOpcode.RDMA_WRITE_LAST_IMM):
        payload_hex = data.get('payload', '')
        payload = bytes.fromhex(payload_hex) if payload_hex else b'\x00' * 64
        has_imm = opcode in (MrcOpcode.RDMA_WRITE_ONLY_IMM, MrcOpcode.RDMA_WRITE_LAST_IMM)
        pkt = builder.build_write(
            src_ipv6=src_ipv6, dst_ipv6=dst_ipv6, src_mac=src_mac, dst_mac=dst_mac,
            src_qpn=src_qpn, dst_qpn=dst_qpn, psn=int(data.get('psn', 0)),
            va=int(data.get('va', 0)), r_key=int(data.get('r_key', 0)),
            dmalen=int(data.get('dmalen', len(payload))), payload=payload,
            msn=int(data.get('msn', 0)), rqmsn=int(data.get('rqmsn', 0)),
            ev_value=ev_value, ev_format=ev_format,
            is_only=opcode in (MrcOpcode.RDMA_WRITE_ONLY, MrcOpcode.RDMA_WRITE_ONLY_IMM),
            has_imm=has_imm, imm_data=int(data.get('imm_data', 0)),
            include_timestamp=data.get('include_timestamp', False),
            tx_timestamp=int(data.get('tx_timestamp', 0)),
            is_retransmit=data.get('is_retransmit', False),
        )
    elif opcode == MrcOpcode.RELIABILITY_SACK:
        pkt = builder.build_sack(
            src_ipv6=src_ipv6, dst_ipv6=dst_ipv6, src_mac=src_mac, dst_mac=dst_mac,
            src_qpn=src_qpn, dst_qpn=dst_qpn,
            cack_psn=int(data.get('cack_psn', 0)),
            ack_psn_offset=int(data.get('ack_psn_offset', 0)),
            sack_offset=int(data.get('sack_offset', 0)),
            sack_bitmap=int(data.get('sack_bitmap', 0)),
            mpr=int(data.get('mpr', 1)), m_flag=int(data.get('m_flag', 0)),
            entropy=ev_value, ev_format=ev_format,
        )
    elif opcode == MrcOpcode.RELIABILITY_NACK:
        pkt = builder.build_nack(
            src_ipv6=src_ipv6, dst_ipv6=dst_ipv6, src_mac=src_mac, dst_mac=dst_mac,
            src_qpn=src_qpn, dst_qpn=dst_qpn,
            nack_psn=int(data.get('nack_psn', 0)),
            nack_reason=int(data.get('nack_reason', 0x01)),
            entropy=ev_value, ev_format=ev_format,
        )
    elif opcode == MrcOpcode.RELIABILITY_PROBE_REQ:
        pkt = builder.build_reliability_probe(
            src_ipv6=src_ipv6, dst_ipv6=dst_ipv6, src_mac=src_mac, dst_mac=dst_mac,
            src_qpn=src_qpn, dst_qpn=dst_qpn,
            probe_id=int(data.get('probe_id', 1)),
            ev_value=ev_value, ev_format=ev_format,
        )
    elif opcode == MrcOpcode.ENDPOINT_REQUEST:
        ep_op = int(data.get('endpoint_op', 0x01))
        if ep_op == 0x00:
            pkt = builder.build_port_status_update(
                src_ipv6=src_ipv6, dst_ipv6=dst_ipv6, src_mac=src_mac, dst_mac=dst_mac,
                probe_id=int(data.get('probe_id', 1)),
                port_mask=int(data.get('port_status_mask', 0xF)),
            )
        else:
            pkt = builder.build_ev_probe(
                src_ipv6=src_ipv6, dst_ipv6=dst_ipv6, src_mac=src_mac, dst_mac=dst_mac,
                probe_id=int(data.get('probe_id', 1)),
                ev_value=ev_value, ev_format=ev_format,
            )
    else:
        return jsonify({'error': f'Unsupported opcode 0x{opcode:02X}'}), 400

    result = pkt.to_dict()
    result['hex_dump'] = pkt.to_hex_dump()
    result['raw_hex'] = pkt.to_bytes().hex()

    _packet_history.append({
        'opcode': f'0x{opcode:02X}',
        'summary': format_packet_summary(pkt),
        'size': len(pkt.to_bytes()),
    })

    return jsonify(result)


@packets_bp.route('/api/packets/parse', methods=['POST'])
def api_parse_packet():
    data = request.json
    pkt = parse_hex_string(data.get('hex', ''))
    if pkt is None:
        return jsonify({'error': 'Could not parse packet'}), 400
    result = pkt.to_dict()
    result['summary'] = format_packet_summary(pkt)
    return jsonify(result)


@packets_bp.route('/api/packets/history')
def api_packet_history():
    return jsonify(_packet_history[-100:])
