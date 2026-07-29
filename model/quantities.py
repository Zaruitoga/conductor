"""
model/quantities.py — From "which slot was it wired to" to "what does it mean".

The defect this exists to fix: the old pipeline asked `typeId == 0x10` and then
demanded seven field names.  The model was coupled to the *wiring* instead of to
the *physics*, so changing the BNO configuration broke it.

Here, every packet is decanted into canonical quantities named for what they
are.  It does not matter whether the gyro arrived as its own 0x01 packet or
bundled inside super slot 3 — both become `omega`.  A signal then declares
`needs=("omega",)` and stops caring how the ESP is set up.

    attitude_rel   orientation, gravity-referenced, yaw relative and drifting
                   slowly.  GAME_RV, else ARVR_RV.  Immune to steel.
    attitude_abs   orientation with a magnetic yaw reference.  RV, else GEO_RV.
                   Absolute heading, but corrupted near steel and lighting rigs
                   — which is why signals ask for one or the other explicitly
                   rather than for "the quaternion".
    omega          body-frame angular velocity, rad/s.  GYRO.
    accel          body-frame specific force, m/s².  ACCEL.
    linear_accel   gravity removed by the sensor's own fusion.  LINEAR_ACCEL.
    mag            geomagnetic field, µT.  MAG.

Sampling coherence
------------------
A super slot is not merely a bandwidth optimisation: it is the only way to get
attitude and angular velocity sampled at the *same instant*.  Delivered as two
separate 100 Hz streams they are interleaved, not aligned.  The resolver records
which quantities arrived in the same datagram (`bundle`) and how stale each one
is relative to the tick, so a signal that needs coherence can be honest about
what it got instead of quietly integrating mismatched samples.
"""

import logging

from transport.protocol import SLOT_FIELDS, SLOT_NAME, TYPE_NAME

log = logging.getLogger("model.quantities")

# ── Canonical quantity names ─────────────────────────────────────────────────
ATTITUDE_REL = "attitude_rel"
ATTITUDE_ABS = "attitude_abs"
OMEGA        = "omega"
ACCEL        = "accel"
LINEAR_ACCEL = "linear_accel"
MAG          = "mag"

QUANTITIES = (ATTITUDE_REL, ATTITUDE_ABS, OMEGA, ACCEL, LINEAR_ACCEL, MAG)

# Which simple slot feeds which quantity, and in what order of preference when
# several are available (lower is better). Slot indices follow the firmware's
# SLOT_DEF order, mirrored in protocol.SLOT_NAME.
_SLOT_QUANTITY: dict[int, tuple[str, int]] = {
    0: (OMEGA,        0),   # GYRO
    1: (ACCEL,        0),   # ACCEL
    2: (MAG,          0),   # MAG
    3: (LINEAR_ACCEL, 0),   # LINEAR_ACCEL
    4: (ATTITUDE_ABS, 0),   # RV       — magnetic yaw reference
    5: (ATTITUDE_ABS, 1),   # GEO_RV   — same, lower rate
    6: (ATTITUDE_REL, 0),   # GAME_RV  — no magnetometer
    7: (ATTITUDE_REL, 1),   # ARVR_RV  — stabilised variant
}

# Field names as they appear on a *simple* packet (0x01–0x08). The same physical
# value carries different keys depending on how it was transported, which is
# exactly the asymmetry this module erases.
_SIMPLE_VEC3 = ("x", "y", "z")
_SIMPLE_QUAT = ("qw", "qx", "qy", "qz")

# A quantity is considered coherent with the tick if it arrived in the same
# datagram; otherwise its age is reported and the caller decides.
_QUAT_QUANTITIES = frozenset({ATTITUDE_REL, ATTITUDE_ABS})


def quantity_of_slot(slot: int) -> str | None:
    """Canonical quantity fed by a simple slot, or None if the slot is unknown."""
    entry = _SLOT_QUANTITY.get(slot)
    return entry[0] if entry else None


def _rank(key: tuple) -> tuple:
    """Sort key for a source, lower being better. See QuantityResolver._offer."""
    slot, bundled = key
    return (_SLOT_QUANTITY[slot][1], 0 if bundled else 1)


class _Sample:
    """The latest value of one quantity, with where and when it came from."""

    __slots__ = ("value", "t_us", "slot", "bundle", "bundled")

    def __init__(self, value: tuple, t_us: int, slot: int, bundle: int,
                 bundled: bool):
        self.value   = value
        self.t_us    = t_us
        self.slot    = slot
        self.bundle  = bundle
        self.bundled = bundled    # arrived alongside other quantities

    @property
    def key(self) -> tuple:
        """Identity of the source, for deciding whether another may take over."""
        return (self.slot, self.bundled)


