"""
MRC Packet Parser

Parses raw packet bytes into structured MRC packet representations.
Handles Ethernet/IPv6/UDP/RoCE/MRC header decoding.
"""

from __future__ import annotations

import struct
from typing import Optional

from .mrc_headers import (
    BTH, METH, TSETH, RETH, SETH, NETH, PETH, ERTH, EETH, AETH, ImmDt,
    MrcOpcode, get_header_stack,
)
from .packet_builder import EthernetHeader, IPv6Header, UDPHeader, MRCPacket


def parse_packet(raw: bytes) -> Optional[MRCPacket]:
    """Parse raw bytes into an MRCPacket. Returns None if not an MRC packet."""
    if len(raw) < EthernetHeader.SIZE + IPv6Header.SIZE + UDPHeader.SIZE + BTH.SIZE:
        return None

    offset = 0
    eth = EthernetHeader.from_bytes(raw[offset:])
    offset += EthernetHeader.SIZE

    if eth.ethertype != 0x86DD:
        return None

    ipv6 = IPv6Header.from_bytes(raw[offset:])
    offset += IPv6Header.SIZE

    if ipv6.next_header != 17:
        return None

    udp = UDPHeader.from_bytes(raw[offset:])
    offset += UDPHeader.SIZE

    bth = BTH.from_bytes(raw[offset:])
    offset += BTH.SIZE

    pkt = MRCPacket(ethernet=eth, ipv6=ipv6, udp=udp, bth=bth)

    try:
        opcode = bth.opcode
        has_tseth = bool(bth.ts)

        if opcode in (MrcOpcode.RDMA_WRITE_FIRST, MrcOpcode.RDMA_WRITE_MIDDLE,
                      MrcOpcode.RDMA_WRITE_LAST, MrcOpcode.RDMA_WRITE_LAST_IMM,
                      MrcOpcode.RDMA_WRITE_ONLY, MrcOpcode.RDMA_WRITE_ONLY_IMM):
            pkt.meth = METH.from_bytes(raw[offset:])
            offset += METH.SIZE
            if has_tseth:
                pkt.tseth = TSETH.from_bytes(raw[offset:])
                offset += TSETH.SIZE
            pkt.reth = RETH.from_bytes(raw[offset:])
            offset += RETH.SIZE
            if opcode in (MrcOpcode.RDMA_WRITE_LAST_IMM, MrcOpcode.RDMA_WRITE_ONLY_IMM):
                pkt.immdt = ImmDt.from_bytes(raw[offset:])
                offset += ImmDt.SIZE
            icrc_offset = len(raw) - 4
            if offset < icrc_offset:
                pkt.payload = raw[offset:icrc_offset]

        elif opcode == MrcOpcode.ACKNOWLEDGE:
            pkt.aeth = AETH.from_bytes(raw[offset:])

        elif opcode == MrcOpcode.RELIABILITY_SACK:
            if len(raw) >= offset + SETH.SIZE:
                pkt.seth = SETH.from_bytes(raw[offset:])

        elif opcode == MrcOpcode.RELIABILITY_NACK:
            if len(raw) >= offset + NETH.SIZE:
                pkt.neth = NETH.from_bytes(raw[offset:])

        elif opcode == MrcOpcode.RELIABILITY_PROBE_REQ:
            if len(raw) >= offset + PETH.SIZE:
                pkt.peth = PETH.from_bytes(raw[offset:])

        elif opcode == MrcOpcode.ENDPOINT_REQUEST:
            if len(raw) >= offset + ERTH.SIZE:
                pkt.erth = ERTH.from_bytes(raw[offset:])

        elif opcode == MrcOpcode.ENDPOINT_RESPONSE:
            if len(raw) >= offset + EETH.SIZE:
                pkt.eeth = EETH.from_bytes(raw[offset:])

    except (struct.error, IndexError):
        pass

    return pkt


def parse_hex_string(hex_str: str) -> Optional[MRCPacket]:
    """Parse a hex string (with or without spaces/colons) into an MRCPacket."""
    cleaned = hex_str.replace(' ', '').replace(':', '').replace('\n', '')
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        return None
    return parse_packet(raw)


def format_packet_summary(pkt: MRCPacket) -> str:
    """One-line summary of a parsed MRC packet."""
    try:
        opname = MrcOpcode(pkt.bth.opcode).name
    except ValueError:
        opname = f'0x{pkt.bth.opcode:02X}'

    parts = [f'{opname}']
    parts.append(f'QPN={pkt.bth.dqp}')
    parts.append(f'PSN={pkt.bth.psn}')

    if pkt.bth.rtx:
        parts.append('RTX')

    if pkt.seth:
        parts.append(f'CACK={pkt.seth.cack_psn}')
        if pkt.seth.pr:
            parts.append('PROBE_RESP')
    if pkt.neth:
        parts.append(f'NACK_PSN={pkt.neth.nack_psn}')
        parts.append(f'reason=0x{pkt.neth.nack_reason:02X}')
    if pkt.reth:
        parts.append(f'VA=0x{pkt.reth.va:X}')
        parts.append(f'len={pkt.reth.dmalen}')

    return ' | '.join(parts)
