"""
MRC QP Manager

Manages simulated MRC Queue Pair state including connection attributes,
PSN tracking, bitmap state, and QP lifecycle (RESET→INIT→RTR→RTS→ERROR).

Per OCP MRC 1.0 Sections 6.1, 6.3, 7.4, 7.5, 10.1.2.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from .mrc_headers import QPState


@dataclass
class QPConnectionAttrs:
    """Per-QP connection attributes exchanged OOB (Table 10-1)."""
    max_wimm_dest: int = 32
    max_mpr_dest: int = 8
    dynamic_mpr: bool = False
    trim_nack_capable: bool = False
    service_time_capable: bool = False


@dataclass
class RequestorState:
    """Requestor per-connection reliability state (Table 7-4)."""
    unack_psn: int = 0
    max_psn_sent: int = 0

    def advance_psn(self) -> int:
        psn = (self.max_psn_sent + 1) & 0xFFFFFF
        self.max_psn_sent = psn
        return psn


@dataclass
class ResponderState:
    """Responder per-connection reliability state (Table 7-5)."""
    cack_psn: int = 0
    rx_rcvd_bytes: int = 0
    max_rcv_psn: int = 0
    lowest_unsacked_psn: int = 0
    sack_trigger_cnt: int = 0
    prev_ar: bool = False
    max_psn_range: int = 128
    rx_ooo_count: int = 0
    bitmap: int = 0

    def receive_packet(self, psn: int, payload_size: int):
        """Track receipt of a packet at the responder."""
        self.rx_rcvd_bytes += max(payload_size, 1024)
        if self._psn_gt(psn, self.max_rcv_psn):
            self.max_rcv_psn = psn
        bit_offset = (psn - (self.cack_psn + 1)) & 0xFFFFFF
        if bit_offset < 128:
            self.bitmap |= (1 << bit_offset)
        self._advance_cack()
        self.sack_trigger_cnt += max(payload_size, 1024)

    def _advance_cack(self):
        while self.bitmap & 1:
            self.bitmap >>= 1
            self.cack_psn = (self.cack_psn + 1) & 0xFFFFFF

    @staticmethod
    def _psn_gt(a: int, b: int) -> bool:
        diff = (a - b) & 0xFFFFFF
        return 0 < diff < (1 << 23)


@dataclass
class MRCQueuePair:
    """A simulated MRC Queue Pair."""
    qpn: int = 0
    state: QPState = QPState.RESET

    src_ipv6: str = '::1'
    dst_ipv6: str = '::1'
    src_mac: str = '00:00:00:00:00:00'
    dst_mac: str = '00:00:00:00:00:00'

    dest_qpn: int = 0
    sq_psn: int = 0
    rq_psn: int = 0

    max_psn_range: int = 128
    max_wimm_inflight: int = 32

    ev_profile_id: Optional[int] = None
    cc_profile_id: Optional[int] = None

    connection_attrs: QPConnectionAttrs = field(default_factory=QPConnectionAttrs)
    requestor: RequestorState = field(default_factory=RequestorState)
    responder: ResponderState = field(default_factory=ResponderState)

    linear_retry_count: int = 3
    exponential_retry_count: int = 5
    timeout_parameter: int = 14

    def modify_to_init(self):
        if self.state != QPState.RESET:
            raise ValueError(f'Cannot transition to INIT from {self.state.name}')
        self.state = QPState.INIT

    def modify_to_rtr(self, rq_psn: int, dest_qpn: int, dst_ipv6: str, dst_mac: str):
        if self.state != QPState.INIT:
            raise ValueError(f'Cannot transition to RTR from {self.state.name}')
        self.rq_psn = rq_psn
        self.dest_qpn = dest_qpn
        self.dst_ipv6 = dst_ipv6
        self.dst_mac = dst_mac
        self.responder.cack_psn = (rq_psn - 1) & 0xFFFFFF
        self.responder.max_rcv_psn = (rq_psn - 1) & 0xFFFFFF
        self.responder.lowest_unsacked_psn = (rq_psn - 1) & 0xFFFFFF
        self.responder.max_psn_range = self.max_psn_range
        self.state = QPState.RTR

    def modify_to_rts(self, sq_psn: int):
        if self.state != QPState.RTR:
            raise ValueError(f'Cannot transition to RTS from {self.state.name}')
        self.sq_psn = sq_psn
        self.requestor.unack_psn = (sq_psn - 1) & 0xFFFFFF
        self.requestor.max_psn_sent = 0
        self.state = QPState.RTS

    def transition_to_error(self, reason: str = ''):
        self.state = QPState.ERROR

    def can_send(self) -> bool:
        if self.state != QPState.RTS:
            return False
        outstanding = (self.requestor.max_psn_sent - self.requestor.unack_psn) & 0xFFFFFF
        return outstanding < self.max_psn_range

    def next_psn(self) -> int:
        return self.requestor.advance_psn()

    def to_dict(self) -> dict:
        return {
            'qpn': self.qpn, 'state': self.state.name,
            'src_ipv6': self.src_ipv6, 'dst_ipv6': self.dst_ipv6,
            'src_mac': self.src_mac, 'dst_mac': self.dst_mac,
            'dest_qpn': self.dest_qpn,
            'sq_psn': self.sq_psn, 'rq_psn': self.rq_psn,
            'max_psn_range': self.max_psn_range,
            'max_wimm_inflight': self.max_wimm_inflight,
            'ev_profile_id': self.ev_profile_id,
            'cc_profile_id': self.cc_profile_id,
            'requestor': {
                'unack_psn': self.requestor.unack_psn,
                'max_psn_sent': self.requestor.max_psn_sent,
            },
            'responder': {
                'cack_psn': self.responder.cack_psn,
                'max_rcv_psn': self.responder.max_rcv_psn,
                'rx_rcvd_bytes': self.responder.rx_rcvd_bytes,
                'rx_ooo_count': self.responder.rx_ooo_count,
                'max_psn_range': self.responder.max_psn_range,
            },
            'connection_attrs': {
                'max_wimm_dest': self.connection_attrs.max_wimm_dest,
                'max_mpr_dest': self.connection_attrs.max_mpr_dest,
                'dynamic_mpr': self.connection_attrs.dynamic_mpr,
                'trim_nack_capable': self.connection_attrs.trim_nack_capable,
                'service_time_capable': self.connection_attrs.service_time_capable,
            },
        }


class QPManager:
    """Manages all MRC Queue Pairs for the emulator."""

    def __init__(self):
        self._qps: dict[int, MRCQueuePair] = {}
        self._next_qpn = 3  # 0x0, 0x1, 0x2 are reserved
        self._lock = threading.Lock()

    def create_qp(self, src_ipv6: str = '::1', src_mac: str = '00:00:00:00:00:00',
                  max_psn_range: int = 128, max_wimm_inflight: int = 32) -> MRCQueuePair:
        with self._lock:
            qpn = self._next_qpn
            self._next_qpn += 1
        qp = MRCQueuePair(
            qpn=qpn, src_ipv6=src_ipv6, src_mac=src_mac,
            max_psn_range=max_psn_range, max_wimm_inflight=max_wimm_inflight,
        )
        self._qps[qpn] = qp
        return qp

    def get_qp(self, qpn: int) -> Optional[MRCQueuePair]:
        return self._qps.get(qpn)

    def destroy_qp(self, qpn: int):
        self._qps.pop(qpn, None)

    def list_qps(self) -> list[dict]:
        return [qp.to_dict() for qp in self._qps.values()]

    def modify_qp(self, qpn: int, **kwargs) -> MRCQueuePair:
        qp = self._qps.get(qpn)
        if qp is None:
            raise KeyError(f'QP {qpn} not found')
        target_state = kwargs.pop('target_state', None)
        if target_state == 'INIT':
            qp.modify_to_init()
        elif target_state == 'RTR':
            qp.modify_to_rtr(
                rq_psn=kwargs.get('rq_psn', 0),
                dest_qpn=kwargs.get('dest_qpn', 0),
                dst_ipv6=kwargs.get('dst_ipv6', '::1'),
                dst_mac=kwargs.get('dst_mac', '00:00:00:00:00:00'),
            )
        elif target_state == 'RTS':
            qp.modify_to_rts(sq_psn=kwargs.get('sq_psn', 0))
        for k, v in kwargs.items():
            if hasattr(qp, k) and k not in ('qpn', 'state'):
                setattr(qp, k, v)
        return qp
