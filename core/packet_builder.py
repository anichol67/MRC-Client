"""
MRC Packet Builder

Assembles complete MRC packets from headers through to iCRC.
Supports all MRC opcodes and EV encoding modes (ECMP, Structured EV, SRv6).

Packet stack:
  Ethernet | IPv6 [| SRv6 outer + optional SRH] | UDP | BTH | [MRC headers] | [Payload] | iCRC
"""

from __future__ import annotations

import struct
import socket
from dataclasses import dataclass, field
from typing import Optional

from .mrc_headers import (
    BTH, METH, TSETH, RETH, SETH, NETH, PETH, ERTH, EETH, AETH, ImmDt,
    CCState, MrcOpcode, get_header_stack,
)


ROCE_UDP_PORT = 4971
NOMINAL_HDRSIZE = 40


@dataclass
class IPv6Header:
    version: int = 6
    traffic_class: int = 0
    flow_label: int = 0
    payload_length: int = 0
    next_header: int = 17  # UDP
    hop_limit: int = 64
    src_addr: str = '::1'
    dst_addr: str = '::1'

    SIZE = 40

    def to_bytes(self) -> bytes:
        vtcfl = (self.version << 28) | ((self.traffic_class & 0xFF) << 20) | (self.flow_label & 0xFFFFF)
        src = socket.inet_pton(socket.AF_INET6, self.src_addr)
        dst = socket.inet_pton(socket.AF_INET6, self.dst_addr)
        return struct.pack('!IHBB', vtcfl, self.payload_length, self.next_header, self.hop_limit) + src + dst

    @classmethod
    def from_bytes(cls, data: bytes) -> IPv6Header:
        vtcfl, payload_length, next_header, hop_limit = struct.unpack_from('!IHBB', data, 0)
        src = socket.inet_ntop(socket.AF_INET6, data[8:24])
        dst = socket.inet_ntop(socket.AF_INET6, data[24:40])
        return cls(
            version=(vtcfl >> 28) & 0xF,
            traffic_class=(vtcfl >> 20) & 0xFF,
            flow_label=vtcfl & 0xFFFFF,
            payload_length=payload_length,
            next_header=next_header,
            hop_limit=hop_limit,
            src_addr=src, dst_addr=dst,
        )


@dataclass
class UDPHeader:
    src_port: int = 0
    dst_port: int = ROCE_UDP_PORT
    length: int = 0
    checksum: int = 0

    SIZE = 8

    def to_bytes(self) -> bytes:
        return struct.pack('!HHHH', self.src_port, self.dst_port, self.length, self.checksum)

    @classmethod
    def from_bytes(cls, data: bytes) -> UDPHeader:
        sp, dp, ln, cs = struct.unpack_from('!HHHH', data, 0)
        return cls(src_port=sp, dst_port=dp, length=ln, checksum=cs)


@dataclass
class EthernetHeader:
    dst_mac: str = '00:00:00:00:00:00'
    src_mac: str = '00:00:00:00:00:00'
    ethertype: int = 0x86DD  # IPv6

    SIZE = 14

    def to_bytes(self) -> bytes:
        dst = bytes.fromhex(self.dst_mac.replace(':', ''))
        src = bytes.fromhex(self.src_mac.replace(':', ''))
        return dst + src + struct.pack('!H', self.ethertype)

    @classmethod
    def from_bytes(cls, data: bytes) -> EthernetHeader:
        dst = ':'.join(f'{b:02x}' for b in data[0:6])
        src = ':'.join(f'{b:02x}' for b in data[6:12])
        ethertype = struct.unpack_from('!H', data, 12)[0]
        return cls(dst_mac=dst, src_mac=src, ethertype=ethertype)


def compute_icrc(data: bytes) -> int:
    """Compute iCRC per IBTA specification (CRC-32C over invariant fields)."""
    # Mask variant fields to 0xFF per IBTA spec before CRC
    buf = bytearray(data)

    # For a full implementation, specific BTH/IP/UDP fields would be masked.
    # This is a simplified CRC-32 for the emulator.
    crc = 0xFFFFFFFF
    poly = 0x1EDC6F41  # CRC-32C polynomial
    for byte in buf:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ poly
            else:
                crc >>= 1
    return crc ^ 0xFFFFFFFF


@dataclass
class DSCPConfig:
    no_trim: int = 0
    trimmable: int = 4
    trimmable_retx: int = 8
    trimmed: int = 12
    trimmed_lasthop: int = 16
    control: int = 46

    def get_dscp_for_opcode(self, opcode: int, is_retransmit: bool = False) -> int:
        if opcode in (MrcOpcode.RELIABILITY_SACK, MrcOpcode.RELIABILITY_NACK,
                      MrcOpcode.ACKNOWLEDGE):
            return self.control
        if opcode == MrcOpcode.RELIABILITY_PROBE_REQ:
            return self.trimmable
        if is_retransmit:
            return self.trimmable_retx
        return self.trimmable


