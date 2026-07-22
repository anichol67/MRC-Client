"""Offline traffic simulation for the OCP MRC emulator.

Runs a closed-loop simulation of an NCCL collective with MRC transport
entirely in memory.  For each flow in a collective step the simulator:

  1. Selects an EV (from a profile or round-robin).
  2. Builds an MRC WRITE packet via PacketBuilder.
  3. Evaluates fault injection on the "received" packet.
  4. Generates the appropriate response (SACK, NACK, or drop).
  5. Updates congestion-control and EV state.

All results are recorded for GUI display.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Optional

from core.collectives import FlowStep, FlowPlan
from core.congestion import QPCC, NSCCConfig
from core.ev_engine import EVProfile, EVState
from core.fault_injection import FaultInjector, FaultDecision
from core.mrc_headers import CCState, NackReason, SackMFlag
from core.packet_builder import PacketBuilder


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FlowResult:
    """Result of simulating a single flow within a step."""
    src_host: str
    dst_host: str
    chunk_id: int
    data_size: int
    ev_value: int
    psn: int
    packet_hex: str       # first 64 bytes hex
    packet_summary: str
    fault_decision: dict   # from FaultDecision.to_dict()
    response_type: str     # 'SACK', 'NACK', 'DROPPED', 'DELAYED_SACK'
    response_details: dict # SACK/NACK fields
    cwnd_after: int
    inflight_after: int
    ev_state_after: str
    rtt_us: float

    def to_dict(self) -> dict:
        return {
            'src_host': self.src_host,
            'dst_host': self.dst_host,
            'chunk_id': self.chunk_id,
            'data_size': self.data_size,
            'ev_value': self.ev_value,
            'psn': self.psn,
            'packet_hex': self.packet_hex,
            'packet_summary': self.packet_summary,
            'fault_decision': self.fault_decision,
            'response_type': self.response_type,
            'response_details': self.response_details,
            'cwnd_after': self.cwnd_after,
            'inflight_after': self.inflight_after,
            'ev_state_after': self.ev_state_after,
            'rtt_us': round(self.rtt_us, 3),
        }


@dataclass
class StepResult:
    """Aggregate result for one step of the collective."""
    step: int
    flows: list  # list[FlowResult]
    total_sent: int
    total_acked: int
    total_dropped: int
    total_trimmed: int
    total_ecn_marked: int
    cwnd_snapshot: int
    inflight_snapshot: int

    def to_dict(self) -> dict:
        return {
            'step': self.step,
            'flows': [f.to_dict() for f in self.flows],
            'total_sent': self.total_sent,
            'total_acked': self.total_acked,
            'total_dropped': self.total_dropped,
            'total_trimmed': self.total_trimmed,
            'total_ecn_marked': self.total_ecn_marked,
            'cwnd_snapshot': self.cwnd_snapshot,
            'inflight_snapshot': self.inflight_snapshot,
        }


@dataclass
class SimulationResult:
    """Complete result from running an entire flow plan."""
    collective_type: str
    num_steps: int
    steps: list        # list[StepResult]
    total_packets: int
    total_bytes: int
    total_dropped: int
    total_retransmitted: int
    total_ecn_events: int
    final_cwnd: int
    ev_state_summary: dict   # count per state name
    timeline: list           # cwnd over time for charting

    def to_dict(self) -> dict:
        return {
            'collective_type': self.collective_type,
            'num_steps': self.num_steps,
            'steps': [s.to_dict() for s in self.steps],
            'total_packets': self.total_packets,
            'total_bytes': self.total_bytes,
            'total_dropped': self.total_dropped,
            'total_retransmitted': self.total_retransmitted,
            'total_ecn_events': self.total_ecn_events,
            'final_cwnd': self.final_cwnd,
            'ev_state_summary': self.ev_state_summary,
            'timeline': self.timeline,
        }


# ---------------------------------------------------------------------------
# Host address mapping helpers
# ---------------------------------------------------------------------------

def _host_ipv6(host_name: str) -> str:
    """Generate a deterministic IPv6 address from a host name."""
    h = hash(host_name) & 0xFFFF
    return f'fd00::{h:x}'


def _host_mac(host_name: str) -> str:
    """Generate a deterministic MAC address from a host name."""
    h = hash(host_name) & 0xFFFFFFFFFF
    octets = [(h >> (i * 8)) & 0xFF for i in range(5)]
    return '02:' + ':'.join(f'{o:02x}' for o in octets)


# ---------------------------------------------------------------------------
# TrafficSimulator
# ---------------------------------------------------------------------------

class TrafficSimulator:
    """Runs a closed-loop MRC traffic simulation in memory.

    Given a packet builder and fault injector, simulates sending each flow
    in a collective step, generating response packets, and updating
    congestion-control and EV state.
    """

    # Simulated base RTT in microseconds.
    BASE_RTT_US: float = 100.0

    def __init__(
        self,
        packet_builder: PacketBuilder,
        fault_injector: FaultInjector,
        ev_profile: Optional[EVProfile] = None,
        cc_config: Optional[NSCCConfig] = None,
    ) -> None:
        self.packet_builder = packet_builder
        self.fault_injector = fault_injector
        self.ev_profile = ev_profile
        self.cc_config = cc_config or NSCCConfig()

        # Congestion controller
        self.cc = QPCC(qpcc_id=0, config=self.cc_config)

        # Packet state
        self._psn: int = 0
        self._ev_counter: int = 0  # used when no EV profile

        # Cumulative rcvd_bytes counter (24-bit, in 256-byte units)
        self._rcvd_bytes_counter: int = 0

        # Tracking
        self._current_step: int = 0
        self._step_results: list = []
        self._total_dropped: int = 0
        self._total_trimmed: int = 0
        self._total_ecn: int = 0
        self._total_bytes: int = 0
        self._total_packets: int = 0
        self._timeline: list = []

    # ------------------------------------------------------------------
    # EV selection
    # ------------------------------------------------------------------

    def _select_ev(self) -> int:
        """Select an EV value from the profile or use round-robin."""
        if self.ev_profile is not None:
            ev = self.ev_profile.select_next_ev()
            if ev is not None:
                return ev.value
        # Fallback: sequential round-robin
        val = self._ev_counter
        self._ev_counter = (self._ev_counter + 1) & 0xFFFF
        return val

    def _get_ev_object(self, ev_value: int):
        """Return the EV object for a given value, if the profile has it."""
        if self.ev_profile is None:
            return None
        for ev in self.ev_profile.ev_universe:
            if ev.value == ev_value:
                return ev
        return None

    def _get_ev_state_str(self, ev_value: int) -> str:
        """Return the current state name of the EV, or 'N/A'."""
        ev_obj = self._get_ev_object(ev_value)
        if ev_obj is not None:
            return ev_obj.state.name
        return 'N/A'

    # ------------------------------------------------------------------
    # Simulated RTT
    # ------------------------------------------------------------------

    def _simulated_rtt_us(self, extra_delay_us: float = 0.0) -> float:
        """Return a simulated RTT with jitter, plus any injected delay."""
        jitter = random.uniform(-10.0, 20.0)
        return max(1.0, self.BASE_RTT_US + jitter + extra_delay_us)

    # ------------------------------------------------------------------
    # run_step
    # ------------------------------------------------------------------

    def run_step(self, flows: list, step_num: int = -1) -> StepResult:
        """Execute one step of a collective flow plan.

        For each flow in the step:
          1. Select an EV from the profile (or use round-robin).
          2. Build an MRC WRITE packet using packet_builder.
          3. Log the packet (hex, summary).
          4. Increment PSN, update CC inflight via on_send().
          5. Evaluate fault injection on the "received" packet.
          6. Based on fault decision:
             - No fault: generate simulated SACK, call cc.on_ack().
             - ECN marked: generate SACK with m=SKIP_ONCE,
               call cc.on_ack(ecn_marked=True), transition EV to SKIP.
             - Dropped: no SACK, increment loss counter.
             - Trimmed: generate NACK with TRIMMED reason,
               call cc.on_nack().
             - Delayed: generate SACK with inflated RTT.
          7. Update EV state based on SACK/NACK m flags.
          8. Record all events in the step result.

        Args:
            flows: List of FlowStep instances for this step.
            step_num: The step number (used for labelling; auto-set if -1).

        Returns:
            StepResult with per-flow details.
        """
        if step_num < 0:
            step_num = self._current_step

        flow_results: list = []
        step_sent = 0
        step_acked = 0
        step_dropped = 0
        step_trimmed = 0
        step_ecn = 0

        for flow in flows:
            ev_value = self._select_ev()
            psn = self._psn

            # Simulated timestamps for CC RTT measurement
            tx_time = time.monotonic()

            # -- Build WRITE packet --
            src_ip = _host_ipv6(flow.src_host)
            dst_ip = _host_ipv6(flow.dst_host)
            src_mac = _host_mac(flow.src_host)
            dst_mac = _host_mac(flow.dst_host)

            # Create a small payload representative of the flow
            payload_size = min(flow.data_size, 128)
            payload = bytes(payload_size)

            pkt = self.packet_builder.build_write(
                src_ipv6=src_ip, dst_ipv6=dst_ip,
                src_mac=src_mac, dst_mac=dst_mac,
                src_qpn=0x100, dst_qpn=0x200,
                psn=psn,
                va=0x1000 + flow.chunk_id * flow.data_size,
                r_key=0xDEAD,
                dmalen=flow.data_size,
                payload=payload,
                ev_value=ev_value,
            )

            # Hex dump (first 64 bytes only)
            raw = pkt.to_bytes()
            packet_hex = raw[:64].hex()
            packet_summary = (
                f'WRITE PSN={psn} EV=0x{ev_value:04X} '
                f'{flow.src_host}->{flow.dst_host} '
                f'chunk={flow.chunk_id} {flow.data_size}B'
            )

            # -- Update PSN and CC --
            self._psn = (self._psn + 1) & 0xFFFFFF
            nominal_size = self.cc.get_nominal_pktsize(flow.data_size)
            self.cc.on_new_data(nominal_size)
            self.cc.on_send(nominal_size)
            step_sent += 1
            self._total_packets += 1
            self._total_bytes += flow.data_size

            # -- Fault injection --
            decision = self.fault_injector.evaluate(psn, ev_value)

            # -- Generate response based on decision --
            rtt_us = self._simulated_rtt_us(decision.delay_us)
            rtt_sec = rtt_us / 1_000_000.0
            arrival_time = tx_time + rtt_sec

            response_type = 'SACK'
            response_details: dict = {}

            if decision.should_drop:
                # Dropped -- no response generated
                response_type = 'DROPPED'
                response_details = {'reason': 'packet_dropped'}
                step_dropped += 1
                self._total_dropped += 1
                # CC treats this as inferred loss
                self.cc.on_inferred_loss()

            elif decision.should_trim:
                # Trimmed -- generate NACK
                response_type = 'NACK'
                nack_reason = (
                    NackReason.TRIMMED_LASTHOP
                    if decision.trim_lasthop
                    else NackReason.TRIMMED
                )
                response_details = {
                    'nack_reason': nack_reason,
                    'nack_reason_name': NackReason(nack_reason).name,
                    'nack_psn': psn,
                }
                step_trimmed += 1
                self._total_trimmed += 1
                self.cc.on_nack(
                    nack_reason=nack_reason,
                    tx_timestamp=tx_time,
                    arrival_time=arrival_time,
                )
                # EV transition: GOOD -> SKIP on trim (not LASTHOP)
                if not decision.trim_lasthop:
                    ev_obj = self._get_ev_object(ev_value)
                    if ev_obj is not None:
                        ev_obj.mark_skip()

            elif decision.ecn_marked:
                # ECN marked -- SACK with m flag
                response_type = 'SACK'
                m_flag = decision.ecn_m_flag or SackMFlag.SKIP_ONCE

                # Update rcvd_bytes counter (24-bit in 256-byte units)
                delta_256 = flow.data_size >> 8
                if delta_256 == 0:
                    delta_256 = 1
                self._rcvd_bytes_counter = (
                    (self._rcvd_bytes_counter + delta_256) & 0xFFFFFF
                )

                response_details = {
                    'm_flag': m_flag,
                    'm_flag_name': (
                        SackMFlag(m_flag).name
                        if m_flag in SackMFlag._value2member_map_
                        else f'0x{m_flag:02X}'
                    ),
                    'ecn_marked': True,
                    'cack_psn': psn,
                    'rcvd_bytes': self._rcvd_bytes_counter,
                }
                step_ecn += 1
                self._total_ecn += 1
                self.cc.on_ack(
                    sack_rcvd_bytes=self._rcvd_bytes_counter,
                    sack_tx_timestamp=tx_time,
                    ecn_marked=True,
                    arrival_time=arrival_time,
                )
                step_acked += 1
                # EV transition based on m flag
                ev_obj = self._get_ev_object(ev_value)
                if ev_obj is not None:
                    if m_flag == SackMFlag.SKIP_ONCE:
                        ev_obj.mark_skip()
                    elif m_flag == SackMFlag.ALWAYS_SKIP:
                        ev_obj.mark_assumed_bad()

            elif decision.delay_us > 0.0:
                # Delayed SACK -- still a SACK but with inflated RTT
                response_type = 'DELAYED_SACK'

                delta_256 = flow.data_size >> 8
                if delta_256 == 0:
                    delta_256 = 1
                self._rcvd_bytes_counter = (
                    (self._rcvd_bytes_counter + delta_256) & 0xFFFFFF
                )

                response_details = {
                    'm_flag': SackMFlag.NONE,
                    'ecn_marked': False,
                    'cack_psn': psn,
                    'rcvd_bytes': self._rcvd_bytes_counter,
                    'delay_us': decision.delay_us,
                    'total_rtt_us': round(rtt_us, 3),
                }
                self.cc.on_ack(
                    sack_rcvd_bytes=self._rcvd_bytes_counter,
                    sack_tx_timestamp=tx_time,
                    ecn_marked=False,
                    arrival_time=arrival_time,
                )
                step_acked += 1

            else:
                # Normal SACK -- no faults
                response_type = 'SACK'

                delta_256 = flow.data_size >> 8
                if delta_256 == 0:
                    delta_256 = 1
                self._rcvd_bytes_counter = (
                    (self._rcvd_bytes_counter + delta_256) & 0xFFFFFF
                )

                response_details = {
                    'm_flag': SackMFlag.NONE,
                    'ecn_marked': False,
                    'cack_psn': psn,
                    'rcvd_bytes': self._rcvd_bytes_counter,
                }
                self.cc.on_ack(
                    sack_rcvd_bytes=self._rcvd_bytes_counter,
                    sack_tx_timestamp=tx_time,
                    ecn_marked=False,
                    arrival_time=arrival_time,
                )
                step_acked += 1

            ev_state_after = self._get_ev_state_str(ev_value)

            flow_results.append(FlowResult(
                src_host=flow.src_host,
                dst_host=flow.dst_host,
                chunk_id=flow.chunk_id,
                data_size=flow.data_size,
                ev_value=ev_value,
                psn=psn,
                packet_hex=packet_hex,
                packet_summary=packet_summary,
                fault_decision=decision.to_dict(),
                response_type=response_type,
                response_details=response_details,
                cwnd_after=self.cc.window.cwnd,
                inflight_after=self.cc.window.inflight,
                ev_state_after=ev_state_after,
                rtt_us=rtt_us,
            ))

        result = StepResult(
            step=step_num,
            flows=flow_results,
            total_sent=step_sent,
            total_acked=step_acked,
            total_dropped=step_dropped,
            total_trimmed=step_trimmed,
            total_ecn_marked=step_ecn,
            cwnd_snapshot=self.cc.window.cwnd,
            inflight_snapshot=self.cc.window.inflight,
        )

        self._step_results.append(result)
        self._current_step += 1
        self._timeline.append({
            'step': step_num,
            'cwnd': self.cc.window.cwnd,
            'inflight': self.cc.window.inflight,
        })

        return result

    # ------------------------------------------------------------------
    # run_full_plan
    # ------------------------------------------------------------------

    def run_full_plan(self, plan: FlowPlan) -> SimulationResult:
        """Run all steps of a flow plan sequentially.

        Args:
            plan: The FlowPlan to simulate.

        Returns:
            SimulationResult with aggregate metrics and per-step details.
        """
        for step_idx, step_flows in enumerate(plan.steps):
            self.run_step(step_flows, step_num=step_idx)

        return self._build_simulation_result(plan)

    # ------------------------------------------------------------------
    # get_state / reset
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        """Return current simulation state for GUI display."""
        ev_summary = {}
        if self.ev_profile is not None:
            for ev in self.ev_profile.ev_universe:
                state_name = ev.state.name
                ev_summary[state_name] = ev_summary.get(state_name, 0) + 1

        return {
            'current_step': self._current_step,
            'psn': self._psn,
            'cwnd': self.cc.window.cwnd,
            'inflight': self.cc.window.inflight,
            'rtt_estimate': round(self.cc.window.rtt_estimate, 3),
            'total_packets': self._total_packets,
            'total_bytes': self._total_bytes,
            'total_dropped': self._total_dropped,
            'total_trimmed': self._total_trimmed,
            'total_ecn': self._total_ecn,
            'ev_state_summary': ev_summary,
            'timeline': self._timeline,
            'steps_completed': len(self._step_results),
            'last_step': (
                self._step_results[-1].to_dict()
                if self._step_results
                else None
            ),
        }

    def reset(self) -> None:
        """Reset all simulation state."""
        self.cc = QPCC(qpcc_id=0, config=self.cc_config)
        self._psn = 0
        self._ev_counter = 0
        self._rcvd_bytes_counter = 0
        self._current_step = 0
        self._step_results = []
        self._total_dropped = 0
        self._total_trimmed = 0
        self._total_ecn = 0
        self._total_bytes = 0
        self._total_packets = 0
        self._timeline = []

        # Reset EV states back to GOOD
        if self.ev_profile is not None:
            for ev in self.ev_profile.ev_universe:
                ev.state = EVState.GOOD
                ev.skip_time = None

        # Reset fault injector stats
        self.fault_injector.reset_stats()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_simulation_result(self, plan: FlowPlan) -> SimulationResult:
        """Build a SimulationResult from accumulated step results."""
        ev_summary = {}
        if self.ev_profile is not None:
            for ev in self.ev_profile.ev_universe:
                state_name = ev.state.name
                ev_summary[state_name] = ev_summary.get(state_name, 0) + 1

        return SimulationResult(
            collective_type=plan.collective_type.value,
            num_steps=len(self._step_results),
            steps=list(self._step_results),
            total_packets=self._total_packets,
            total_bytes=self._total_bytes,
            total_dropped=self._total_dropped,
            total_retransmitted=0,  # retransmits not yet implemented
            total_ecn_events=self._total_ecn,
            final_cwnd=self.cc.window.cwnd,
            ev_state_summary=ev_summary,
            timeline=list(self._timeline),
        )
