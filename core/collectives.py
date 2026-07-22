"""
NCCL collective communication pattern emulation for the OCP MRC emulator.

Each collective defines a traffic pattern between MRC endpoint hosts,
generating flow plans that the packet builder uses. Supports ring-based
AllReduce, AllGather, ReduceScatter, rotation-based AllToAll, fan-out
Broadcast, and Point-to-Point patterns.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import math


class CollectiveType(Enum):
    """Supported NCCL collective communication patterns."""
    ALLREDUCE_RING = "allreduce_ring"
    ALLGATHER_RING = "allgather_ring"
    REDUCESCATTER_RING = "reducescatter_ring"
    ALLTOALL = "alltoall"
    BROADCAST = "broadcast"
    POINT_TO_POINT = "point_to_point"


@dataclass
class FlowStep:
    """A single directed flow within one step of a collective.

    Attributes:
        step: Step number in the collective sequence.
        src_host: Source host name (e.g. 'p0-host0').
        dst_host: Destination host name.
        chunk_id: Which chunk of the message data this flow carries.
        data_size: Number of bytes for this chunk.
        description: Human-readable description of this flow.
    """
    step: int
    src_host: str
    dst_host: str
    chunk_id: int
    data_size: int
    description: str

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "src_host": self.src_host,
            "dst_host": self.dst_host,
            "chunk_id": self.chunk_id,
            "data_size": self.data_size,
            "description": self.description,
        }


@dataclass
class FlowPlan:
    """Complete flow plan produced by a collective generator.

    Attributes:
        collective_type: The collective pattern used.
        hosts: Participating host names in ring/topology order.
        message_size: Total message size in bytes.
        chunk_size: Per-chunk size, PMTU-aligned (e.g. 4096).
        num_steps: Number of sequential steps in the collective.
        steps: Flows grouped by step number; steps[s] is a list of
               parallel FlowStep instances that execute concurrently.
        total_bytes: Sum of data_size across every FlowStep.
        total_flows: Total number of individual FlowStep entries.
    """
    collective_type: CollectiveType
    hosts: list
    message_size: int
    chunk_size: int
    num_steps: int
    steps: list  # list[list[FlowStep]]
    total_bytes: int
    total_flows: int

    def to_dict(self) -> dict:
        return {
            "collective_type": self.collective_type.value,
            "hosts": list(self.hosts),
            "message_size": self.message_size,
            "chunk_size": self.chunk_size,
            "num_steps": self.num_steps,
            "steps": [
                [flow.to_dict() for flow in step_flows]
                for step_flows in self.steps
            ],
            "total_bytes": self.total_bytes,
            "total_flows": self.total_flows,
        }


@dataclass
class CollectiveConfig:
    """Configuration for generating a collective flow plan.

    Attributes:
        collective_type: Which collective pattern to generate.
        hosts: Participating hosts, ordered for ring topology.
        message_size: Total message size in bytes (default 1 MB).
        chunk_size: Per-chunk size in bytes, PMTU-aligned (default 4096).
        root_host: Root host for broadcast collective.
        ring_order: Custom ring order; empty list means use hosts order.
    """
    collective_type: CollectiveType
    hosts: list
    message_size: int = 1048576
    chunk_size: int = 4096
    root_host: str = ""
    ring_order: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "collective_type": self.collective_type.value,
            "hosts": list(self.hosts),
            "message_size": self.message_size,
            "chunk_size": self.chunk_size,
            "root_host": self.root_host,
            "ring_order": list(self.ring_order),
        }


def _align_to_pmtu(size: int, pmtu: int) -> int:
    """Align a byte size up to the nearest PMTU boundary."""
    if size <= 0:
        return pmtu
    return math.ceil(size / pmtu) * pmtu


class CollectiveGenerator:
    """Generates flow plans for NCCL collective communication patterns.

    Given a CollectiveConfig, dispatches to the appropriate pattern
    generator and returns a FlowPlan describing every flow in every step.
    The flow plan is pure data -- no packets are sent here.
    """

    def __init__(self, config: CollectiveConfig):
        self.config = config
        # Use custom ring order if specified, otherwise use hosts order
        self._ring = list(config.ring_order) if config.ring_order else list(config.hosts)
        self._n = len(self._ring)

    def generate(self) -> FlowPlan:
        """Generate a FlowPlan by dispatching to the pattern-specific method."""
        generators = {
            CollectiveType.ALLREDUCE_RING: self._generate_allreduce_ring,
            CollectiveType.ALLGATHER_RING: self._generate_allgather_ring,
            CollectiveType.REDUCESCATTER_RING: self._generate_reducescatter_ring,
            CollectiveType.ALLTOALL: self._generate_alltoall,
            CollectiveType.BROADCAST: self._generate_broadcast,
            CollectiveType.POINT_TO_POINT: self._generate_point_to_point,
        }
        generator = generators.get(self.config.collective_type)
        if generator is None:
            raise ValueError(
                f"Unsupported collective type: {self.config.collective_type}"
            )
        return generator()

    def _build_plan(self, steps: list) -> FlowPlan:
        """Build a FlowPlan from a list of step groups."""
        total_bytes = sum(
            flow.data_size for step_flows in steps for flow in step_flows
        )
        total_flows = sum(len(step_flows) for step_flows in steps)
        return FlowPlan(
            collective_type=self.config.collective_type,
            hosts=list(self._ring),
            message_size=self.config.message_size,
            chunk_size=self.config.chunk_size,
            num_steps=len(steps),
            steps=steps,
            total_bytes=total_bytes,
            total_flows=total_flows,
        )

    def _generate_allreduce_ring(self) -> FlowPlan:
        """Ring AllReduce: reduce-scatter followed by all-gather.

        With N hosts the message is split into N chunks. Two phases:

        Phase 1 -- Reduce-Scatter (N-1 steps):
            At step s, host[i] sends chunk (i - s) % N to host[(i+1) % N].
            After this phase each host holds the fully reduced version of
            exactly one chunk.

        Phase 2 -- All-Gather (N-1 steps):
            At step s (offset by N-1), host[i] sends chunk (i - s + 1) % N
            to host[(i+1) % N].  After this phase every host holds all
            fully reduced chunks.

        Total: 2*(N-1) steps.
        """
        n = self._n
        chunk_size = _align_to_pmtu(
            self.config.message_size // n, self.config.chunk_size
        )
        all_steps: list = []

        # Phase 1: Reduce-Scatter
        for s in range(n - 1):
            step_flows: list = []
            for i in range(n):
                src = self._ring[i]
                dst = self._ring[(i + 1) % n]
                chunk_id = (i - s) % n
                step_flows.append(FlowStep(
                    step=s,
                    src_host=src,
                    dst_host=dst,
                    chunk_id=chunk_id,
                    data_size=chunk_size,
                    description=(
                        f"ReduceScatter step {s}: {src} -> {dst} chunk {chunk_id}"
                    ),
                ))
            all_steps.append(step_flows)

        # Phase 2: All-Gather
        for s in range(n - 1):
            global_step = (n - 1) + s
            step_flows = []
            for i in range(n):
                src = self._ring[i]
                dst = self._ring[(i + 1) % n]
                chunk_id = (i - s + 1) % n
                step_flows.append(FlowStep(
                    step=global_step,
                    src_host=src,
                    dst_host=dst,
                    chunk_id=chunk_id,
                    data_size=chunk_size,
                    description=(
                        f"AllGather step {s}: {src} -> {dst} chunk {chunk_id}"
                    ),
                ))
            all_steps.append(step_flows)

        return self._build_plan(all_steps)

    def _generate_allgather_ring(self) -> FlowPlan:
        """Ring AllGather: each host circulates its chunk around the ring.

        N-1 steps. At step s, host[i] sends chunk (i - s) % N to
        host[(i+1) % N].
        """
        n = self._n
        chunk_size = _align_to_pmtu(
            self.config.message_size // n, self.config.chunk_size
        )
        all_steps: list = []

        for s in range(n - 1):
            step_flows: list = []
            for i in range(n):
                src = self._ring[i]
                dst = self._ring[(i + 1) % n]
                chunk_id = (i - s) % n
                step_flows.append(FlowStep(
                    step=s,
                    src_host=src,
                    dst_host=dst,
                    chunk_id=chunk_id,
                    data_size=chunk_size,
                    description=(
                        f"AllGather step {s}: {src} -> {dst} chunk {chunk_id}"
                    ),
                ))
            all_steps.append(step_flows)

        return self._build_plan(all_steps)

    def _generate_reducescatter_ring(self) -> FlowPlan:
        """Ring ReduceScatter: reduce then scatter across the ring.

        N-1 steps. At step s, host[i] sends chunk (i - s) % N to
        host[(i+1) % N]. Same ring pattern as AllGather, different
        semantic meaning (data is reduced, not just gathered).
        """
        n = self._n
        chunk_size = _align_to_pmtu(
            self.config.message_size // n, self.config.chunk_size
        )
        all_steps: list = []

        for s in range(n - 1):
            step_flows: list = []
            for i in range(n):
                src = self._ring[i]
                dst = self._ring[(i + 1) % n]
                chunk_id = (i - s) % n
                step_flows.append(FlowStep(
                    step=s,
                    src_host=src,
                    dst_host=dst,
                    chunk_id=chunk_id,
                    data_size=chunk_size,
                    description=(
                        f"ReduceScatter step {s}: {src} -> {dst} chunk {chunk_id}"
                    ),
                ))
            all_steps.append(step_flows)

        return self._build_plan(all_steps)

    def _generate_alltoall(self) -> FlowPlan:
        """AllToAll: every host sends unique data to every other host.

        N-1 steps using a rotation pattern. At step s, host[i] sends to
        host[(i + s + 1) % N]. Each host sends message_size / N bytes
        to each destination.
        """
        n = self._n
        per_peer_size = _align_to_pmtu(
            self.config.message_size // n, self.config.chunk_size
        )
        all_steps: list = []

        for s in range(n - 1):
            step_flows: list = []
            for i in range(n):
                src = self._ring[i]
                dst_idx = (i + s + 1) % n
                dst = self._ring[dst_idx]
                # chunk_id encodes the destination index from this source
                chunk_id = dst_idx
                step_flows.append(FlowStep(
                    step=s,
                    src_host=src,
                    dst_host=dst,
                    chunk_id=chunk_id,
                    data_size=per_peer_size,
                    description=(
                        f"AllToAll step {s}: {src} -> {dst} "
                        f"({per_peer_size} bytes)"
                    ),
                ))
            all_steps.append(step_flows)

        return self._build_plan(all_steps)

    def _generate_broadcast(self) -> FlowPlan:
        """Broadcast: fan-out from root to all other hosts.

        Single step with N-1 parallel flows from the root host to every
        other participating host. Each flow carries the full message.
        """
        root = self.config.root_host
        if not root:
            root = self._ring[0]

        step_flows: list = []
        for i, host in enumerate(self._ring):
            if host == root:
                continue
            step_flows.append(FlowStep(
                step=0,
                src_host=root,
                dst_host=host,
                chunk_id=0,
                data_size=self.config.message_size,
                description=f"Broadcast: {root} -> {host}",
            ))

        return self._build_plan([step_flows] if step_flows else [])

    def _generate_point_to_point(self) -> FlowPlan:
        """Point-to-Point: single flow from hosts[0] to hosts[1].

        Used for baseline testing. Requires at least two hosts.
        """
        if len(self._ring) < 2:
            raise ValueError(
                "Point-to-point requires at least 2 hosts, "
                f"got {len(self._ring)}"
            )

        src = self._ring[0]
        dst = self._ring[1]
        step_flows = [
            FlowStep(
                step=0,
                src_host=src,
                dst_host=dst,
                chunk_id=0,
                data_size=self.config.message_size,
                description=f"P2P: {src} -> {dst}",
            )
        ]

        return self._build_plan([step_flows])


class TrafficOrchestrator:
    """Manages execution progress through a flow plan.

    Tracks the current step, provides access to per-step flows, and
    records per-step completion results. The orchestrator does not send
    packets itself -- it is a state machine over the pure-data FlowPlan.
    """

    def __init__(self):
        self.current_plan: Optional[FlowPlan] = None
        self.current_step: int = 0
        self.is_running: bool = False
        self.results: list = []

    def load_plan(self, plan: FlowPlan) -> None:
        """Set the active flow plan and reset state."""
        self.current_plan = plan
        self.current_step = 0
        self.is_running = True
        self.results = []

    def get_current_step_flows(self) -> list:
        """Return the list of FlowStep instances for the current step.

        Returns an empty list if no plan is loaded or the plan is
        complete.
        """
        if self.current_plan is None:
            return []
        if self.current_step >= self.current_plan.num_steps:
            return []
        return self.current_plan.steps[self.current_step]

    def advance_step(self) -> bool:
        """Move to the next step.

        Returns True if there are more steps remaining, False if the
        plan is now complete.
        """
        if self.current_plan is None:
            return False

        self.current_step += 1
        if self.current_step >= self.current_plan.num_steps:
            self.is_running = False
            return False
        return True

    def reset(self) -> None:
        """Reset to step 0 without clearing the loaded plan."""
        self.current_step = 0
        if self.current_plan is not None:
            self.is_running = True
        self.results = []

    def get_progress(self) -> dict:
        """Return progress information for the current plan."""
        if self.current_plan is None:
            return {
                "current_step": 0,
                "total_steps": 0,
                "percent_complete": 0.0,
                "is_running": False,
            }

        total = self.current_plan.num_steps
        pct = (self.current_step / total * 100.0) if total > 0 else 100.0
        return {
            "current_step": self.current_step,
            "total_steps": total,
            "percent_complete": round(pct, 1),
            "is_running": self.is_running,
        }

    def to_dict(self) -> dict:
        return {
            "current_plan": (
                self.current_plan.to_dict() if self.current_plan else None
            ),
            "current_step": self.current_step,
            "is_running": self.is_running,
            "results": list(self.results),
            "progress": self.get_progress(),
        }
