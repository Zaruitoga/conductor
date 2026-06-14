"""
transport/super_layout.py — Shared super-slot dependency state.

SuperSlotLayout is a lightweight shared-state object that maps super slot
indices to their component simple slot list.  It is written by
EspConfigurator (on every ACK) and read by protocol.parse_packet (on every
super datagram) so that the parser can emit properly named payload fields
instead of the opaque s0..sN fallback.

The field-name tables themselves (SLOT_FIELDS, SLOT_FLOAT_COUNT,
ALL_SUPER_NAMED_FIELDS) live in `transport/protocol.py` with the rest of the
wire format; this module holds only the mutable runtime state.
"""


class SuperSlotLayout:
    """
    Maps super slot indices to their component simple slot list.

    Written by EspConfigurator on every ACK, read by protocol.parse_packet on
    every super-slot datagram.  Dict operations are protected by the GIL, which
    is sufficient given that the writer runs in a ThreadPoolExecutor thread and
    the reader runs in the asyncio event loop thread.

    Until the first ACK is received, all get_deps() calls return None and
    parse_packet falls back to generic s{i} field naming.
    """

    def __init__(self):
        self._deps: dict[int, list[int]] = {}

    def update(self, state: dict) -> None:
        """Synchronise from a parsed ACK state dict."""
        for s in state.get("supers", []):
            if s["active"]:
                self._deps[s["slot"]] = list(s["deps"])
            else:
                self._deps.pop(s["slot"], None)

    def get_deps(self, super_idx: int) -> list[int] | None:
        """Return the dep_slots list for a super slot, or None if not yet known."""
        return self._deps.get(super_idx)
