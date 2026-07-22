"""
OCP MRC 1.0 Probing — EV Probes, Reliability Probes, and Port Status Updates

Implements the probing mechanisms defined in the OCP Multipath Reliable
Connection Specification Revision 1.0:

  - Reliability Probes (Section 7.4.6): BTH opcode 0xDE addressed to a peer
    QP.  The responder generates a SACK (0xDC) with pr=1.  These do NOT
    consume PSNs; BTH.PSN is reserved.

  - EV Probes (Section 7.4.7): Endpoint Request (0xD8) with BTH.QPN=0x2 and
    BTH.PSN[15:0]=probe_id.  Any traffic class may be used.  The response is
    an Endpoint Response (0xD9) with BTH.QPN=0x2, always on the Control TC.

  - Port Status Updates (Section 7.4.8): Endpoint Request like EV Probes but
    with op=0x00 (PORT_STATUS_UPDATE).  The port_status_mask bitmap indicates
    which ports are reachable (bit=1).

Entropy Values (Section 6.5): The EV value carried in a probe determines the
network path the packet traverses, enabling per-path health measurement.

All classes provide a to_dict() method for JSON serialization to the web GUI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from .mrc_headers import (
    MrcOpcode,
    SackMFlag,
    EndpointOp,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ProbeType(Enum):
    """Probe mechanism as selected by the caller."""
    RELIABILITY_PROBE = auto()
    EV_PROBE = auto()
    PORT_STATUS_UPDATE = auto()


# ---------------------------------------------------------------------------
# ProbeRequest — a single outgoing probe
# ---------------------------------------------------------------------------

@dataclass
class ProbeRequest:
    """Represents a probe packet that has been (or will be) transmitted.

    Fields mirror the spec-level parameters needed to construct the
    appropriate BTH + extended header:

      - Reliability Probes: BTH opcode=0xDE, target_qpn=peer QP.
      - EV Probes: BTH opcode=0xD8, target_qpn=0x2, PSN[15:0]=probe_id.
      - Port Status Updates: same as EV Probe but op=0x00.
    """
    probe_type: ProbeType
    probe_id: int                       # 16-bit requestor-private identifier
    ev_value: int                       # entropy value — determines network path
    target_ipv6: str                    # destination IPv6 address
    target_qpn: int                     # peer QP number (0x2 for endpoint ops)
    source_qpn: int                     # local QP number
    tx_timestamp: int = 0               # 16-bit timestamp at send time
    port_status_mask: int = 0           # 32-bit bitmap, PORT_STATUS_UPDATE only
    sent_time: float = 0.0             # monotonic time when sent (local RTT calc)
    traffic_class: str = "CS0"          # DSCP codepoint to use

    def __post_init__(self) -> None:
        self.probe_id &= 0xFFFF
        self.port_status_mask &= 0xFFFFFFFF

    @property
    def bth_opcode(self) -> int:
        """Return the BTH opcode for this probe type."""
        if self.probe_type is ProbeType.RELIABILITY_PROBE:
            return MrcOpcode.RELIABILITY_PROBE_REQ      # 0xDE
        return MrcOpcode.ENDPOINT_REQUEST               # 0xD8

    @property
    def endpoint_op(self) -> Optional[int]:
        """Return the ERTH op field, or None for reliability probes."""
        if self.probe_type is ProbeType.EV_PROBE:
            return EndpointOp.EV_PROBE                  # 0x01
        if self.probe_type is ProbeType.PORT_STATUS_UPDATE:
            return EndpointOp.PORT_STATUS_UPDATE         # 0x00
        return None

    def to_dict(self) -> dict:
        return {
            "probe_type": self.probe_type.name,
            "probe_id": self.probe_id,
            "ev_value": self.ev_value,
            "ev_hex": f"0x{self.ev_value:08X}",
            "target_ipv6": self.target_ipv6,
            "target_qpn": self.target_qpn,
            "source_qpn": self.source_qpn,
            "bth_opcode": f"0x{self.bth_opcode:02X}",
            "tx_timestamp": self.tx_timestamp,
            "port_status_mask": f"0x{self.port_status_mask:08X}",
            "sent_time": self.sent_time,
            "traffic_class": self.traffic_class,
        }


# ---------------------------------------------------------------------------
# ProbeResult — a received response matched to a request
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """Parsed response for a completed probe.

    For reliability probes the response is a SACK (0xDC) with pr=1.  The
    m field, cack_psn, and sack_bitmap are extracted from the SETH header.

    For EV probes and port-status updates the response is an Endpoint
    Response (0xD9) with BTH.QPN=0x2.
    """
    probe_id: int
    probe_type: ProbeType
    ev_value: int
    rtt_us: float                       # round-trip time in microseconds
    reachable: bool                     # True if a valid response was received
    response_timestamp: int = 0         # responder's tx_timestamp echo
    ecn_marked: bool = False            # from SACK m field for reliability probes
    m_flag: int = 0                     # SACK m field: 0=NONE, 1=SKIP, 2=ALWAYS_SKIP
    cack_psn: Optional[int] = None      # from SACK for reliability probes
    sack_bitmap: Optional[int] = None   # from SACK for reliability probes
    received_time: float = 0.0          # monotonic time when response arrived

    @property
    def m_flag_name(self) -> str:
        if self.m_flag in SackMFlag._value2member_map_:
            return SackMFlag(self.m_flag).name
        return f"UNKNOWN({self.m_flag})"

    def to_dict(self) -> dict:
        d: dict = {
            "probe_id": self.probe_id,
            "probe_type": self.probe_type.name,
            "ev_value": self.ev_value,
            "ev_hex": f"0x{self.ev_value:08X}",
            "rtt_us": round(self.rtt_us, 3),
            "reachable": self.reachable,
            "response_timestamp": self.response_timestamp,
            "ecn_marked": self.ecn_marked,
            "m_flag": self.m_flag,
            "m_flag_name": self.m_flag_name,
            "received_time": self.received_time,
        }
        if self.cack_psn is not None:
            d["cack_psn"] = self.cack_psn
        if self.sack_bitmap is not None:
            d["sack_bitmap"] = f"0x{self.sack_bitmap:016X}"
        return d


# ---------------------------------------------------------------------------
# ProbeSession — outstanding probes for one destination
# ---------------------------------------------------------------------------

class ProbeSession:
    """Manages probe lifecycle for a single target IPv6 address.

    Tracks outstanding (un-replied) probes, completed results, and provides
    per-EV-value health aggregation.
    """

    def __init__(self, session_id: int, target_ipv6: str) -> None:
        self.session_id: int = session_id
        self.target_ipv6: str = target_ipv6
        self.outstanding: dict[int, ProbeRequest] = {}   # probe_id -> request
        self.results: list[ProbeResult] = []
        self.next_probe_id: int = 0

    # -- probe-id allocation ------------------------------------------------

    def _allocate_probe_id(self) -> int:
        pid = self.next_probe_id
        self.next_probe_id = (self.next_probe_id + 1) & 0xFFFF
        return pid

    # -- probe creation -----------------------------------------------------

    def create_reliability_probe(
        self,
        ev_value: int,
        target_qpn: int,
        source_qpn: int,
        traffic_class: str = "CS0",
    ) -> ProbeRequest:
        """Create a Reliability Probe (Section 7.4.6).

        Transmitted as a request without PSN (BTH opcode=0xDE) to a specific
        peer QP.  The EV value selects the network path.  The responder
        generates a SACK with pr=1.  BTH.PSN is reserved and should not be
        consumed.
        """
        probe_id = self._allocate_probe_id()
        now = time.monotonic()
        req = ProbeRequest(
            probe_type=ProbeType.RELIABILITY_PROBE,
            probe_id=probe_id,
            ev_value=ev_value,
            target_ipv6=self.target_ipv6,
            target_qpn=target_qpn,
            source_qpn=source_qpn,
            tx_timestamp=int(now * 1e6) & 0xFFFF,
            sent_time=now,
            traffic_class=traffic_class,
        )
        self.outstanding[probe_id] = req
        return req

    def create_ev_probe(
        self,
        ev_value: int,
        target_qpn: int = 0x2,
        source_qpn: int = 0x2,
        traffic_class: str = "CS0",
    ) -> ProbeRequest:
        """Create an EV Probe (Section 7.4.7).

        Uses Endpoint Request (BTH opcode=0xD8) with BTH.QPN=0x2 and
        BTH.PSN[15:0]=probe_id.  Any traffic class may be used for the
        request; the response always arrives on the Control TC.
        """
        probe_id = self._allocate_probe_id()
        now = time.monotonic()
        req = ProbeRequest(
            probe_type=ProbeType.EV_PROBE,
            probe_id=probe_id,
            ev_value=ev_value,
            target_ipv6=self.target_ipv6,
            target_qpn=target_qpn,
            source_qpn=source_qpn,
            tx_timestamp=int(now * 1e6) & 0xFFFF,
            sent_time=now,
            traffic_class=traffic_class,
        )
        self.outstanding[probe_id] = req
        return req

    def create_port_status_update(
        self,
        port_mask: int,
        target_qpn: int = 0x2,
        source_qpn: int = 0x2,
        traffic_class: str = "CS0",
    ) -> ProbeRequest:
        """Create a Port Status Update (Section 7.4.8).

        Same as an EV Probe but with op=0x00 (PORT_STATUS_UPDATE).  The
        port_status_mask bitmap indicates which local ports are reachable
        (bit=1 means reachable).
        """
        probe_id = self._allocate_probe_id()
        now = time.monotonic()
        req = ProbeRequest(
            probe_type=ProbeType.PORT_STATUS_UPDATE,
            probe_id=probe_id,
            ev_value=0,
            target_ipv6=self.target_ipv6,
            target_qpn=target_qpn,
            source_qpn=source_qpn,
            tx_timestamp=int(now * 1e6) & 0xFFFF,
            port_status_mask=port_mask,
            sent_time=now,
            traffic_class=traffic_class,
        )
        self.outstanding[probe_id] = req
        return req

    # -- response handling --------------------------------------------------

    def record_response(self, probe_id: int, result: ProbeResult) -> None:
        """Match *result* to its outstanding request and finalize RTT.

        If the probe_id is found in *outstanding*, the RTT is computed from
        the original sent_time and the result's received_time.  The request
        is removed from outstanding and the result appended to *results*.

        If the probe_id is not found (duplicate, late, or unsolicited), the
        result is still recorded but RTT is left as-is.
        """
        req = self.outstanding.pop(probe_id, None)
        if req is not None:
            if result.received_time > 0 and req.sent_time > 0:
                result.rtt_us = (result.received_time - req.sent_time) * 1e6
            result.ev_value = req.ev_value
        self.results.append(result)

    # -- timeout management -------------------------------------------------

    def timeout_probes(self, timeout_seconds: float) -> list[int]:
        """Return probe_ids whose requests have exceeded *timeout_seconds*.

        Timed-out probes are removed from *outstanding* and a synthetic
        unreachable ProbeResult is appended for each.
        """
        now = time.monotonic()
        timed_out: list[int] = []
        for pid, req in list(self.outstanding.items()):
            if (now - req.sent_time) >= timeout_seconds:
                timed_out.append(pid)
                self.outstanding.pop(pid)
                self.results.append(ProbeResult(
                    probe_id=pid,
                    probe_type=req.probe_type,
                    ev_value=req.ev_value,
                    rtt_us=0.0,
                    reachable=False,
                    received_time=now,
                ))
        return timed_out

    # -- analytics ----------------------------------------------------------

    def get_path_health(self) -> dict:
        """Aggregate probe results per EV value.

        Returns a dict keyed by EV value (hex string) with:
          - total:        number of probes sent
          - successes:    number with reachable=True
          - success_rate: fraction in [0.0, 1.0]
          - avg_rtt_us:   average RTT of successful probes
          - last_status:  'reachable' or 'unreachable'
        """
        ev_stats: dict[int, dict] = {}
        for r in self.results:
            ev = r.ev_value
            if ev not in ev_stats:
                ev_stats[ev] = {
                    "total": 0,
                    "successes": 0,
                    "rtt_sum": 0.0,
                    "last_reachable": False,
                }
            s = ev_stats[ev]
            s["total"] += 1
            if r.reachable:
                s["successes"] += 1
                s["rtt_sum"] += r.rtt_us
            s["last_reachable"] = r.reachable

        health: dict[str, dict] = {}
        for ev, s in ev_stats.items():
            success_rate = s["successes"] / s["total"] if s["total"] else 0.0
            avg_rtt = s["rtt_sum"] / s["successes"] if s["successes"] else 0.0
            health[f"0x{ev:08X}"] = {
                "total": s["total"],
                "successes": s["successes"],
                "success_rate": round(success_rate, 4),
                "avg_rtt_us": round(avg_rtt, 3),
                "last_status": "reachable" if s["last_reachable"] else "unreachable",
            }
        return health

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "target_ipv6": self.target_ipv6,
            "next_probe_id": self.next_probe_id,
            "outstanding_count": len(self.outstanding),
            "outstanding": {
                pid: req.to_dict() for pid, req in self.outstanding.items()
            },
            "results_count": len(self.results),
            "results": [r.to_dict() for r in self.results],
            "path_health": self.get_path_health(),
        }


# ---------------------------------------------------------------------------
# ProbeManager — top-level probe orchestration
# ---------------------------------------------------------------------------

class ProbeManager:
    """Orchestrates probe sessions across all peer endpoints.

    Provides session management, bulk probing across all EVs in an
    EV profile, and periodic probe scheduling (configuration only --
    actual timer execution is delegated to the Flask layer).
    """

    def __init__(self) -> None:
        self.sessions: dict[str, ProbeSession] = {}   # target_ipv6 -> session
        self._next_session_id: int = 0
        self._periodic_configs: list[dict] = []

    # -- session management -------------------------------------------------

    def create_session(self, target_ipv6: str) -> ProbeSession:
        """Create a new ProbeSession for *target_ipv6*.

        If a session already exists for this target, it is replaced.
        """
        sid = self._next_session_id
        self._next_session_id += 1
        session = ProbeSession(session_id=sid, target_ipv6=target_ipv6)
        self.sessions[target_ipv6] = session
        return session

    def get_session(self, target_ipv6: str) -> ProbeSession:
        """Return the existing session for *target_ipv6*.

        Creates a new session if none exists.
        """
        if target_ipv6 not in self.sessions:
            return self.create_session(target_ipv6)
        return self.sessions[target_ipv6]

    # -- bulk probing -------------------------------------------------------

    def probe_all_evs(
        self,
        target_ipv6: str,
        ev_profile: list[int],
        probe_type: ProbeType = ProbeType.EV_PROBE,
        target_qpn: int = 0x2,
        source_qpn: int = 0x2,
        traffic_class: str = "CS0",
    ) -> list[ProbeRequest]:
        """Create probes for all active EVs in *ev_profile*.

        This enables measuring health across all network paths to a
        destination in a single sweep.  For RELIABILITY_PROBE type the
        caller must supply the appropriate target_qpn and source_qpn for
        the peer QP.

        Returns the list of ProbeRequest objects created.
        """
        session = self.get_session(target_ipv6)
        requests: list[ProbeRequest] = []
        for ev in ev_profile:
            if probe_type is ProbeType.RELIABILITY_PROBE:
                req = session.create_reliability_probe(
                    ev_value=ev,
                    target_qpn=target_qpn,
                    source_qpn=source_qpn,
                    traffic_class=traffic_class,
                )
            elif probe_type is ProbeType.EV_PROBE:
                req = session.create_ev_probe(
                    ev_value=ev,
                    target_qpn=target_qpn,
                    source_qpn=source_qpn,
                    traffic_class=traffic_class,
                )
            elif probe_type is ProbeType.PORT_STATUS_UPDATE:
                req = session.create_port_status_update(
                    port_mask=ev,
                    target_qpn=target_qpn,
                    source_qpn=source_qpn,
                    traffic_class=traffic_class,
                )
            else:
                continue
            requests.append(req)
        return requests

    # -- periodic probing ---------------------------------------------------

    def run_periodic_probes(
        self,
        target_ipv6: str,
        ev_profile: list[int],
        interval_ms: int,
        probe_type: ProbeType = ProbeType.EV_PROBE,
        target_qpn: int = 0x2,
        source_qpn: int = 0x2,
        traffic_class: str = "CS0",
    ) -> dict:
        """Register a periodic probe configuration.

        Stores the parameters for repeated probing.  The actual timer-based
        scheduling is performed by the Flask application layer, which reads
        this configuration and calls probe_all_evs() at each interval.

        Returns the stored configuration dict.
        """
        config = {
            "target_ipv6": target_ipv6,
            "ev_profile": list(ev_profile),
            "interval_ms": interval_ms,
            "probe_type": probe_type.name,
            "target_qpn": target_qpn,
            "source_qpn": source_qpn,
            "traffic_class": traffic_class,
            "created_at": time.monotonic(),
        }
        self._periodic_configs.append(config)
        # Ensure a session exists for this target
        self.get_session(target_ipv6)
        return config

    # -- aggregation --------------------------------------------------------

    def get_all_results(self) -> dict:
        """Aggregate results across all sessions.

        Returns a dict keyed by target IPv6 with per-session summaries
        including path health and counts.
        """
        results: dict[str, dict] = {}
        for ipv6, session in self.sessions.items():
            health = session.get_path_health()
            total = sum(h["total"] for h in health.values()) if health else 0
            successes = sum(h["successes"] for h in health.values()) if health else 0
            results[ipv6] = {
                "session_id": session.session_id,
                "outstanding_count": len(session.outstanding),
                "total_probes": total,
                "total_successes": successes,
                "overall_success_rate": round(
                    successes / total, 4
                ) if total else 0.0,
                "path_health": health,
            }
        return results

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "session_count": len(self.sessions),
            "sessions": {
                ipv6: session.to_dict()
                for ipv6, session in self.sessions.items()
            },
            "periodic_configs": [
                {k: v for k, v in cfg.items() if k != "created_at"}
                for cfg in self._periodic_configs
            ],
            "all_results": self.get_all_results(),
        }
