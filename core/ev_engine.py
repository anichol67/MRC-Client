"""
Entropy Value (EV) Management Engine for the OCP MRC Specification Rev 1.0.

Implements EV state management, profile management, structured EV packing,
SRv6 address construction, and pseudo-random EV selection per spec sections
9.3.x and Figure 5 (EV state machine).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EVState(IntEnum):
    """EV lifecycle states per spec Figure 5."""
    GOOD = 0
    DENIED = 1
    SKIP = 2
    ASSUMED_BAD = 3


class EVFormat(IntEnum):
    """Supported EV encoding formats."""
    ECMP = 0            # 16-bit hash-based ECMP
    STRUCTURED_EV = 1   # 32-bit structured (hop fields, Table 9-1)
    SRV6_USID = 2       # 128-bit SRv6 with micro-SIDs
    SRV6_USID_SRH = 3   # 256-bit SRv6 with SRH compressed segments


class EVMode(IntEnum):
    """How EVs are sourced for a profile."""
    AUTO = 0        # Implementation picks EVs automatically
    EXPLICIT = 1    # Operator supplies a fixed EV list
    GENERATED = 2   # EVs generated from hop-field enumeration


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HopField:
    """Defines one hop within a Structured EV (Table 9-1).

    The hop occupies *width_bits* bits inside the 32-bit structured EV
    value.  Valid EV values for this hop range from *min_value* to
    *max_value* inclusive.
    """
    width_bits: int
    min_value: int
    max_value: int

    def __post_init__(self) -> None:
        if self.width_bits < 1:
            raise ValueError("width_bits must be >= 1")
        field_max = (1 << self.width_bits) - 1
        if self.max_value > field_max:
            raise ValueError(
                f"max_value {self.max_value} exceeds {self.width_bits}-bit "
                f"maximum ({field_max})"
            )
        if self.min_value < 0 or self.min_value > self.max_value:
            raise ValueError(
                f"min_value ({self.min_value}) must be in "
                f"[0, max_value ({self.max_value})]"
            )

    def to_dict(self) -> dict:
        return {
            "width_bits": self.width_bits,
            "min_value": self.min_value,
            "max_value": self.max_value,
        }


@dataclass
class SRv6Config:
    """Configuration for SRv6 uSID-based EV encoding.

    *lid*          -- Locator ID prefix (variable length, typically 6 bytes).
    *usid_width*   -- 16 (F1616) or 32 (F3216) bits per uSID.
    *usids*        -- Up to 6 micro-SID values.
    *use_srh*      -- Whether an SRH with compressed segments is appended.
    *srh_segments* -- Optional SRH compressed segment list (128-bit each).
    """
    lid: bytes
    usid_width: int  # 16 or 32
    usids: list[int] = field(default_factory=list)
    use_srh: bool = False
    srh_segments: list[bytes] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.usid_width not in (16, 32):
            raise ValueError("usid_width must be 16 (F1616) or 32 (F3216)")
        if len(self.usids) > 6:
            raise ValueError("At most 6 uSIDs are supported")

    def to_dict(self) -> dict:
        return {
            "lid": self.lid.hex(),
            "usid_width": self.usid_width,
            "usids": list(self.usids),
            "use_srh": self.use_srh,
            "srh_segments": [s.hex() for s in self.srh_segments],
        }


# ---------------------------------------------------------------------------
# EV (single entropy value)
# ---------------------------------------------------------------------------

@dataclass
class EV:
    """A single entropy value with its lifecycle state.

    State transitions follow the state machine in spec Figure 5.
    """
    value: int
    state: EVState = EVState.GOOD
    skip_time: Optional[float] = None

    # -- state transitions ---------------------------------------------------

    def mark_skip(self) -> None:
        """GOOD -> SKIP  (on SACK m=skip or NACK TRIMMED, not LASTHOP)."""
        if self.state != EVState.GOOD:
            return
        self.state = EVState.SKIP
        self.skip_time = time.time()

    def mark_denied(self) -> None:
        """GOOD -> DENIED  (admin disable)."""
        if self.state != EVState.GOOD:
            return
        self.state = EVState.DENIED
        self.skip_time = None

    def mark_assumed_bad(self) -> None:
        """GOOD -> ASSUMED_BAD  (SACK m=always_skip or bad path detected)."""
        if self.state != EVState.GOOD:
            return
        self.state = EVState.ASSUMED_BAD
        self.skip_time = None

    def resolve_skip(self) -> None:
        """SKIP -> GOOD  (implementation-defined timeout expired)."""
        if self.state != EVState.SKIP:
            return
        self.state = EVState.GOOD
        self.skip_time = None

    def admin_enable(self) -> None:
        """DENIED -> GOOD  (admin re-enable)."""
        if self.state != EVState.DENIED:
            return
        self.state = EVState.GOOD

    def probe_resolved(self) -> None:
        """ASSUMED_BAD -> GOOD  (probe response m=NONE or SKIP_ONCE)."""
        if self.state != EVState.ASSUMED_BAD:
            return
        self.state = EVState.GOOD

    def needs_probe(self) -> bool:
        """Return True if this EV is ASSUMED_BAD and should be probed."""
        return self.state == EVState.ASSUMED_BAD

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "state": self.state.name,
            "skip_time": self.skip_time,
        }


# ---------------------------------------------------------------------------
# EVProfile
# ---------------------------------------------------------------------------

class EVProfile:
    """A named collection of EVs sharing the same format and selection mode.

    Provides structured-EV packing/unpacking (Table 9-1), SRv6 address
    construction, EV generation from hop fields, and pseudo-random
    selection (spec 9.3.1).
    """

    _next_id: int = 1  # class-level auto-increment for profile IDs

    def __init__(
        self,
        name: str,
        mode: EVMode,
        ev_format: EVFormat,
        profile_id: Optional[int] = None,
    ) -> None:
        if profile_id is not None:
            self.profile_id = profile_id
        else:
            self.profile_id = EVProfile._next_id
            EVProfile._next_id += 1
        self.name: str = name
        self.mode: EVMode = mode
        self.ev_format: EVFormat = ev_format
        self.ev_universe: list[EV] = []
        self.hop_fields: list[HopField] = []
        self.srv6_config: Optional[SRv6Config] = None

    # -- properties ----------------------------------------------------------

    @property
    def active_evs(self) -> list[EV]:
        """Return EVs whose state is GOOD."""
        return [ev for ev in self.ev_universe if ev.state == EVState.GOOD]

    @property
    def inactive_evs(self) -> list[EV]:
        """Return EVs whose state is NOT GOOD."""
        return [ev for ev in self.ev_universe if ev.state != EVState.GOOD]

    # -- basic EV management -------------------------------------------------

    def add_ev(self, value: int) -> EV:
        """Add a new EV to the universe and return it."""
        ev = EV(value=value)
        self.ev_universe.append(ev)
        return ev

    def remove_ev(self, value: int) -> None:
        """Remove the first EV matching *value* from the universe."""
        for i, ev in enumerate(self.ev_universe):
            if ev.value == value:
                self.ev_universe.pop(i)
                return
        raise ValueError(f"EV with value {value} not found in profile")

    # -- selection (spec 9.3.1) ----------------------------------------------

    def select_next_ev(self) -> Optional[EV]:
        """Pseudo-random selection from active (GOOD) EVs.

        Returns None if no EVs are active.  Uses shuffling to avoid
        synchronization between QPs (spec 9.3.1).
        """
        candidates = self.active_evs
        if not candidates:
            return None
        return random.choice(candidates)

    # -- EV generation from hop fields (GENERATED mode) ----------------------

    def generate_evs(
        self,
        hop_fields: list[HopField],
        count: int,
    ) -> list[EV]:
        """Enumerate structured EVs from hop-field cross-product.

        Stores *hop_fields* on the profile, enumerates all combinations of
        hop values within each field's [min_value, max_value] range, packs
        them via :meth:`build_structured_ev`, and adds up to *count* EVs.

        Returns the list of newly created :class:`EV` instances.
        """
        self.hop_fields = list(hop_fields)
        total_bits = sum(h.width_bits for h in hop_fields)
        if total_bits > 32:
            raise ValueError(
                f"Total hop-field width ({total_bits} bits) exceeds 32 bits"
            )

        # Build the full cross-product lazily so we can cap at *count*.
        ranges = [range(h.min_value, h.max_value + 1) for h in hop_fields]
        added: list[EV] = []
        for combo in _cartesian(ranges):
            if len(added) >= count:
                break
            packed = self.build_structured_ev(list(combo))
            ev = self.add_ev(packed)
            added.append(ev)
        return added

    # -- structured EV packing / unpacking (Table 9-1) -----------------------

    def build_structured_ev(self, hop_values: list[int]) -> int:
        """Pack per-hop values into a 32-bit structured EV (MSB-first).

        The 32-bit value maps to UDP.src_port[15:0] || IPv6.flow_label[15:0].
        """
        if len(hop_values) != len(self.hop_fields):
            raise ValueError(
                f"Expected {len(self.hop_fields)} hop values, "
                f"got {len(hop_values)}"
            )
        result = 0
        for hf, val in zip(self.hop_fields, hop_values):
            if val < hf.min_value or val > hf.max_value:
                raise ValueError(
                    f"Hop value {val} out of range "
                    f"[{hf.min_value}, {hf.max_value}]"
                )
            result = (result << hf.width_bits) | (val & ((1 << hf.width_bits) - 1))
        return result

    def parse_structured_ev(self, ev_value: int) -> list[int]:
        """Unpack a 32-bit structured EV into per-hop values."""
        values: list[int] = []
        # Walk fields from LSB to MSB, then reverse.
        for hf in reversed(self.hop_fields):
            mask = (1 << hf.width_bits) - 1
            values.append(ev_value & mask)
            ev_value >>= hf.width_bits
        values.reverse()
        return values

    # -- SRv6 address construction -------------------------------------------

    def build_srv6_address(self, usid_values: list[int]) -> bytes:
        """Build a 128-bit SRv6 address from the profile's LID and the
        supplied uSID values.

        Layout (F1616 example):
            LID (48 bits) | uSID0 (16b) | uSID1 (16b) | ... | padding

        The total must fit within 128 bits (16 bytes).
        """
        if self.srv6_config is None:
            raise RuntimeError("SRv6 config not set on this profile")
        cfg = self.srv6_config
        if len(usid_values) > 6:
            raise ValueError("At most 6 uSIDs are supported")

        lid_bits = len(cfg.lid) * 8
        usid_bits = cfg.usid_width * len(usid_values)
        total_bits = lid_bits + usid_bits
        if total_bits > 128:
            raise ValueError(
                f"LID ({lid_bits}b) + uSIDs ({usid_bits}b) exceeds 128 bits"
            )

        # Build as a big integer, then convert to 16 bytes.
        addr_int = int.from_bytes(cfg.lid, "big")
        addr_int <<= (128 - lid_bits)  # shift LID to top

        bit_offset = lid_bits
        for usid_val in usid_values:
            shift = 128 - bit_offset - cfg.usid_width
            addr_int |= (usid_val & ((1 << cfg.usid_width) - 1)) << shift
            bit_offset += cfg.usid_width

        return addr_int.to_bytes(16, "big")

    # -- skip recovery -------------------------------------------------------

    def recover_skip_evs(self, timeout_seconds: float) -> list[EV]:
        """Transition SKIP EVs back to GOOD after *timeout_seconds*.

        Returns the list of recovered EVs.
        """
        now = time.time()
        recovered: list[EV] = []
        for ev in self.ev_universe:
            if ev.state == EVState.SKIP and ev.skip_time is not None:
                if (now - ev.skip_time) >= timeout_seconds:
                    ev.resolve_skip()
                    recovered.append(ev)
        return recovered

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict:
        result: dict = {
            "profile_id": self.profile_id,
            "name": self.name,
            "mode": self.mode.name,
            "ev_format": self.ev_format.name,
            "ev_universe": [ev.to_dict() for ev in self.ev_universe],
            "active_count": len(self.active_evs),
            "inactive_count": len(self.inactive_evs),
            "hop_fields": [h.to_dict() for h in self.hop_fields],
        }
        if self.srv6_config is not None:
            result["srv6_config"] = self.srv6_config.to_dict()
        return result


# ---------------------------------------------------------------------------
# EVProfileManager
# ---------------------------------------------------------------------------

class EVProfileManager:
    """Top-level manager for multiple :class:`EVProfile` instances."""

    def __init__(self) -> None:
        self._profiles: dict[int, EVProfile] = {}

    def create_profile(
        self,
        name: str,
        mode: EVMode,
        ev_format: EVFormat,
    ) -> EVProfile:
        """Create and register a new profile, returning it."""
        profile = EVProfile(name=name, mode=mode, ev_format=ev_format)
        self._profiles[profile.profile_id] = profile
        return profile

    def delete_profile(self, profile_id: int) -> None:
        """Remove a profile by ID."""
        if profile_id not in self._profiles:
            raise KeyError(f"Profile {profile_id} not found")
        del self._profiles[profile_id]

    def get_profile(self, profile_id: int) -> EVProfile:
        """Retrieve a profile by ID."""
        if profile_id not in self._profiles:
            raise KeyError(f"Profile {profile_id} not found")
        return self._profiles[profile_id]

    def list_profiles(self) -> list[dict]:
        """Return a JSON-serializable summary of all profiles."""
        return [p.to_dict() for p in self._profiles.values()]

    def deny_ev(self, profile_id: int, ev_index: int) -> None:
        """Admin-disable the EV at *ev_index* in the given profile."""
        profile = self.get_profile(profile_id)
        if ev_index < 0 or ev_index >= len(profile.ev_universe):
            raise IndexError(
                f"ev_index {ev_index} out of range for profile "
                f"with {len(profile.ev_universe)} EVs"
            )
        profile.ev_universe[ev_index].mark_denied()

    def enable_ev(self, profile_id: int, ev_index: int) -> None:
        """Admin-enable the EV at *ev_index* in the given profile."""
        profile = self.get_profile(profile_id)
        if ev_index < 0 or ev_index >= len(profile.ev_universe):
            raise IndexError(
                f"ev_index {ev_index} out of range for profile "
                f"with {len(profile.ev_universe)} EVs"
            )
        profile.ev_universe[ev_index].admin_enable()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cartesian(ranges: list[range]):
    """Yield tuples from the Cartesian product of *ranges* (lazy)."""
    if not ranges:
        yield ()
        return
    first, *rest = ranges
    for val in first:
        for tail in _cartesian(rest):
            yield (val, *tail)