class QuantityResolver:
    """
    Decants wire packets into canonical quantities.

    Stateful (it holds the latest sample of each quantity) and driven from a
    single task.  `reset()` clears it, exactly like a stateful model node — a
    replay must not start with the last live sample still in place.
    """

    def __init__(self, source_timeout_us: int = 500_000,
                 presence_timeout_us: int = 2_000_000):
        # How long a source may go quiet before a lesser one may take over. Same
        # order as the clock's gap tolerance: below it, a silence is a hiccup;
        # above it, the sensor is gone.
        self._source_timeout_us = int(source_timeout_us)
        # How long a quantity survives without a sample before it counts as gone.
        # More generous, because a slow stream is not an absent one — but finite,
        # because a sensor switched off mid-session must stop being "available"
        # rather than keep offering its last value forever.
        self._presence_timeout_us = int(presence_timeout_us)
        self.reset()

    def reset(self) -> None:
        self._samples: dict[str, _Sample] = {}
        self._bundle = 0
        self._sources_key: tuple = ()
        self.ingested = 0
        self.ignored  = 0

    # ── Ingestion ────────────────────────────────────────────────────────────

    def ingest(self, packet: dict, t_us: int) -> frozenset[str]:
        """
        Decant one wire packet. Returns the set of quantities it updated.

        An empty set means the packet carried nothing the model can use (a
        heartbeat, a sensor nobody needs, or a value a better source already
        supplies) — not an error.
        """
        type_id = packet.get("typeId")
        if not isinstance(type_id, int):
            return frozenset()

        carried = self._decant(packet, type_id)
        if not carried:
            self.ignored += 1
            return frozenset()

        self._bundle += 1
        # A packet carrying several quantities sampled them at the same instant.
        # That coherence is the reason super slots exist, and the reason such a
        # source outranks the same sensor delivered on its own.
        bundled = len(carried) > 1

        updated = {
            quantity
            for quantity, slot, value in carried
            if self._offer(quantity, value, t_us, slot, bundled)
        }
        if updated:
            self.ingested += 1
        else:
            self.ignored += 1
        return frozenset(updated)

    def _decant(self, packet: dict, type_id: int) -> list[tuple]:
        """Every (quantity, slot, value) this packet carries."""
        if 0x01 <= type_id <= 0x08:
            # Simple packet: the slot is the type, and the payload keys are the
            # generic x/y/z or qw/qx/qy/qz.
            slot = type_id - 1
            quantity = quantity_of_slot(slot)
            if quantity is None:
                return []
            keys = _SIMPLE_QUAT if quantity in _QUAT_QUANTITIES else _SIMPLE_VEC3
            value = self._read(packet, keys)
            return [] if value is None else [(quantity, slot, value)]

        if 0x10 <= type_id <= 0x17:
            # Super packet: several slots concatenated, each under its own
            # prefixed field names.
            out = []
            for slot, fields in SLOT_FIELDS.items():
                quantity = quantity_of_slot(slot)
                if quantity is None:
                    continue
                value = self._read(packet, fields)
                if value is not None:
                    out.append((quantity, slot, value))
            return out

        return []

    @staticmethod
    def _read(packet: dict, keys) -> tuple | None:
        """Pull a field group out of a packet, or None if it is not all there."""
        out = []
        for k in keys:
            v = packet.get(k)
            if v is None:
                return None
            out.append(float(v))
        return tuple(out)

    def _offer(self, quantity: str, value: tuple, t_us: int, slot: int,
               bundled: bool) -> bool:
        """
        Store a sample unless a better source already owns the quantity.

        Redundancy is normal, not exceptional: an ESP configured with a super
        slot *and* the same sensors as simple slots delivers every quantity
        twice.  Left alone that produces two model ticks per period, with a
        wildly irregular dt — which every rate and every envelope then reads as
        real.  So one source owns a quantity, and the others are dropped.

        Ranking, best first:
          1. the sensor the preference table names first (RV over GEO_RV,
             GAME_RV over ARVR_RV — a quality judgement about the sensor)
          2. bundled over standalone, since only a bundle guarantees that
             attitude and angular velocity were sampled at the same instant

        An incumbent that has gone quiet for longer than the gap tolerance loses
        its claim, so unplugging the preferred sensor mid-session falls back
        instead of freezing.
        """
        current = self._samples.get(quantity)
        new_key = (slot, bundled)

        if current is not None and current.key != new_key:
            better = _rank(new_key) < _rank(current.key)
            if not better and (t_us - current.t_us) < self._source_timeout_us:
                return False
        self._samples[quantity] = _Sample(value, t_us, slot, self._bundle, bundled)
        return True

    # ── Reading ──────────────────────────────────────────────────────────────

    def get(self, quantity: str) -> tuple | None:
        s = self._samples.get(quantity)
        return s.value if s else None

    def has(self, quantity: str) -> bool:
        return quantity in self._samples

    def present(self, now_us: int | None = None) -> frozenset[str]:
        """
        Quantities currently arriving.

        With `now_us`, stale ones are excluded: switching a sensor off must make
        the signals that need it go unavailable, not leave them reporting the
        last value they ever saw as though nothing had happened.  Without it,
        everything seen since the last reset counts — the answer before any tick
        has established a time.
        """
        if now_us is None:
            return frozenset(self._samples)
        return frozenset(
            q for q, s in self._samples.items()
            if now_us - s.t_us <= self._presence_timeout_us
        )

    def master(self) -> str | None:
        """
        The quantity whose arrival drives a model tick.

        Attitude is the root of every geometric signal, so the model runs at the
        attitude rate. The relative one is preferred: it is the source that is
        always trustworthy, whereas the absolute one can be magnetically
        disturbed — the tick rate should not depend on the magnetic environment.
        """
        if ATTITUDE_REL in self._samples:
            return ATTITUDE_REL
        if ATTITUDE_ABS in self._samples:
            return ATTITUDE_ABS
        return None

    def staleness(self, t_us: int) -> dict[str, int]:
        """Age in µs of each quantity relative to the tick instant."""
        return {q: t_us - s.t_us for q, s in self._samples.items()}

    def bundled_with(self, quantity: str) -> frozenset[str]:
        """Quantities that arrived in the same datagram as `quantity`."""
        ref = self._samples.get(quantity)
        if ref is None:
            return frozenset()
        return frozenset(
            q for q, s in self._samples.items() if s.bundle == ref.bundle
        )

    # ── Which sources are in play ────────────────────────────────────────────

    def sources(self, now_us: int | None = None) -> dict[str, str]:
        """Quantity → the sensor name actually supplying it right now."""
        live = self.present(now_us)
        return {
            q: SLOT_NAME[s.slot] if s.slot < len(SLOT_NAME) else f"slot{s.slot}"
            for q, s in sorted(self._samples.items()) if q in live
        }

    def sources_changed(self) -> bool:
        """
        True once when the resolved source set changes.

        Worth surfacing: it means the numbers a downstream consumer is reading
        just started coming from a different sensor, which is the kind of thing
        that otherwise shows up as an unexplained jump mid-take.
        """
        key = tuple(sorted((q, s.slot) for q, s in self._samples.items()))
        if key != self._sources_key:
            self._sources_key = key
            return True
        return False


