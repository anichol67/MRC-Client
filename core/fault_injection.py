"""Receiver-side fault injection and congestion simulation for MRC emulation.

Allows the receiving MRC emulator to simulate congestion signals (ECN marks,
packet drops, trimming, delay) when real ECN from switches is unavailable.
The sender sees identical signals whether congestion is real (from switches)
or simulated (from this module), making it suitable for testing the NSCC
congestion control algorithm and EV state machine.

Key design points:
  - FaultInjector holds an ordered list of FaultRules evaluated per-packet.
  - A single packet can trigger multiple rules (e.g. ECN + delay).
  - Drop takes priority: if any rule drops the packet, trim/ECN/delay are moot.
  - FailureScenario and ScenarioRunner provide canned multi-step scenarios
    such as link failure, spine failure, and link-flap patterns.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ---------------------------------------------------------------------------
# FaultType enum
# ---------------------------------------------------------------------------

class FaultType(Enum):
    """Types of faults that can be injected on the receiver side."""
    ECN_MARK = auto()    # Probabilistic or EV-targeted ECN marking
    DROP_RATE = auto()   # Probabilistic packet drop
    DROP_PSN = auto()    # Drop specific PSNs
    DROP_EV = auto()     # Drop packets with specific EV values
    TRIM = auto()        # Probabilistic packet trimming
    DELAY = auto()       # Inject additional latency


# ---------------------------------------------------------------------------
# FaultRule dataclass
# ---------------------------------------------------------------------------

@dataclass
class FaultRule:
    """A single fault injection rule evaluated against each received packet.

    Attributes:
        fault_id: Auto-assigned unique identifier for this rule.
        fault_type: Which category of fault this rule represents.
        enabled: Whether the rule is currently active.
        drop_rate: Probability of dropping a packet (0.0-1.0, for DROP_RATE).
        drop_psns: Specific PSN values to drop (for DROP_PSN).
        drop_evs: Specific EV values whose packets are dropped (for DROP_EV).
        ecn_rate: Probability of marking a packet as ECN (for ECN_MARK).
        ecn_evs: Specific EV values to always ECN-mark.
        ecn_m_flag: SACK m-flag value: 1 = SKIP_ONCE, 2 = ALWAYS_SKIP.
        trim_rate: Probability of trimming a packet (for TRIM).
        trim_lasthop: If True, use TRIMMED_LASTHOP instead of TRIMMED.
        delay_us: Microseconds of additional delay to inject.
        description: Human-readable description of this rule.
    """
    fault_id: int = -1
    fault_type: FaultType = FaultType.DROP_RATE
    enabled: bool = True
    drop_rate: float = 0.0
    drop_psns: list[int] = field(default_factory=list)
    drop_evs: list[int] = field(default_factory=list)
    ecn_rate: float = 0.0
    ecn_evs: list[int] = field(default_factory=list)
    ecn_m_flag: int = 1
    trim_rate: float = 0.0
    trim_lasthop: bool = False
    delay_us: float = 0.0
    description: str = ''

    def to_dict(self) -> dict:
        """Serialize the rule to a plain dictionary."""
        return {
            'fault_id': self.fault_id,
            'fault_type': self.fault_type.name,
            'enabled': self.enabled,
            'drop_rate': self.drop_rate,
            'drop_psns': list(self.drop_psns),
            'drop_evs': list(self.drop_evs),
            'ecn_rate': self.ecn_rate,
            'ecn_evs': list(self.ecn_evs),
            'ecn_m_flag': self.ecn_m_flag,
            'trim_rate': self.trim_rate,
            'trim_lasthop': self.trim_lasthop,
            'delay_us': self.delay_us,
            'description': self.description,
        }


# ---------------------------------------------------------------------------
# FaultDecision dataclass
# ---------------------------------------------------------------------------

@dataclass
class FaultDecision:
    """Result of evaluating a received packet against all active fault rules.

    A single packet may trigger multiple rules simultaneously (e.g. both an
    ECN mark and an injected delay).  If *should_drop* is True, the packet
    is discarded and trim/ECN/delay fields are irrelevant.

    Attributes:
        should_drop: Whether the packet should be silently dropped.
        should_trim: Whether the packet should be trimmed (payload removed).
        trim_lasthop: If trimmed, whether to use TRIMMED_LASTHOP signal.
        ecn_marked: Whether the packet should carry an ECN congestion mark.
        ecn_m_flag: SACK m-flag value to use if ECN-marked (0 = not set).
        delay_us: Additional delay in microseconds to inject.
        triggered_rules: List of fault_ids whose conditions matched.
    """
    should_drop: bool = False
    should_trim: bool = False
    trim_lasthop: bool = False
    ecn_marked: bool = False
    ecn_m_flag: int = 0
    delay_us: float = 0.0
    triggered_rules: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize the decision to a plain dictionary."""
        return {
            'should_drop': self.should_drop,
            'should_trim': self.should_trim,
            'trim_lasthop': self.trim_lasthop,
            'ecn_marked': self.ecn_marked,
            'ecn_m_flag': self.ecn_m_flag,
            'delay_us': self.delay_us,
            'triggered_rules': list(self.triggered_rules),
        }