@dataclass
class MRCPacket:
    """A fully assembled MRC packet ready for transmission."""
    ethernet: EthernetHeader = field(default_factory=EthernetHeader)
    ipv6: IPv6Header = field(default_factory=IPv6Header)
    udp: UDPHeader = field(default_factory=UDPHeader)
    bth: BTH = field(default_factory=BTH)
    meth: Optional[METH] = None
    tseth: Optional[TSETH] = None
    reth: Optional[RETH] = None
    immdt: Optional[ImmDt] = None
    seth: Optional[SETH] = None
    neth: Optional[NETH] = None
    peth: Optional[PETH] = None
    erth: Optional[ERTH] = None
    eeth: Optional[EETH] = None
    aeth: Optional[AETH] = None
    payload: bytes = b''

    def get_mrc_payload_bytes(self) -> bytes:
        """Serialize all headers after BTH plus payload."""
        parts = []
        for hdr in [self.meth, self.tseth, self.reth, self.immdt,
                     self.seth, self.neth, self.peth, self.erth, self.eeth, self.aeth]:
            if hdr is not None:
                parts.append(hdr.to_bytes())
        parts.append(self.payload)
        return b''.join(parts)

    def to_bytes(self) -> bytes:
        mrc_payload = self.get_mrc_payload_bytes()
        bth_bytes = self.bth.to_bytes()

        udp_payload = bth_bytes + mrc_payload
        # iCRC is 4 bytes appended after all headers+payload
        icrc = compute_icrc(udp_payload)
        udp_payload_with_icrc = udp_payload + struct.pack('!I', icrc)

        self.udp.length = UDPHeader.SIZE + len(udp_payload_with_icrc)
        self.udp.checksum = 0

        udp_bytes = self.udp.to_bytes() + udp_payload_with_icrc

        self.ipv6.payload_length = len(udp_bytes)
        ipv6_bytes = self.ipv6.to_bytes()

        eth_bytes = self.ethernet.to_bytes()

        return eth_bytes + ipv6_bytes + udp_bytes

    def to_hex_dump(self, bytes_per_line: int = 16) -> str:
        raw = self.to_bytes()
        lines = []
        for i in range(0, len(raw), bytes_per_line):
            chunk = raw[i:i + bytes_per_line]
            hex_part = ' '.join(f'{b:02x}' for b in chunk)
            ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            lines.append(f'{i:04x}  {hex_part:<{bytes_per_line * 3}}  {ascii_part}')
        return '\n'.join(lines)

    def to_dict(self) -> dict:
        d = {
            'ethernet': {
                'dst_mac': self.ethernet.dst_mac, 'src_mac': self.ethernet.src_mac,
                'ethertype': f'0x{self.ethernet.ethertype:04X}',
            },
            'ipv6': {
                'src': self.ipv6.src_addr, 'dst': self.ipv6.dst_addr,
                'tc': self.ipv6.traffic_class, 'flow_label': f'0x{self.ipv6.flow_label:05X}',
                'payload_length': self.ipv6.payload_length,
                'hop_limit': self.ipv6.hop_limit,
            },
            'udp': {
                'src_port': self.udp.src_port, 'dst_port': self.udp.dst_port,
                'length': self.udp.length,
            },
            'bth': self.bth.to_dict(),
        }
        for name, hdr in [('meth', self.meth), ('tseth', self.tseth), ('reth', self.reth),
                           ('immdt', self.immdt), ('seth', self.seth), ('neth', self.neth),
                           ('peth', self.peth), ('erth', self.erth), ('eeth', self.eeth),
                           ('aeth', self.aeth)]:
            if hdr is not None:
                d[name] = hdr.to_dict()
        if self.payload:
            d['payload_length'] = len(self.payload)
            d['payload_hex'] = self.payload[:64].hex()
        d['total_size'] = len(self.to_bytes())
        return d


