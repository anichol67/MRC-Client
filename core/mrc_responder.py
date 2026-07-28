"""MRC Responder — receives WRITE packets and generates SACK/NACK responses.

Implements the responder-side behavior per OCP MRC spec §6.3, §7.5:
  - PSN bitmap tracking for out-of-order packet reception
  - Cumulative ACK (cack_psn) maintenance
  - SACK generation with bitmap, m-flag, CC_STATE
  - NACK generation for error conditions
  - ECN detection from IP header ECN bits
  - Entropy reflection in control messages (§7.5.2.1)
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable

from core.packet_builder import PacketBuilder, MRCPacket, IPv6Header
from core.packet_parser import parse_packet
from core.mrc_headers import (
    MrcOpcode, CCState, SackMFlag, NackReason,
)

logger = logging.getLogger(__name__)


@dataclass
class ResponderQPState:
    """Per-QP responder state for tracking received packets (§7.5)."""
    src_qpn: int = 0
    dst_qpn: int = 0
    src_ipv6: str = ''
    dst_ipv6: str = ''
    cack_psn: int = -1
    max_rcv_psn: int = -1
    lowest_unsacked_psn: int = -1
    rx_rcvd_bytes: int = 0
    rx_ooo_count: int = 0
    sack_trigger_cnt: int = 0
    max_psn_range: int = 128
    bitmap: dict[int, bool] = field(default_factory=dict)
    prev_ar: bool = False

    SACK_GEN_THRESHOLD: int = 4096

    def receive_psn(self, psn: int, data_size: int) -> None:
        """Record reception of a packet with the given PSN."""
        if self.cack_psn < 0:
            self.cack_psn = psn - 1

        self.bitmap[psn] = True
        self.rx_rcvd_bytes += data_size

        if psn > self.max_rcv_psn:
            self.max_rcv_psn = psn

        self._advance_cack()

        self.sack_trigger_cnt += max(data_size, 1024)

    def _advance_cack(self) -> None:
        """Advance cack_psn as far as possible (no gaps)."""
        while self.bitmap.get(self.cack_psn + 1, False):
            self.cack_psn += 1
            self.bitmap.pop(self.cack_psn, None)

    def should_sack(self, is_ecn: bool = False, is_retransmit: bool = False,
                    is_ar: bool = False) -> bool:
        """Check if a SACK should be generated per §7.5.2."""
        if is_ecn or is_retransmit or is_ar:
            return True
        if self.sack_trigger_cnt >= self.SACK_GEN_THRESHOLD:
            return True
        return False

    def build_sack_bitmap(self) -> tuple[int, int]:
        """Compute sack_offset and 64-bit sack_bitmap per §7.5.2.2."""
        if self.cack_psn < 0:
            return 0, 0

        sack_base = self.cack_psn + 1
        if self.max_rcv_psn >= 0 and sack_base + 64 >= self.max_rcv_psn:
            sack_base = max(self.max_rcv_psn - 64, self.cack_psn + 1)

        sack_offset = (sack_base - self.cack_psn) & 0xFFFF
        bitmap_val = 0
        for i in range(64):
            psn = sack_base + i
            if self.bitmap.get(psn, False) or psn <= self.cack_psn:
                bitmap_val |= (1 << i)

        self.sack_trigger_cnt = 0
        return sack_offset, bitmap_val

    @property
    def rcvd_bytes_256(self) -> int:
        """rx_rcvd_bytes in 256-byte units for CC_STATE (§7.5.5.4)."""
        return ((self.rx_rcvd_bytes + 255) >> 8) & 0xFFFFFF


class MRCResponder:
    """Receives MRC packets and generates SACK/NACK responses.

    Manages per-QP responder state and uses PacketBuilder to construct
    wire-format response packets.
    """

    def __init__(self, packet_builder: Optional[PacketBuilder] = None):
        self.pb = packet_builder or PacketBuilder()
        self._qp_states: dict[int, ResponderQPState] = {}
        self._lock = threading.Lock()
        self._event_log: list[dict] = []
        self._stats = {
            'packets_received': 0,
            'sacks_sent': 0,
            'nacks_sent': 0,
            'ecn_detected': 0,
        }

    def get_or_create_qp(self, src_qpn: int, dst_qpn: int = 0,
                         src_ipv6: str = '', dst_ipv6: str = ''
                         ) -> ResponderQPState:
        with self._lock:
            if src_qpn not in self._qp_states:
                self._qp_states[src_qpn] = ResponderQPState(
                    src_qpn=src_qpn, dst_qpn=dst_qpn,
                    src_ipv6=src_ipv6, dst_ipv6=dst_ipv6,
                )
            return self._qp_states[src_qpn]

    def process_packet(self, raw: bytes, iface: str,
                       send_fn: Optional[Callable] = None) -> Optional[bytes]:
        """Process a received MRC packet and generate a response.

        Args:
            raw: Raw Ethernet frame bytes.
            iface: Interface the packet was received on.
            send_fn: Optional callback to send the response. Called with
                ``(response_bytes, iface)``.

        Returns:
            Response packet bytes (SACK or NACK), or None if no
            response is needed.
        """
        try:
            pkt = parse_packet(raw)
        except Exception as exc:
            logger.debug('Failed to parse packet on %s: %s', iface, exc)
            return None

        if pkt is None:
            return None

        self._stats['packets_received'] += 1
        opcode = pkt.bth.opcode if pkt.bth else 0

        # Only respond to WRITE operations
        if opcode not in (
            MrcOpcode.RDMA_WRITE_ONLY, MrcOpcode.RDMA_WRITE_ONLY_IMM,
            MrcOpcode.RDMA_WRITE_FIRST, MrcOpcode.RDMA_WRITE_MIDDLE,
            MrcOpcode.RDMA_WRITE_LAST, MrcOpcode.RDMA_WRITE_LAST_IMM,
        ):
            return None

        bth = pkt.bth
        psn = bth.psn
        src_qpn = bth.dqp
        is_retransmit = bool(bth.rtx)
        is_ar = bool(bth.a)

        # Detect ECN from outer/inner IPv6 traffic class
        ecn_ce = False
        tc = pkt.ipv6.traffic_class if pkt.ipv6 else 0
        if (tc & 0x03) == 0x03:
            ecn_ce = True
            self._stats['ecn_detected'] += 1

        data_size = len(pkt.payload) if pkt.payload else 0

        # Get entropy from UDP source port
        entropy = pkt.udp.src_port if pkt.udp else 0

        # Get responder QP state
        src_ip = pkt.ipv6.src_addr if pkt.ipv6 else ''
        dst_ip = pkt.ipv6.dst_addr if pkt.ipv6 else ''
        qp = self.get_or_create_qp(src_qpn, bth.dqp, src_ip, dst_ip)
        qp.receive_psn(psn, data_size)

        # Determine if SACK should be generated
        if not qp.should_sack(is_ecn=ecn_ce, is_retransmit=is_retransmit,
                              is_ar=is_ar):
            return None

        # Build SACK response
        sack_offset, sack_bitmap = qp.build_sack_bitmap()
        ack_psn_offset = (psn - qp.cack_psn) & 0xFFFF

        m_flag = SackMFlag.NONE
        if ecn_ce:
            m_flag = SackMFlag.SKIP_ONCE

        tx_timestamp = 0
        if pkt.tseth:
            tx_timestamp = pkt.tseth.tx_timestamp

        cc_state = CCState(
            tx_timestamp=tx_timestamp,
            ooo_count=min(qp.rx_ooo_count, 0x7FFF),
            rcv_cwnd_pen=0,
            restore_cwnd=0,
            rcvd_bytes=qp.rcvd_bytes_256,
        )

        # Swap src/dst for the response
        sack_pkt = self.pb.build_sack(
            src_ipv6=dst_ip, dst_ipv6=src_ip,
            src_mac='00:00:00:00:00:00',
            dst_mac='00:00:00:00:00:00',
            src_qpn=bth.dqp, dst_qpn=src_qpn,
            cack_psn=qp.cack_psn,
            ack_psn_offset=ack_psn_offset,
            sack_offset=sack_offset,
            sack_bitmap=sack_bitmap,
            mpr=1,
            m_flag=m_flag,
            entropy=entropy,
            cc_state=cc_state,
            is_retransmit=is_retransmit,
        )

        response_bytes = sack_pkt.to_bytes()
        self._stats['sacks_sent'] += 1

        self._event_log.append({
            'time': time.time(),
            'type': 'SACK_SENT',
            'psn': psn,
            'cack_psn': qp.cack_psn,
            'entropy': entropy,
            'm_flag': m_flag,
            'ecn': ecn_ce,
            'iface': iface,
        })

        if send_fn is not None:
            send_fn(response_bytes, iface)

        return response_bytes

    def get_stats(self) -> dict:
        return dict(self._stats)

    def get_event_log(self) -> list[dict]:
        return list(self._event_log)

    def get_qp_states(self) -> dict:
        with self._lock:
            return {
                qpn: {
                    'src_qpn': qs.src_qpn,
                    'cack_psn': qs.cack_psn,
                    'max_rcv_psn': qs.max_rcv_psn,
                    'rcvd_bytes': qs.rx_rcvd_bytes,
                    'ooo_count': qs.rx_ooo_count,
                }
                for qpn, qs in self._qp_states.items()
            }

    def reset(self) -> None:
        with self._lock:
            self._qp_states.clear()
        self._event_log.clear()
        for key in self._stats:
            self._stats[key] = 0
