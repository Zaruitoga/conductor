"""
tests/test_csv.py — The CSV schema: one quantity, one column.

The take on disk is the only thing the whole `storage/` half rests on — the
replay, the pose track, the onset curve and every future tool read it — and
until issue #12 nothing asserted the property that makes it readable at all:

  * a quantity lands in the *same columns* whatever slot delivered it, so a
    gyro sent on its own and a gyro bundled in a super slot are indistinguish-
    able to a reader.  Before, a simple slot filed its vector under `x,y,z`
    and a super slot under `gyro_x…`, and every reader had to know both;
  * a packet written then read back is the same packet — values, components in
    order, and the stream identity `type_id` carries;
  * telemetry stays out: a heartbeat is not sensor data and writes no row.

None of the existing modules would have gone red if simple slots quietly got
their own columns back, which is what this one is for.  It drives the real
`CSVLogger` and the real `row_to_packet`: a test that wrote its own header
would be asserting against a second implementation of the thing under test.
"""

import csv
import os
import shutil
import tempfile

from simulator.wire import build_quat, build_super, build_vec3
from storage.csv_logger import CSV_FIELDS, CSVLogger
from storage.playback_engine import row_to_packet
from storage.session_manager import SessionManager
from transport.protocol import (
    ALL_SUPER_NAMED_FIELDS, PACKET_FIELDS, SLOT_FIELDS, TYPE_NAME, parse_packet,
)
from transport.super_layout import SuperSlotLayout

_TMP_DIRS: list[str] = []

GYRO   = (0.25, -1.5, 3.0)
QUAT   = (0.5, 0.5, -0.5, 0.5)
TS     = 1_234_567


def _simple_gyro(seq=1):
    """The gyro on its own datagram — the reference session's configuration."""
    return {"type": "gyro", "typeId": 0x01, "seq": seq,
            "ts_esp_us": TS, "ts_rx_us": TS + 9,
            "gyro_x": GYRO[0], "gyro_y": GYRO[1], "gyro_z": GYRO[2]}


def _bundled_gyro(seq=1):
    """The same gyro, sampled beside an attitude inside super slot 0."""
    return {"type": "super_0", "typeId": 0x10, "seq": seq,
            "ts_esp_us": TS, "ts_rx_us": TS + 9, "dep_slots": [0, 6],
            "gyro_x": GYRO[0], "gyro_y": GYRO[1], "gyro_z": GYRO[2],
            "game_rv_qw": QUAT[0], "game_rv_qx": QUAT[1],
            "game_rv_qy": QUAT[2], "game_rv_qz": QUAT[3]}


def _heartbeat(seq=1):
    return {"type": "heartbeat", "typeId": 0x20, "seq": seq,
            "ts_esp_us": TS, "ts_rx_us": TS + 9, "uptime_ms": 4200,
            "packets_sent": 99, "udp_errors": 0, "rssi_dbm": -54,
            "cpu_temp_c": 41.5, "battery_pct": 87.0}


def _write(packets) -> str:
    """Record `packets` through the real logger; return the CSV's path."""
    root = tempfile.mkdtemp(prefix="conductor-csv-")
    _TMP_DIRS.append(root)
    sm = SessionManager(os.path.join(root, "sessions"))
    sm.create_session(title="bench")
    take_dir, meta = sm.new_take(title="essai")

    logger = CSVLogger(sm)
    logger.start(take_dir, meta)
    for p in packets:
        logger.write(p)
    logger.stop()
    return sm.csv_path(take_dir)


