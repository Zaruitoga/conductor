"""
tests/test_udp_receiver.py — What the host throws away, it counts (#76).

The receiver is the one place on the host where a packet can be lost before
`processing_loop` sees it, and a packet lost here digs exactly the same `seq`
hole as one lost in the air.  If that loss is only a log line, a measure of the
WiFi link reads the host's own failure as the installation's — so the property
checked here is that a full queue *counts*, and that the count reaches
`status.udp`, the only place anything reads it from.

The datagrams go through the real `parse_packet` (built with the simulator's
wire helpers), so a drop is exercised on the path a live packet takes, not on a
dict the test made up.  The socket buffer is checked on a real socket: what the
kernel keeps is the kernel's decision, and the contract is that we read it back
and never fail to start over it.
"""

import asyncio
import logging
import socket

import config
import core
from simulator.wire import build_vec3
from transport import protocol
from transport.udp_receiver import UDPReceiver, set_rcvbuf

GYRO = next(t for t, name in protocol.TYPE_NAME.items() if name == "gyro")
ADDR = ("10.0.0.42", 4210)


def _datagram(seq: int) -> bytes:
    return build_vec3(GYRO, seq, seq * 10_000, (0.0, 0.0, 1.0))


def _feed(receiver: UDPReceiver, n: int) -> None:
    for seq in range(n):
        receiver.datagram_received(_datagram(seq), ADDR)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def test_a_full_queue_counts_every_packet_it_drops():
    receiver = UDPReceiver(asyncio.Queue(maxsize=3))
    _feed(receiver, 10)

    assert receiver.queue.qsize() == 3
    assert receiver.stats["rx"] == 10
    assert receiver.stats["dropped"] == 7
    # A drop is neither a parse error nor a muted packet: three different
    # causes, three different gestures for whoever reads them.
    assert receiver.stats["errors"] == 0
    assert receiver.stats["muted"] == 0


def test_nothing_is_counted_while_the_queue_has_room():
    receiver = UDPReceiver(asyncio.Queue())
    _feed(receiver, 50)
    assert receiver.stats["dropped"] == 0
    assert receiver.queue.qsize() == 50


def test_a_flood_of_drops_warns_once_with_the_running_count():
    capture = _Capture()
    logger = logging.getLogger("udp_receiver")
    logger.addHandler(capture)
    try:
        receiver = UDPReceiver(asyncio.Queue(maxsize=1))
        _feed(receiver, 200)
    finally:
        logger.removeHandler(capture)

    drops = [r for r in capture.records if "Queue full" in r.getMessage()]
    # 199 drops in far less than DROP_LOG_INTERVAL_S: one line, not 199.
    assert receiver.stats["dropped"] == 199
    assert len(drops) == 1, len(drops)
    assert "1 since start" in drops[0].getMessage()


def test_the_drop_count_reaches_the_status():
    saved = core.udp_protocol
    try:
        receiver = UDPReceiver(asyncio.Queue(maxsize=2))
        receiver.rcvbuf = 123_456
        _feed(receiver, 5)
        core.udp_protocol = receiver
        udp = core.status_dict()["udp"]
    finally:
        core.udp_protocol = saved

    assert udp["dropped"] == 3
    assert udp["rcvbuf"] == 123_456


def test_the_status_reads_zero_before_the_receiver_exists():
    saved = core.udp_protocol
    try:
        core.udp_protocol = None
        udp = core.status_dict()["udp"]
    finally:
        core.udp_protocol = saved
    assert udp["dropped"] == 0
    assert udp["rcvbuf"] is None


def test_the_receive_buffer_is_raised_and_read_back():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        default = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        effective = set_rcvbuf(sock, config.UDP_RCVBUF)
        assert effective == sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        # The kernel may clamp or round, but never below what was in place.
        assert effective >= default
    finally:
        sock.close()


class _RefusingSocket:
    """A socket whose kernel refuses every enlargement, as macOS does above
    its ceiling (ENOBUFS) — the case that must never stop startup."""

    def __init__(self, current: int):
        self.current = current
        self.attempts: list[int] = []

    def getsockopt(self, level, opt):
        return self.current

    def setsockopt(self, level, opt, value):
        self.attempts.append(value)
        raise OSError(55, "No buffer space available")


def test_a_refused_buffer_falls_back_without_raising():
    sock = _RefusingSocket(current=786_896)
    assert set_rcvbuf(sock, 8 * 1024 * 1024) == 786_896
    # Halved until it reaches what was already there, then gives up.
    assert sock.attempts == [8 * 1024 * 1024, 4 * 1024 * 1024,
                             2 * 1024 * 1024, 1024 * 1024]


def test_an_unreadable_buffer_is_reported_as_unknown():
    class _Broken:
        def getsockopt(self, level, opt):
            raise OSError("nope")

    assert set_rcvbuf(_Broken(), config.UDP_RCVBUF) is None


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