class PacketBuilder:
    """Build MRC packets from configuration parameters."""

    def __init__(self, dscp_config: Optional[DSCPConfig] = None):
        self.dscp = dscp_config or DSCPConfig()
        self.roce_port = ROCE_UDP_PORT

    def apply_ev_to_packet(self, pkt: MRCPacket, ev_value: int, ev_format: str = 'ECMP'):
        """Encode an entropy value into the packet headers."""
        if ev_format == 'ECMP':
            pkt.udp.src_port = ev_value & 0xFFFF
            pkt.ipv6.flow_label = ev_value & 0xFFFFF
        elif ev_format == 'STRUCTURED_EV':
            pkt.udp.src_port = (ev_value >> 16) & 0xFFFF
            pkt.ipv6.flow_label = ev_value & 0xFFFF
        elif ev_format in ('SRV6_USID', 'SRV6_USID_SRH'):
            pass  # SRv6 addressing handled in build_srv6_packet
        else:
            pkt.udp.src_port = ev_value & 0xFFFF

    def build_write(self, src_ipv6: str, dst_ipv6: str, src_mac: str, dst_mac: str,
                    src_qpn: int, dst_qpn: int, psn: int, va: int, r_key: int,
                    dmalen: int, payload: bytes, msn: int = 0, rqmsn: int = 0,
                    ev_value: int = 0, ev_format: str = 'ECMP',
                    is_only: bool = True, has_imm: bool = False, imm_data: int = 0,
                    include_timestamp: bool = False, tx_timestamp: int = 0,
                    is_retransmit: bool = False) -> MRCPacket:
        if is_only and has_imm:
            opcode = MrcOpcode.RDMA_WRITE_ONLY_IMM
        elif is_only:
            opcode = MrcOpcode.RDMA_WRITE_ONLY
        elif has_imm:
            opcode = MrcOpcode.RDMA_WRITE_LAST_IMM
        else:
            opcode = MrcOpcode.RDMA_WRITE_ONLY

        dscp = self.dscp.get_dscp_for_opcode(opcode, is_retransmit)
        pkt = MRCPacket(
            ethernet=EthernetHeader(dst_mac=dst_mac, src_mac=src_mac),
            ipv6=IPv6Header(src_addr=src_ipv6, dst_addr=dst_ipv6,
                            traffic_class=dscp << 2),
            udp=UDPHeader(dst_port=self.roce_port),
            bth=BTH(opcode=opcode, dqp=dst_qpn, psn=psn,
                    rtx=1 if is_retransmit else 0,
                    ts=1 if include_timestamp else 0),
            meth=METH(rqmsn=rqmsn, msn=msn),
            reth=RETH(va=va, r_key=r_key, dmalen=dmalen),
            payload=payload,
        )
        if include_timestamp:
            pkt.tseth = TSETH(tx_timestamp=tx_timestamp, ftype=0x1)
        if has_imm:
            pkt.immdt = ImmDt(value=imm_data)

        self.apply_ev_to_packet(pkt, ev_value, ev_format)
        return pkt

    def build_sack(self, src_ipv6: str, dst_ipv6: str, src_mac: str, dst_mac: str,
                   src_qpn: int, dst_qpn: int, cack_psn: int,
                   ack_psn_offset: int, sack_offset: int, sack_bitmap: int,
                   mpr: int = 0, m_flag: int = 0, pr: int = 0,
                   entropy: int = 0, ev_format: str = 'ECMP',
                   cc_state: Optional[CCState] = None,
                   is_retransmit: bool = False) -> MRCPacket:
        dscp = self.dscp.control
        pkt = MRCPacket(
            ethernet=EthernetHeader(dst_mac=dst_mac, src_mac=src_mac),
            ipv6=IPv6Header(src_addr=src_ipv6, dst_addr=dst_ipv6,
                            traffic_class=dscp << 2),
            udp=UDPHeader(dst_port=self.roce_port),
            bth=BTH(opcode=MrcOpcode.RELIABILITY_SACK, dqp=dst_qpn,
                    psn=cack_psn, rtx=1 if is_retransmit else 0),
            seth=SETH(
                m=m_flag, pr=pr,
                ack_psn_offset=ack_psn_offset,
                entropy=entropy,
                spdcid=src_qpn & 0xFFFF,
                dpdcid=dst_qpn & 0xFFFF,
                cack_psn=cack_psn,
                mpr=mpr,
                sack_offset=sack_offset,
                sack_bitmap=sack_bitmap,
                cc_state=cc_state or CCState(),
            ),
        )
        self.apply_ev_to_packet(pkt, entropy, ev_format)
        return pkt

    def build_nack(self, src_ipv6: str, dst_ipv6: str, src_mac: str, dst_mac: str,
                   src_qpn: int, dst_qpn: int, nack_psn: int,
                   nack_reason: int, entropy: int = 0, ev_format: str = 'ECMP',
                   tx_timestamp: int = 0) -> MRCPacket:
        dscp = self.dscp.control
        pkt = MRCPacket(
            ethernet=EthernetHeader(dst_mac=dst_mac, src_mac=src_mac),
            ipv6=IPv6Header(src_addr=src_ipv6, dst_addr=dst_ipv6,
                            traffic_class=dscp << 2),
            udp=UDPHeader(dst_port=self.roce_port),
            bth=BTH(opcode=MrcOpcode.RELIABILITY_NACK, dqp=dst_qpn, psn=nack_psn),
            neth=NETH(
                nack_reason=nack_reason,
                entropy=entropy,
                spdcid=src_qpn & 0xFFFF,
                dpdcid=dst_qpn & 0xFFFF,
                nack_psn=nack_psn,
                tx_timestamp=tx_timestamp,
            ),
        )
        self.apply_ev_to_packet(pkt, entropy, ev_format)
        return pkt

    def build_reliability_probe(self, src_ipv6: str, dst_ipv6: str,
                                 src_mac: str, dst_mac: str,
                                 src_qpn: int, dst_qpn: int,
                                 probe_id: int, ev_value: int = 0,
                                 ev_format: str = 'ECMP',
                                 tx_timestamp: int = 0) -> MRCPacket:
        pkt = MRCPacket(
            ethernet=EthernetHeader(dst_mac=dst_mac, src_mac=src_mac),
            ipv6=IPv6Header(src_addr=src_ipv6, dst_addr=dst_ipv6,
                            traffic_class=self.dscp.trimmable << 2),
            udp=UDPHeader(dst_port=self.roce_port),
            bth=BTH(opcode=MrcOpcode.RELIABILITY_PROBE_REQ, dqp=dst_qpn),
            peth=PETH(
                probe_id=probe_id,
                spdcid=src_qpn & 0xFFFF,
                dpdcid=dst_qpn & 0xFFFF,
                tx_timestamp=tx_timestamp,
            ),
        )
        self.apply_ev_to_packet(pkt, ev_value, ev_format)
        return pkt

    def build_ev_probe(self, src_ipv6: str, dst_ipv6: str,
                       src_mac: str, dst_mac: str,
                       probe_id: int, ev_value: int = 0,
                       ev_format: str = 'ECMP',
                       tx_timestamp: int = 0) -> MRCPacket:
        pkt = MRCPacket(
            ethernet=EthernetHeader(dst_mac=dst_mac, src_mac=src_mac),
            ipv6=IPv6Header(src_addr=src_ipv6, dst_addr=dst_ipv6),
            udp=UDPHeader(dst_port=self.roce_port),
            bth=BTH(opcode=MrcOpcode.ENDPOINT_REQUEST, dqp=0x2,
                    psn=probe_id & 0xFFFF),
            erth=ERTH(op=0x01, tx_timestamp=tx_timestamp),
        )
        self.apply_ev_to_packet(pkt, ev_value, ev_format)
        return pkt

    def build_port_status_update(self, src_ipv6: str, dst_ipv6: str,
                                  src_mac: str, dst_mac: str,
                                  probe_id: int, port_mask: int,
                                  tx_timestamp: int = 0) -> MRCPacket:
        dscp = self.dscp.control
        pkt = MRCPacket(
            ethernet=EthernetHeader(dst_mac=dst_mac, src_mac=src_mac),
            ipv6=IPv6Header(src_addr=src_ipv6, dst_addr=dst_ipv6,
                            traffic_class=dscp << 2),
            udp=UDPHeader(dst_port=self.roce_port),
            bth=BTH(opcode=MrcOpcode.ENDPOINT_REQUEST, dqp=0x2,
                    psn=probe_id & 0xFFFF),
            erth=ERTH(op=0x00, port_status_mask=port_mask,
                      tx_timestamp=tx_timestamp),
        )
        return pkt

    def build_endpoint_response(self, src_ipv6: str, dst_ipv6: str,
                                 src_mac: str, dst_mac: str,
                                 probe_id: int, op: int = 0x01,
                                 tx_timestamp: int = 0,
                                 ev_value: int = 0,
                                 ev_format: str = 'ECMP') -> MRCPacket:
        dscp = self.dscp.control
        pkt = MRCPacket(
            ethernet=EthernetHeader(dst_mac=dst_mac, src_mac=src_mac),
            ipv6=IPv6Header(src_addr=src_ipv6, dst_addr=dst_ipv6,
                            traffic_class=dscp << 2),
            udp=UDPHeader(dst_port=self.roce_port),
            bth=BTH(opcode=MrcOpcode.ENDPOINT_RESPONSE, dqp=0x2,
                    psn=probe_id & 0xFFFF),
            eeth=EETH(op=op, tx_timestamp=tx_timestamp),
        )
        self.apply_ev_to_packet(pkt, ev_value, ev_format)
        return pkt
