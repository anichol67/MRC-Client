"""NSCC (Network Signal Congestion Control) for OCP MRC Rev 1.0.

Implements the congestion control algorithm defined in OCP MRC Spec Section 8.
Provides per-QPCC (QP Congestion Controller) state tracking, cwnd management,
and the NSCC event actions described in Table 8-2.

Key concepts:
  - Each QPCC manages a congestion window for one or more QPs to a destination.
  - rcvd_bytes is carried in SACK packets as a 24-bit field in 256-byte units;
    internally we track the full byte count (shifted left by 8).
  - nominal_pktsize = UDP payload length + nominal_hdrsize (40 bytes, spec 8.3.1).
  - cwnd is always clamped to [min_cwnd, max_cwnd].
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ---------------------------------------------------------------------------
# Modular-arithmetic helpers (spec uses 24-bit PSN / rcvd_bytes fields)
# ---------------------------------------------------------------------------

_MOD24 = 1 << 24  # 16_777_216


def _mod24_diff(a: int, b: int) -> int:
    """Return (a - b) mod 2^24 as a signed value in [-2^23, 2^23).

    Used for comparing 24-bit PSN and rcvd_bytes counters that wrap around.
    Per spec Section 8.4, the receiver advertises rcvd_bytes as a 24-bit
    monotonically increasing counter.
    """
    diff = (a - b) % _MOD24
    if diff >= _MOD24 // 2:
        diff -= _MOD24
    return diff


# ---------------------------------------------------------------------------
# NSCCConfig -- per-QPCC tunable parameters (spec Section 8)
# ---------------------------------------------------------------------------

@dataclass
class NSCCConfig:
    """Per-QPCC tunable parameters for the NSCC algorithm.

    These values correspond to the implementation-defined constants referenced
    throughout spec Section 8 and its subsections.

    Attributes:
        target_qdelay: Target queuing delay in microseconds.  The NSCC
            algorithm switches between proportional-increase and
            additive-increase modes based on whether the measured RTT is
            above or below this threshold (spec 8.4, Table 8-1).
        min_cwnd: Minimum congestion window in bytes.
        max_cwnd: Maximum congestion window in bytes.
        initial_cwnd: Initial congestion window in bytes at QPCC creation.
        ai_increment: Additive-increase bytes added to cwnd per RTT when
            the link is near congestion (RTT >= target_qdelay, no ECN).
        md_factor: Multiplicative-decrease factor applied on congestion
            signals (ECN or inferred loss).  E.g. 0.5 halves the cwnd.
        nominal_hdrsize: Fixed header overhead added to the UDP payload
            length when computing nominal_pktsize (spec 8.3.1, 40 bytes).
    """

    target_qdelay: float = 10.0       # microseconds
    min_cwnd: int = 4096               # bytes
    max_cwnd: int = 16 * 1024 * 1024   # bytes (16 MiB)
    initial_cwnd: int = 65536          # bytes (64 KiB)
    ai_increment: int = 4096           # bytes per RTT
    md_factor: float = 0.5
    nominal_hdrsize: int = 40          # spec 8.3.1


# ---------------------------------------------------------------------------
# QPScheduleState -- QP scheduling states (Figure 4)
# ---------------------------------------------------------------------------

class QPScheduleState(Enum):
    """QP scheduling states per spec Figure 4.

    IDLE    -- No data queued, QP not scheduled.
    ACTIVE  -- Data queued and cwnd allows sending.
    READY   -- Data queued, waiting for cwnd to open.
    PENDING -- Waiting for ACK / SACK before more data can be sent.
    """

    IDLE = auto()
    ACTIVE = auto()
    READY = auto()
    PENDING = auto()


# ---------------------------------------------------------------------------
# CongestionWindow -- per-QPCC congestion state (Table 8-3)
# ---------------------------------------------------------------------------

@dataclass
class CongestionWindow:
    """Per-QPCC congestion window and RTT state (Table 8-3).

    Attributes:
        cwnd: Current congestion window in bytes.
        inflight: Bytes sent but not yet acknowledged (32-bit tracked).
        prev_rcvd_bytes: Previous largest rcvd_bytes value received from
            SACKs (24-bit field; stored as the raw 24-bit value before
            the <<8 expansion).
        rtt_estimate: Exponentially-weighted smoothed RTT in microseconds.
        min_rtt: Minimum observed RTT in microseconds across the
            lifetime of this QPCC.
        saved_cwnd: Saved cwnd value for responder flow-control restore
            (spec 7.5.1).  ``None`` when no save is active.
    """

    cwnd: int = 65536
    inflight: int = 0
    prev_rcvd_bytes: int = 0
    rtt_estimate: float = 0.0
    min_rtt: float = float("inf")
    saved_cwnd: Optional[int] = None


# ---------------------------------------------------------------------------
# QPCC -- QP Congestion Controller (one per destination)
# ---------------------------------------------------------------------------

class QPCC:
    """QP Congestion Controller -- one instance per destination.

    Implements the NSCC event actions defined in Table 8-2 of the OCP MRC
    Rev 1.0 specification.  A single QPCC may govern multiple QPs that
    share a path to the same destination.

    Attributes:
        qpcc_id: Unique identifier for this QPCC.
        config: Tunable parameters (:class:`NSCCConfig`).
        window: Congestion-window state (:class:`CongestionWindow`).
        schedule_state: Current QP scheduling state (:class:`QPScheduleState`).
        qp_ids: List of QP identifiers mapped to this QPCC.
        backlog: Outstanding application data in bytes waiting to be sent.
        _in_flow_control: True while responder flow-control penalty is active.
        _loss_epoch: Monotonic timestamp of the last loss event, used to
            coalesce multiple losses within the same RTT.
    """

    # Smoothing factor for EWMA RTT estimate (alpha).
    _RTT_ALPHA: float = 0.125

    def __init__(self, qpcc_id: int, config: NSCCConfig | None = None) -> None:
        self.qpcc_id = qpcc_id
        self.config = config or NSCCConfig()
        self.window = CongestionWindow(
            cwnd=self.config.initial_cwnd,
        )
        self.schedule_state = QPScheduleState.IDLE
        self.qp_ids: list[int] = []
        self.backlog: int = 0
        self._in_flow_control: bool = False
        self._loss_epoch: float = 0.0

    # -- helpers -------------------------------------------------------------

    def _clamp_cwnd(self) -> None:
        """Clamp cwnd to [min_cwnd, max_cwnd] (spec 8.4)."""
        self.window.cwnd = max(
            self.config.min_cwnd,
            min(self.window.cwnd, self.config.max_cwnd),
        )

    def _update_rtt(self, rtt_sample: float) -> None:
        """Update smoothed RTT estimate and min_rtt.

        Uses an EWMA with alpha = 1/8 (spec 8.4).
        """
        if self.window.rtt_estimate <= 0.0:
            # First sample -- initialise directly.
            self.window.rtt_estimate = rtt_sample
        else:
            alpha = self._RTT_ALPHA
            self.window.rtt_estimate = (
                (1.0 - alpha) * self.window.rtt_estimate + alpha * rtt_sample
            )
        if rtt_sample < self.window.min_rtt:
            self.window.min_rtt = rtt_sample

    def _update_schedule_state(self) -> None:
        """Recalculate schedule_state from current window / backlog.

        State machine per Figure 4:
          IDLE    -- no data and nothing in flight
          ACTIVE  -- data to send and cwnd allows it
          READY   -- data to send but cwnd is exhausted
          PENDING -- no backlog data but packets still in flight
        """
        has_data = self.backlog > 0
        can_send = self.window.cwnd > self.window.inflight

        if not has_data and self.window.inflight == 0:
            self.schedule_state = QPScheduleState.IDLE
        elif has_data and can_send:
            self.schedule_state = QPScheduleState.ACTIVE
        elif has_data and not can_send:
            self.schedule_state = QPScheduleState.READY
        else:
            # has no backlog data but inflight > 0
            self.schedule_state = QPScheduleState.PENDING

    # -- NSCC event actions (Table 8-2) -------------------------------------

    def on_ack(
        self,
        sack_rcvd_bytes: int,
        sack_tx_timestamp: float,
        ecn_marked: bool,
        rcv_cwnd_pen: int = 0,
        restore_cwnd: bool = False,
        rtx_flag: bool = False,
        arrival_time: float | None = None,
    ) -> None:
        """Process an incoming SACK (Table 8-2: OnACK event).

        Implements the cwnd adjustment logic from spec Section 8.4 and
        Table 8-1, plus responder flow-control handling (spec 7.5.1).

        Args:
            sack_rcvd_bytes: The ``rcvd_bytes`` field from the SACK packet
                (24-bit value in 256-byte units as carried on the wire).
            sack_tx_timestamp: The echoed TX timestamp from the SACK,
                in seconds (monotonic clock).
            ecn_marked: True if the SACK carries an ECN congestion signal.
            rcv_cwnd_pen: Responder flow-control penalty (0..127).
                0 = no effect; 127 = maximum slow-down to 1 pkt/RTT
                (spec 7.5.1).
            restore_cwnd: If True, restore ``saved_cwnd`` when transitioning
                out of responder flow control (spec 7.5.1).
            rtx_flag: True if this ACK acknowledges a retransmitted packet.
            arrival_time: Monotonic timestamp of SACK arrival (seconds).
                Defaults to ``time.monotonic()`` if not supplied.
        """
        if arrival_time is None:
            arrival_time = time.monotonic()

        # -- RTT measurement ------------------------------------------------
        rtt_sample_us = (arrival_time - sack_tx_timestamp) * 1_000_000.0
        if rtt_sample_us > 0:
            self._update_rtt(rtt_sample_us)

        # -- newly received bytes (spec 8.4) --------------------------------
        # rcvd_bytes is a 24-bit counter in 256-byte units.
        diff24 = _mod24_diff(sack_rcvd_bytes, self.window.prev_rcvd_bytes)
        if diff24 > 0:
            newly_rcvd_bytes = diff24 << 8  # convert to bytes
            self.window.prev_rcvd_bytes = sack_rcvd_bytes
        else:
            newly_rcvd_bytes = 0

        # -- Decrement inflight ---------------------------------------------
        self.window.inflight = max(0, self.window.inflight - newly_rcvd_bytes)

        # -- Responder flow-control (spec 7.5.1) ----------------------------
        if rcv_cwnd_pen > 0:
            if not self._in_flow_control:
                # Entering flow control -- save current cwnd.
                self.window.saved_cwnd = self.window.cwnd
                self._in_flow_control = True

            # Scale cwnd down: penalty 127 -> 1 pkt/RTT (min_cwnd),
            # penalty 1 -> almost no effect.  Linear interpolation between
            # saved_cwnd and min_cwnd.
            pen_fraction = rcv_cwnd_pen / 127.0
            effective = self.window.saved_cwnd or self.window.cwnd
            penalized = int(
                effective * (1.0 - pen_fraction)
                + self.config.min_cwnd * pen_fraction
            )
            self.window.cwnd = max(self.config.min_cwnd, penalized)
            self._clamp_cwnd()
            self._update_schedule_state()
            return  # Skip normal NSCC adjustment while penalized.

        if self._in_flow_control and restore_cwnd:
            # Transitioning out of flow control -- restore saved cwnd.
            if self.window.saved_cwnd is not None:
                self.window.cwnd = self.window.saved_cwnd
                self.window.saved_cwnd = None
            self._in_flow_control = False
            self._clamp_cwnd()
            self._update_schedule_state()
            return
        elif self._in_flow_control and rcv_cwnd_pen == 0:
            # Penalty dropped to zero without explicit restore flag.
            self._in_flow_control = False

        # -- NSCC cwnd adjustment (Table 8-1) --------------------------------
        if newly_rcvd_bytes > 0 and not rtx_flag:
            qdelay = self.window.rtt_estimate - self.window.min_rtt
            if not ecn_marked and qdelay < self.config.target_qdelay:
                # Proportional increase: scale increase by fraction of
                # newly_rcvd_bytes relative to cwnd (like TCP NewReno).
                increment = max(
                    1,
                    int(
                        (newly_rcvd_bytes * newly_rcvd_bytes)
                        / self.window.cwnd
                    ),
                )
                self.window.cwnd += increment
            elif not ecn_marked and qdelay >= self.config.target_qdelay:
                # Fair / additive increase: add ai_increment scaled by
                # the fraction of the window acknowledged.
                increment = max(
                    1,
                    int(
                        self.config.ai_increment
                        * newly_rcvd_bytes
                        / self.window.cwnd
                    ),
                )
                self.window.cwnd += increment
            elif ecn_marked and qdelay >= self.config.target_qdelay:
                # Multiplicative decrease.
                self.window.cwnd = max(
                    self.config.min_cwnd,
                    int(self.window.cwnd * self.config.md_factor),
                )
            # else: ECN marked but qdelay < target -- no change (Table 8-1).

        self._clamp_cwnd()
        self._update_schedule_state()

    def on_nack(
        self,
        nack_reason: int,
        tx_timestamp: float,
        arrival_time: float | None = None,
    ) -> None:
        """Process an incoming NACK (Table 8-2: OnNACK event).

        Calculates RTT from the echoed timestamp, marks the packet for
        retransmission, and treats the event as an inferred loss for
        congestion-control purposes (OnInferredLoss behaviour, spec 8.4).

        Args:
            nack_reason: NACK reason code from the packet.
            tx_timestamp: Echoed TX timestamp (seconds, monotonic).
            arrival_time: Monotonic arrival timestamp (seconds).
                Defaults to ``time.monotonic()``.
        """
        if arrival_time is None:
            arrival_time = time.monotonic()

        rtt_sample_us = (arrival_time - tx_timestamp) * 1_000_000.0
        if rtt_sample_us > 0:
            self._update_rtt(rtt_sample_us)

        # Treat as loss event.
        self.on_inferred_loss()

    def on_inferred_loss(self) -> None:
        """Handle an inferred loss event (Table 8-2: OnInferredLoss).

        Applies multiplicative decrease to cwnd.  Multiple losses within
        the same RTT are coalesced into a single decrease event to avoid
        over-reaction.
        """
        now = time.monotonic()
        # Coalesce losses within one RTT estimate.
        rtt_sec = self.window.rtt_estimate / 1_000_000.0 if self.window.rtt_estimate > 0 else 0.0
        if now - self._loss_epoch < rtt_sec:
            return  # Already reacted within this RTT.

        self._loss_epoch = now
        self.window.cwnd = max(
            self.config.min_cwnd,
            int(self.window.cwnd * self.config.md_factor),
        )
        self._clamp_cwnd()
        self._update_schedule_state()

    def on_new_data(self, delta_backlog_bytes: int) -> None:
        """Track new application data queued for transmission (Table 8-2).

        Transitions from IDLE to ACTIVE when data appears and the cwnd
        allows sending.

        Args:
            delta_backlog_bytes: Number of new bytes added to the send queue.
        """
        self.backlog += delta_backlog_bytes
        self._update_schedule_state()

    def on_send(self, nominal_pktsize: int) -> None:
        """Account for a packet being transmitted (Table 8-2: OnSend).

        Increments inflight by the nominal packet size and decrements
        the backlog accordingly.  Updates the schedule state.

        Args:
            nominal_pktsize: Size of the transmitted packet including
                the nominal header overhead (UDP length + nominal_hdrsize).
        """
        self.window.inflight += nominal_pktsize
        self.backlog = max(0, self.backlog - nominal_pktsize)
        self._update_schedule_state()

    def can_send(self) -> bool:
        """Return True if the congestion window allows sending.

        Per spec 8.3.1, a packet may be sent when cwnd > inflight.
        """
        return self.window.cwnd > self.window.inflight

    def get_nominal_pktsize(self, udp_length: int) -> int:
        """Compute nominal packet size per spec 8.3.1.

        ``nominal_pktsize = udp_length + nominal_hdrsize``

        Args:
            udp_length: Length of the UDP payload in bytes.

        Returns:
            The nominal packet size including the fixed header overhead.
        """
        return udp_length + self.config.nominal_hdrsize

    # -- Serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        """Return a JSON-serializable snapshot of the QPCC state.

        Intended for the emulator GUI dashboard and logging.
        """
        return {
            "qpcc_id": self.qpcc_id,
            "schedule_state": self.schedule_state.name,
            "qp_ids": list(self.qp_ids),
            "backlog": self.backlog,
            "config": {
                "target_qdelay": self.config.target_qdelay,
                "min_cwnd": self.config.min_cwnd,
                "max_cwnd": self.config.max_cwnd,
                "initial_cwnd": self.config.initial_cwnd,
                "ai_increment": self.config.ai_increment,
                "md_factor": self.config.md_factor,
                "nominal_hdrsize": self.config.nominal_hdrsize,
            },
            "window": {
                "cwnd": self.window.cwnd,
                "inflight": self.window.inflight,
                "prev_rcvd_bytes": self.window.prev_rcvd_bytes,
                "rtt_estimate": self.window.rtt_estimate,
                "min_rtt": self.window.min_rtt
                if self.window.min_rtt != float("inf")
                else None,
                "saved_cwnd": self.window.saved_cwnd,
            },
        }


# ---------------------------------------------------------------------------
# QPCCManager -- manages multiple QPCCs
# ---------------------------------------------------------------------------

class QPCCManager:
    """Manages a collection of :class:`QPCC` instances.

    Provides creation, lookup, and QP-to-QPCC mapping for the emulator.
    Each QPCC is identified by an auto-incrementing integer ID.
    """

    def __init__(self) -> None:
        self._qpccs: dict[int, QPCC] = {}
        self._next_id: int = 0
        self._qp_map: dict[int, int] = {}  # qp_id -> qpcc_id

    def create_qpcc(self, config: NSCCConfig | None = None) -> QPCC:
        """Create and register a new QPCC.

        Args:
            config: Optional tunable parameters.  Uses defaults if ``None``.

        Returns:
            The newly created :class:`QPCC` instance.
        """
        qpcc_id = self._next_id
        self._next_id += 1
        qpcc = QPCC(qpcc_id, config)
        self._qpccs[qpcc_id] = qpcc
        return qpcc

    def get_qpcc(self, qpcc_id: int) -> QPCC:
        """Look up a QPCC by its identifier.

        Args:
            qpcc_id: The integer identifier assigned at creation.

        Returns:
            The :class:`QPCC` instance.

        Raises:
            KeyError: If no QPCC with the given ID exists.
        """
        return self._qpccs[qpcc_id]

    def map_qp(self, qpcc_id: int, qp_id: int) -> None:
        """Map a QP to a QPCC.

        A QP may only be mapped to one QPCC at a time.  Re-mapping a QP
        removes it from its previous QPCC.

        Args:
            qpcc_id: Target QPCC identifier.
            qp_id: QP identifier to map.

        Raises:
            KeyError: If the QPCC does not exist.
        """
        qpcc = self._qpccs[qpcc_id]  # raises KeyError if missing

        # Remove from previous QPCC if already mapped.
        prev_qpcc_id = self._qp_map.get(qp_id)
        if prev_qpcc_id is not None and prev_qpcc_id in self._qpccs:
            prev_qpcc = self._qpccs[prev_qpcc_id]
            if qp_id in prev_qpcc.qp_ids:
                prev_qpcc.qp_ids.remove(qp_id)

        qpcc.qp_ids.append(qp_id)
        self._qp_map[qp_id] = qpcc_id

    def list_qpccs(self) -> list[dict]:
        """Return a list of JSON-serializable state snapshots for all QPCCs.

        Delegates to :meth:`QPCC.to_dict` for each registered instance.
        """
        return [qpcc.to_dict() for qpcc in self._qpccs.values()]