# ---------------------------------------------------------------------------
# FaultInjector class
# ---------------------------------------------------------------------------

class FaultInjector:
    """Manages fault injection rules and evaluates packets against them.

    The injector maintains an ordered list of :class:`FaultRule` objects.
    When :meth:`evaluate` is called with a packet's PSN and EV value, every
    enabled rule is checked.  Multiple rules can fire on the same packet;
    drop takes priority over all other effects.

    Stats are tracked for observability and are exposed via :meth:`get_stats`.
    """

    def __init__(self) -> None:
        self.rules: list[FaultRule] = []
        self._next_id: int = 0
        self.stats: dict[str, int] = {
            'total_packets': 0,
            'dropped': 0,
            'trimmed': 0,
            'ecn_marked': 0,
            'delayed': 0,
        }

    # -- Rule management ----------------------------------------------------

    def add_rule(self, rule: FaultRule) -> int:
        """Add a fault rule, assign it a unique ID, and return that ID."""
        rule.fault_id = self._next_id
        self._next_id += 1
        self.rules.append(rule)
        return rule.fault_id

    def remove_rule(self, fault_id: int) -> None:
        """Remove a rule by its fault_id.

        Raises ValueError if the fault_id is not found.
        """
        for i, rule in enumerate(self.rules):
            if rule.fault_id == fault_id:
                self.rules.pop(i)
                return
        raise ValueError(f"No rule with fault_id={fault_id}")

    def enable_rule(self, fault_id: int) -> None:
        """Enable a rule by its fault_id."""
        for rule in self.rules:
            if rule.fault_id == fault_id:
                rule.enabled = True
                return
        raise ValueError(f"No rule with fault_id={fault_id}")

    def disable_rule(self, fault_id: int) -> None:
        """Disable a rule by its fault_id."""
        for rule in self.rules:
            if rule.fault_id == fault_id:
                rule.enabled = False
                return
        raise ValueError(f"No rule with fault_id={fault_id}")

    def clear_rules(self) -> None:
        """Remove all fault rules."""
        self.rules.clear()

    # -- Packet evaluation --------------------------------------------------

    def evaluate(self, psn: int, ev_value: int) -> FaultDecision:
        """Evaluate a received packet against all enabled fault rules.

        Args:
            psn: The packet sequence number of the received packet.
            ev_value: The entropy value the packet was sent on.

        Returns:
            A :class:`FaultDecision` describing what actions to take on the
            packet.  Drop takes priority: if any rule drops the packet,
            trim/ECN/delay effects are still recorded but *should_drop* is
            set True so the caller knows to discard.
        """
        self.stats['total_packets'] += 1
        decision = FaultDecision()

        for rule in self.rules:
            if not rule.enabled:
                continue

            triggered = False

            if rule.fault_type == FaultType.DROP_RATE:
                if rule.drop_rate > 0.0 and random.random() < rule.drop_rate:
                    decision.should_drop = True
                    triggered = True

            elif rule.fault_type == FaultType.DROP_PSN:
                if psn in rule.drop_psns:
                    decision.should_drop = True
                    triggered = True

            elif rule.fault_type == FaultType.DROP_EV:
                if ev_value in rule.drop_evs:
                    decision.should_drop = True
                    triggered = True

            elif rule.fault_type == FaultType.ECN_MARK:
                ecn_hit = False
                if ev_value in rule.ecn_evs:
                    ecn_hit = True
                elif rule.ecn_rate > 0.0 and random.random() < rule.ecn_rate:
                    ecn_hit = True
                if ecn_hit:
                    decision.ecn_marked = True
                    decision.ecn_m_flag = rule.ecn_m_flag
                    triggered = True

            elif rule.fault_type == FaultType.TRIM:
                if rule.trim_rate > 0.0 and random.random() < rule.trim_rate:
                    decision.should_trim = True
                    if rule.trim_lasthop:
                        decision.trim_lasthop = True
                    triggered = True

            elif rule.fault_type == FaultType.DELAY:
                if rule.delay_us > 0.0:
                    decision.delay_us = max(decision.delay_us, rule.delay_us)
                    triggered = True

            if triggered:
                decision.triggered_rules.append(rule.fault_id)

        # Update stats based on the final decision.
        if decision.should_drop:
            self.stats['dropped'] += 1
        if decision.should_trim:
            self.stats['trimmed'] += 1
        if decision.ecn_marked:
            self.stats['ecn_marked'] += 1
        if decision.delay_us > 0.0:
            self.stats['delayed'] += 1

        return decision

    # -- Stats & introspection ----------------------------------------------

    def reset_stats(self) -> None:
        """Reset all counters to zero."""
        for key in self.stats:
            self.stats[key] = 0

    def get_stats(self) -> dict:
        """Return a copy of the current counters."""
        return dict(self.stats)

    def list_rules(self) -> list[dict]:
        """Return all rules as a list of plain dictionaries."""
        return [rule.to_dict() for rule in self.rules]

    def to_dict(self) -> dict:
        """Full state snapshot suitable for GUI serialization."""
        return {
            'rules': self.list_rules(),
            'stats': self.get_stats(),
            'next_id': self._next_id,
        }