def configured_quantities(esp_state: dict | None) -> dict[str, str]:
    """
    Quantities the ESP is *configured* to send, from the last CFG_ACK.

    Distinct from what the resolver has actually seen: "not configured" and
    "configured but not arriving" are different problems with different fixes,
    and the panel should be able to say which one it is.
    """
    if not esp_state:
        return {}

    found: dict[str, tuple[int, int]] = {}    # quantity -> (preference, slot)

    def offer(slot: int) -> None:
        entry = _SLOT_QUANTITY.get(slot)
        if entry is None:
            return
        quantity, pref = entry
        best = found.get(quantity)
        if best is None or pref < best[0]:
            found[quantity] = (pref, slot)

    enabled_slots = {
        s["slot"] for s in esp_state.get("simples", []) if s.get("enabled")
    }
    for slot in enabled_slots:
        offer(slot)

    # A super slot only carries deps whose simple slot is itself enabled — the
    # firmware samples through the simple slot's report.
    for sup in esp_state.get("supers", []):
        if not sup.get("active"):
            continue
        for slot in sup.get("deps", []) or []:
            if slot in enabled_slots:
                offer(slot)

    return {
        q: SLOT_NAME[slot] if slot < len(SLOT_NAME) else f"slot{slot}"
        for q, (_, slot) in sorted(found.items())
    }


def bundled_quantities(esp_state: dict | None) -> list[frozenset[str]]:
    """Quantity groups the ESP delivers in a single datagram, per active super."""
    if not esp_state:
        return []
    groups = []
    for sup in esp_state.get("supers", []):
        if not sup.get("active"):
            continue
        qs = {quantity_of_slot(s) for s in (sup.get("deps") or [])}
        qs.discard(None)
        if len(qs) > 1:
            groups.append(frozenset(qs))
    return groups


def slots_for(quantity: str) -> list[str]:
    """Sensor names that can supply a quantity, best first — used in error text."""
    candidates = sorted(
        ((pref, slot) for slot, (q, pref) in _SLOT_QUANTITY.items() if q == quantity)
    )
    return [SLOT_NAME[slot] for _, slot in candidates]