def _rows(path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _filled(row) -> dict:
    """The payload columns this row actually carries."""
    return {k: v for k, v in row.items()
            if k in ALL_SUPER_NAMED_FIELDS and v not in ("", None)}


# ── The invariant ───────────────────────────────────────────────────────────

def test_a_quantity_lands_in_the_same_columns_whatever_slot_delivered_it():
    """
    The whole of issue #12, stated once.  A gyro is `gyro_x/y/z` whether it
    arrived as a 0x01 packet or bundled in a super slot; the bundled row simply
    carries an attitude beside it.  Nothing about which columns hold what may
    depend on how the ESP happened to be configured.
    """
    simple  = _filled(_rows(_write([_simple_gyro()]))[0])
    bundled = _filled(_rows(_write([_bundled_gyro()]))[0])

    gyro_cols = set(SLOT_FIELDS[0])
    assert set(simple) == gyro_cols, \
        f"the simple slot filed its gyro under {sorted(simple)}"
    assert gyro_cols <= set(bundled), \
        f"the bundled gyro is missing from {sorted(bundled)}"
    assert {k: simple[k] for k in gyro_cols} == {k: bundled[k] for k in gyro_cols}, \
        "the same vector was written differently depending on its transport"


def test_the_schema_has_no_anonymous_columns_left():
    """
    The retired columns, named: a schema keeping `x` or `qw` would let a writer
    quietly resume filing simple slots apart, and every reader would have to
    start guessing again.  32 columns — four common, twenty-eight payload.
    """
    for retired in ("x", "y", "z", "qw", "qx", "qy", "qz"):
        assert retired not in CSV_FIELDS, f"{retired!r} is still a column"

    assert CSV_FIELDS == ["ts_rx_us", "seq", "ts_esp_us", "type_id",
                          *ALL_SUPER_NAMED_FIELDS]
    assert len(CSV_FIELDS) == 32, len(CSV_FIELDS)


def test_the_columns_are_the_packets_own_field_names():
    """
    There is no CSV-side table to keep in step with the parser's: a payload
    column *is* a packet field, and both sides read `protocol.PACKET_FIELDS`.
    A type whose fields fell outside the schema would be recorded blank.
    """
    for type_id, fields in PACKET_FIELDS.items():
        assert set(fields) <= set(CSV_FIELDS), \
            f"{TYPE_NAME[type_id]} names a field the CSV has no column for"


def test_the_wire_itself_names_the_quantity_it_carries():
    """
    Where the naming actually happens.  Everything above starts from a packet
    dict, so it would stay green if `parse_packet` went back to filing a 0x01
    payload under `x,y,z` — the file would follow, and the asymmetry would be
    back with nothing red.  So this one starts from bytes: the same three
    floats sent as a simple GYRO datagram and inside a super slot must come out
    of the parser under one set of names, which is what lets every layer above
    — the CSV included — have a single table.
    """
    layout = SuperSlotLayout()
    layout.update({"supers": [{"slot": 0, "active": True, "deps": [0, 6]}]})

    solo = parse_packet(build_vec3(0x01, 1, TS, GYRO))
    bundled = parse_packet(build_super(0, 2, TS, list(GYRO) + list(QUAT)), layout)
    attitude = parse_packet(build_quat(0x07, 3, TS, QUAT))

    for name, value in zip(SLOT_FIELDS[0], GYRO):
        assert abs(solo[name] - value) < 1e-6, f"the simple gyro has no {name}"
        assert abs(bundled[name] - value) < 1e-6, f"the bundled gyro has no {name}"
    for name, value in zip(SLOT_FIELDS[6], QUAT):
        assert abs(attitude[name] - value) < 1e-6, f"the attitude has no {name}"

    for retired in ("x", "y", "z", "qw", "qx", "qy", "qz"):
        assert retired not in solo and retired not in attitude, \
            f"the parser still files a payload under {retired!r}"


# ── The round trip ──────────────────────────────────────────────────────────

def test_a_packet_written_then_read_back_is_the_same_packet():
    """
    Values, component order, and the stream identity — for both shapes.  A
    transposition inside a quaternion is exactly the kind of defect a hash over
    an unordered payload would miss, so the comparison is per field, by name.
    """
    for original in (_simple_gyro(seq=7), _bundled_gyro(seq=7)):
        row = _rows(_write([original]))[0]
        back = row_to_packet(row)

        assert back is not None, "the row did not decode"
        assert back["typeId"] == original["typeId"]
        assert back["type"] == original["type"], \
            "the replayed stream lost the name LiveMonitor and ?types= use"
        assert back["seq"] == original["seq"]
        assert back["ts_esp_us"] == original["ts_esp_us"]
        assert back["ts_rx_us"] == original["ts_rx_us"]

        for field in PACKET_FIELDS[original["typeId"]]:
            if field in original:
                assert back[field] == original[field], f"{field} came back wrong"
            else:
                assert field not in back, \
                    f"{field} was not recorded but came back anyway"


def test_type_id_is_what_tells_the_two_rows_apart():
    """
    The columns no longer distinguish a simple row from a super one — which is
    the point — so `type_id` is the only record of which datagram it was, and
    a replay rebuilds the stream's name from it.  A super slot carrying only a
    gyro would be indistinguishable from a simple one without it.
    """
    rows = _rows(_write([_simple_gyro(seq=1), _bundled_gyro(seq=2)]))
    solo = {k: v for k, v in _filled(rows[0]).items() if k in SLOT_FIELDS[0]}
    both = {k: v for k, v in _filled(rows[1]).items() if k in SLOT_FIELDS[0]}

    assert solo == both, "the gyro columns should be indistinguishable"
    assert rows[0]["type_id"] != rows[1]["type_id"]
    assert row_to_packet(rows[0])["type"] == "gyro"
    assert row_to_packet(rows[1])["type"] == "super_0"


# ── What never reaches the file ─────────────────────────────────────────────

def test_a_heartbeat_writes_no_row():
    """
    Telemetry is not sensor data: it is absent from PACKET_FIELDS, which is the
    same membership that makes a row replayable — so it cannot be written on
    one side and skipped on the other, as two tables could drift into.
    """
    assert 0x20 not in PACKET_FIELDS
    rows = _rows(_write([_heartbeat(), _simple_gyro(seq=2)]))

    assert len(rows) == 1, "the heartbeat was recorded"
    assert rows[0]["type_id"] == "1"


def test_a_super_packet_arriving_before_the_layout_is_not_recorded():
    """
    Without the dep list there is nothing to name the floats *with* — the
    parser falls back to s0..sN — and a row of unnamed values would be a row no
    reader could ever place.  Dropping it keeps "a column says what it holds"
    true of every row in the file.
    """
    opaque = {"type": "super_0", "typeId": 0x10, "seq": 1,
              "ts_esp_us": TS, "ts_rx_us": TS, "dep_slots": None,
              "s0": 1.0, "s1": 2.0, "s2": 3.0}

    assert _rows(_write([opaque])) == []


def test_a_row_naming_a_type_that_is_not_recorded_is_skipped():
    """
    `row_to_packet` decides from PACKET_FIELDS, not from a type-name table of
    its own: a row carrying a heartbeat's id — hand-written, or from a version
    that recorded them — decodes to nothing rather than to a payload-less
    packet the model would tick on.
    """
    assert row_to_packet({"type_id": "32", "seq": "1",
                          "ts_esp_us": "1", "ts_rx_us": "1"}) is None
    assert row_to_packet({"type_id": "", "seq": "1",
                          "ts_esp_us": "1", "ts_rx_us": "1"}) is None


def main():
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print(f"  ok  {name}")
    finally:
        for d in _TMP_DIRS:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