# ---------------------------------------------------------------------------
# FailureScenario dataclass
# ---------------------------------------------------------------------------

@dataclass
class FailureScenario:
    """A pre-defined, multi-step failure scenario.

    Each step describes an action to take on the :class:`FaultInjector`
    (add or remove a rule, or wait for a specified duration).

    Attributes:
        scenario_id: Short identifier, e.g. ``'single_link'``.
        name: Human-readable scenario name.
        description: Longer explanation of what the scenario simulates.
        steps: Ordered list of step dicts.  Each step has keys:
            - ``action``: One of ``'add_rule'``, ``'remove_rule'``, ``'wait'``.
            - ``params``: Dict of parameters for the action.
            - ``duration_ms``: How long this step lasts (0 for instant).
    """
    scenario_id: str = ''
    name: str = ''
    description: str = ''
    steps: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize the scenario to a plain dictionary."""
        return {
            'scenario_id': self.scenario_id,
            'name': self.name,
            'description': self.description,
            'steps': list(self.steps),
        }


# ---------------------------------------------------------------------------
# ScenarioRunner class
# ---------------------------------------------------------------------------

class ScenarioRunner:
    """Executes a :class:`FailureScenario` step-by-step against a
    :class:`FaultInjector`.

    Callers advance through the scenario by repeatedly calling
    :meth:`advance_step`.  Each step's action is applied to the injector
    immediately; ``'wait'`` steps are recorded but the caller is responsible
    for the actual timing.

    Attributes:
        injector: The FaultInjector to apply scenario actions to.
        current_scenario: The loaded scenario, or None.
        current_step: Index of the next step to execute.
        is_running: Whether a scenario is in progress.
        history: Log of executed steps with timestamps.
    """

    def __init__(self, injector: FaultInjector) -> None:
        self.injector: FaultInjector = injector
        self.current_scenario: Optional[FailureScenario] = None
        self.current_step: int = 0
        self.is_running: bool = False
        self.history: list[dict] = []

    def load_scenario(self, scenario: FailureScenario) -> None:
        """Load a scenario, resetting any previous state."""
        self.current_scenario = scenario
        self.current_step = 0
        self.is_running = True
        self.history = []

    def advance_step(self) -> bool:
        """Execute the next step in the loaded scenario.

        Returns:
            True if the step was executed and more steps remain, False if
            the scenario is complete (no more steps).

        Raises:
            RuntimeError: If no scenario is loaded or the runner is not
                running.
        """
        if self.current_scenario is None or not self.is_running:
            raise RuntimeError("No scenario is loaded or runner is stopped")

        if self.current_step >= len(self.current_scenario.steps):
            self.is_running = False
            return False

        step = self.current_scenario.steps[self.current_step]
        action = step.get('action', '')
        params = step.get('params', {})
        timestamp = time.time()

        if action == 'add_rule':
            rule = FaultRule(**params)
            fault_id = self.injector.add_rule(rule)
            self.history.append({
                'step': self.current_step,
                'action': action,
                'fault_id': fault_id,
                'timestamp': timestamp,
            })

        elif action == 'remove_rule':
            fault_id = params.get('fault_id', -1)
            try:
                self.injector.remove_rule(fault_id)
            except ValueError:
                pass  # Rule may already have been removed
            self.history.append({
                'step': self.current_step,
                'action': action,
                'fault_id': fault_id,
                'timestamp': timestamp,
            })

        elif action == 'wait':
            duration_ms = step.get('duration_ms', 0)
            self.history.append({
                'step': self.current_step,
                'action': action,
                'duration_ms': duration_ms,
                'timestamp': timestamp,
            })

        else:
            self.history.append({
                'step': self.current_step,
                'action': action,
                'error': f"Unknown action: {action}",
                'timestamp': timestamp,
            })

        self.current_step += 1

        if self.current_step >= len(self.current_scenario.steps):
            self.is_running = False
            return False

        return True

    def reset(self) -> None:
        """Reset the runner, clearing scenario state and history."""
        self.current_scenario = None
        self.current_step = 0
        self.is_running = False
        self.history = []

    def get_progress(self) -> dict:
        """Return current progress through the scenario."""
        total = 0
        if self.current_scenario is not None:
            total = len(self.current_scenario.steps)
        return {
            'current_step': self.current_step,
            'total_steps': total,
            'is_running': self.is_running,
            'scenario_id': (
                self.current_scenario.scenario_id
                if self.current_scenario else None
            ),
        }

    def to_dict(self) -> dict:
        """Full state snapshot for GUI serialization."""
        return {
            'progress': self.get_progress(),
            'history': list(self.history),
            'scenario': (
                self.current_scenario.to_dict()
                if self.current_scenario else None
            ),
        }

    # -- Built-in scenario factories ----------------------------------------

    @staticmethod
    def scenario_single_link_failure(ev_value: int) -> FailureScenario:
        """Simulate a single leaf-to-spine link going down.

        All packets on the given EV are dropped, as if the physical link
        carrying that path has failed.
        """
        return FailureScenario(
            scenario_id='single_link',
            name='Single Link Failure',
            description=(
                f'Drop all packets on EV {ev_value}, simulating one '
                f'leaf-to-spine link down.'
            ),
            steps=[
                {
                    'action': 'add_rule',
                    'params': {
                        'fault_type': FaultType.DROP_EV,
                        'drop_evs': [ev_value],
                        'description': f'Drop all packets on EV {ev_value}',
                    },
                    'duration_ms': 0,
                },
            ],
        )

    @staticmethod
    def scenario_spine_failure(ev_values: list[int]) -> FailureScenario:
        """Simulate a spine switch failure.

        All packets on EVs that traverse the failed spine are dropped.
        """
        return FailureScenario(
            scenario_id='spine_failure',
            name='Spine Failure',
            description=(
                f'Drop all packets on EVs {ev_values}, simulating a '
                f'spine switch going down.'
            ),
            steps=[
                {
                    'action': 'add_rule',
                    'params': {
                        'fault_type': FaultType.DROP_EV,
                        'drop_evs': list(ev_values),
                        'description': (
                            f'Drop all packets on EVs {ev_values} '
                            f'(spine failure)'
                        ),
                    },
                    'duration_ms': 0,
                },
            ],
        )

    @staticmethod
    def scenario_plane_failure(ev_values: list[int]) -> FailureScenario:
        """Simulate a full network plane failure.

        All EVs belonging to the failed plane are dropped.
        """
        return FailureScenario(
            scenario_id='plane_failure',
            name='Plane Failure',
            description=(
                f'Drop all packets on EVs {ev_values}, simulating an '
                f'entire network plane going down.'
            ),
            steps=[
                {
                    'action': 'add_rule',
                    'params': {
                        'fault_type': FaultType.DROP_EV,
                        'drop_evs': list(ev_values),
                        'description': (
                            f'Drop all packets on EVs {ev_values} '
                            f'(plane failure)'
                        ),
                    },
                    'duration_ms': 0,
                },
            ],
        )

    @staticmethod
    def scenario_partial_degradation(ev_value: int) -> FailureScenario:
        """Simulate partial degradation on one path.

        A high drop rate (30%) plus ECN marking on the specified EV,
        modelling a link with serious but not total packet loss.
        """
        return FailureScenario(
            scenario_id='partial_degradation',
            name='Partial Degradation',
            description=(
                f'30% drop rate + ECN marking on EV {ev_value}, simulating '
                f'a degraded but not fully failed link.'
            ),
            steps=[
                {
                    'action': 'add_rule',
                    'params': {
                        'fault_type': FaultType.DROP_RATE,
                        'drop_rate': 0.30,
                        'description': f'30% drop rate on EV {ev_value}',
                    },
                    'duration_ms': 0,
                },
                {
                    'action': 'add_rule',
                    'params': {
                        'fault_type': FaultType.ECN_MARK,
                        'ecn_evs': [ev_value],
                        'ecn_m_flag': 1,
                        'description': f'ECN-mark packets on EV {ev_value}',
                    },
                    'duration_ms': 0,
                },
            ],
        )

    @staticmethod
    def scenario_link_flap(
        ev_value: int,
        flap_count: int = 5,
        interval_ms: int = 2000,
    ) -> FailureScenario:
        """Simulate a flapping link.

        The specified EV alternates between dropped and normal, with
        *flap_count* cycles separated by *interval_ms* milliseconds.
        The caller is responsible for honouring the wait durations.
        """
        steps: list[dict] = []
        for i in range(flap_count):
            # Link goes down: add a drop rule.
            steps.append({
                'action': 'add_rule',
                'params': {
                    'fault_type': FaultType.DROP_EV,
                    'drop_evs': [ev_value],
                    'description': (
                        f'Link flap #{i + 1}: drop EV {ev_value}'
                    ),
                },
                'duration_ms': 0,
            })
            # Wait while the link is down.
            steps.append({
                'action': 'wait',
                'params': {},
                'duration_ms': interval_ms,
            })
            # Link comes back up: remove the rule we just added.
            # The fault_id is not known until runtime; the runner records
            # it in history.  We use a remove_rule action with a sentinel
            # that advance_step resolves from the most recent add_rule.
            steps.append({
                'action': 'remove_rule',
                'params': {'fault_id': -1},  # resolved at execution time
                'duration_ms': 0,
            })
            # Wait while the link is up.
            if i < flap_count - 1:
                steps.append({
                    'action': 'wait',
                    'params': {},
                    'duration_ms': interval_ms,
                })

        return FailureScenario(
            scenario_id='link_flap',
            name='Link Flap',
            description=(
                f'Flap EV {ev_value} {flap_count} times with '
                f'{interval_ms}ms intervals.'
            ),
            steps=steps,
        )
