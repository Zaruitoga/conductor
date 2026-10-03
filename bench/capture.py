"""
bench/capture.py — Capture hôte d'une passe de banc (#80, carte #49).

    python3 -m bench.capture --label temoin                 # 5 min, config du spectacle
    python3 -m bench.capture --esp 127.0.0.1 --config-port 4311 --duree 20 --no-prompt
                                                            # contre `python3 -m simulator`

Remplace l'orchestrateur sur les ports 4210/4211 le temps d'une passe : il
configure l'ESP lui-même, horodate chaque datagramme et écrit un fichier daté
dont l'en-tête **situe** la passe. Il n'analyse rien — c'est `bench.analyse`,
pour que le fichier reste la seule chose qu'une passe produit et qu'une analyse
corrigée puisse être relancée sur les vingt passes de la campagne.

Trois propriétés tenues ici, et qui ne se devinent pas :

- **Le fil se décode avec `transport/protocol.py`, jamais avec une copie.** Un
  banc qui recopie les dispositions `struct` peut mesurer un défaut qui n'est
  que sa copie — la règle que `simulator/wire.py` applique déjà.
- **Aucune horloge ajustable.** L'arrivée est datée en `time.monotonic_ns()`,
  pas en `time.time_ns()` comme `ts_rx_us` (#74 : murale, corrigée par `timed`).
- **Deux dates d'arrivée par datagramme** sur macOS : celle où Python l'a lu, et
  celle où le noyau l'a reçu (`SO_TIMESTAMP_MONOTONIC`, même horloge). Leur
  écart est le retard que *l'hôte* a ajouté — le discriminant « le noyau l'a
  reçu tard / Python l'a lu tard » que #68 attendait d'une capture pcap, gratuit
  ici, et décisif sur une machine souvent saturée.

Format (texte, une ligne par enregistrement, préfixe = nature) :

    #conductor-bench 1
    #header {json}
    U,<t_user_ns>,<t_kern_ns>,<type_id>,<seq>,<ts_esp_us>,<size>
    H,<t_user_ns>,<t_kern_ns>,<seq>,<ts_esp_us>,<uptime_ms>,<packets_sent>,<udp_errors>,<rssi_dbm>,<cpu_temp_c>,<battery_pct>
    #footer {json}

`t_kern_ns` est vide quand le noyau ne date pas. #81 ajoutera ses lignes série
(`S,…`, estampillées `micros()`, donc sur l'horloge de `ts_esp_us`) dans ce même
fichier : la jointure est le but, deux fichiers la reporteraient sur chaque passe.
"""

import argparse
import datetime as dt
import json
import os
import platform
import socket
import struct
import subprocess
import sys
import time

import config
from transport import protocol
from transport.esp_configurator import EspConfigurator

FORMAT_VERSION = 1
RUNS_DIR = config.data_path("bench_runs")

# Charge du spectacle (#63, #80) : GYRO + GAME_RV à 100 Hz, super 0 = [GYRO, GAME_RV]
# — la configuration de démarrage que `simulator/esp32.py` imite.
DEFAULT_RATE_HZ = 100.0
DEFAULT_SIMPLES = "0,6"
DEFAULT_SUPER0  = "0,6"

# 4 Mo : la moitié du plafond `kern.ipc.maxsockbuf` de cette machine (8 Mo).
# Le banc doit mesurer la perte de l'air, pas celle de son propre tampon (#76).
RCVBUF_BYTES = 4 * 1024 * 1024

# Darwin, <sys/socket.h> — absents du module `socket` de Python.
_SO_TIMESTAMP_MONOTONIC  = 0x0800
_SCM_TIMESTAMP_MONOTONIC = 0x04

_AIRPORT = ("/System/Library/PrivateFrameworks/Apple80211.framework/"
            "Versions/Current/Resources/airport")

_MANUAL_FIELDS = (
    ("distance_ap_m", "Distance ESP ↔ AP (m)"),
    ("alimentation",  "Alimentation de l'ESP (usb / batterie)"),
    ("placement",     "Position et orientation de la carte"),
    ("temperature_c", "Température ambiante (°C)"),
    ("note",          "Note libre"),
)


# ── Situer la passe ──────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: float = 30.0) -> tuple[str | None, str | None]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"{type(e).__name__}: {e}"
    if out.returncode != 0:
        return None, f"code {out.returncode}: {out.stderr.strip()[:200]}"
    return out.stdout, None


def rf_snapshot() -> dict:
    """L'instantané RF gratuit, sans `sudo` : il ne répond à rien, il *date* la passe.

    `airport` est supprimé par Apple sur les macOS récents : son absence est
    consignée telle quelle, pour la reprise dans six mois.
    """
    snap: dict = {"at_monotonic_ns": time.monotonic_ns()}
    out, err = _run([_AIRPORT, "-I"], timeout=10)
    if out is None:
        snap["airport_error"] = err
    else:
        snap["airport"] = {
            k.strip(): v.strip()
            for k, _, v in (line.partition(":") for line in out.splitlines()) if _
        }
    out, err = _run(["system_profiler", "-json", "SPAirPortDataType"], timeout=60)
    if out is None:
        snap["system_profiler_error"] = err
    else:
        try:
            snap["system_profiler"] = json.loads(out)
        except ValueError as e:
            snap["system_profiler_error"] = f"JSON illisible : {e}"
    return snap


def host_info() -> dict:
    git, _ = _run(["git", "-C", os.path.dirname(os.path.dirname(__file__)),
                   "describe", "--always", "--dirty", "--tags"], timeout=5)
    return {
        "hostname":  socket.gethostname(),
        "platform":  platform.platform(),
        "machine":   platform.machine(),
        "python":    sys.version.split()[0],
        "conductor": git.strip() if git else None,
        "loadavg":   os.getloadavg(),
    }


def ask_manual(given: dict, prompt: bool) -> dict:
    """Ce qui ne s'automatise pas. Demandé à la console pour ne pas l'oublier."""
    out = dict(given)
    if prompt:
        print("\nConditions de la passe (Entrée pour laisser vide) :")
        for key, label in _MANUAL_FIELDS:
            if out.get(key) in (None, ""):
                out[key] = input(f"  {label} : ").strip()
    return {k: out.get(k) or None for k, _ in _MANUAL_FIELDS}


# ── Horloge du noyau ─────────────────────────────────────────────────────────

def _mach_timebase() -> tuple[int, int] | None:
    try:
        import ctypes
        import ctypes.util

        class _TB(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        lib = ctypes.CDLL(ctypes.util.find_library("c"))
        tb = _TB()
        if lib.mach_timebase_info(ctypes.byref(tb)) != 0 or not tb.denom:
            return None
        return tb.numer, tb.denom
    except Exception:
        return None


class KernelClock:
    """Date d'arrivée noyau, convertie dans le domaine de `time.monotonic_ns()`.

    Activée seulement après une **vérification** sur la boucle locale : la date
    noyau doit tomber entre l'envoi et la lecture. Une horloge supposée être la
    bonne et qui ne l'est pas produirait un « retard de l'hôte » aussi plausible
    que faux ; mieux vaut une colonne vide et la raison dans l'en-tête.
    """

    def __init__(self) -> None:
        self.enabled = False
        self.timebase: tuple[int, int] | None = None
        self.check: dict = {}

    def enable(self, sock: socket.socket, port: int) -> None:
        if sys.platform != "darwin":
            self.check = {"ok": False, "raison": "SO_TIMESTAMP_MONOTONIC est propre à Darwin"}
            return
        self.timebase = _mach_timebase()
        if self.timebase is None:
            self.check = {"ok": False, "raison": "mach_timebase_info indisponible"}
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, _SO_TIMESTAMP_MONOTONIC, 1)
        except OSError as e:
            self.check = {"ok": False, "raison": f"setsockopt : {e}"}
            return
        self.enabled = True
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        marker = b"conductor-bench-probe"
        try:
            sock.settimeout(1.0)
            t_send = time.monotonic_ns()
            probe.sendto(marker, ("127.0.0.1", port))
            # L'ESP peut déjà émettre vers nous (hôte restauré de sa NVS) :
            # ne juger que la sonde elle-même.
            while True:
                data, anc, _, _ = sock.recvmsg(2048, socket.CMSG_SPACE(8))
                t_user = time.monotonic_ns()
                if data == marker:
                    break
            t_kern = self.from_ancillary(anc)
        except OSError as e:
            self.enabled, self.check = False, {"ok": False, "raison": f"sonde : {e}"}
            return
        finally:
            probe.close()
        ok = t_kern is not None and t_send <= t_kern <= t_user
        self.enabled = ok
        self.check = {"ok": ok, "send_ns": t_send, "kern_ns": t_kern, "user_ns": t_user}
        if not ok:
            self.check["raison"] = "la date noyau ne tombe pas entre l'envoi et la lecture"

    def from_ancillary(self, anc: list) -> int | None:
        for level, kind, data in anc:
            if level == socket.SOL_SOCKET and kind == _SCM_TIMESTAMP_MONOTONIC and len(data) >= 8:
                ticks = struct.unpack_from("<Q", data)[0]
                numer, denom = self.timebase
                return ticks * numer // denom
        return None


# ── Configurer l'ESP ─────────────────────────────────────────────────────────

def _slots(text: str) -> list[int]:
    return [int(s) for s in text.split(",") if s.strip()]


def local_ip_towards(ip: str) -> str:
    """L'adresse locale par laquelle on joint l'ESP — pas celle qui va vers Internet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def apply_target(cfg: EspConfigurator, state: dict, simples: list[int],
                 super0: list[int], rate_us: int) -> list[str]:
    """Amène l'ESP sur la cible en n'envoyant que les différences.

    Chaque commande réécrit la NVS de l'ESP (`saveNVS`) : ne rien renvoyer qui
    soit déjà vrai. Retourne la liste des commandes envoyées, pour l'en-tête.
    """
    sent: list[str] = []
    by_slot = {s["slot"]: s for s in state["simples"]}
    for slot in range(8):
        cur = by_slot.get(slot, {"enabled": False, "rate_us": 0})
        needed = slot in simples or slot in super0
        want_on = slot in simples
        want_rate = rate_us if needed else cur["rate_us"]
        if cur["enabled"] != want_on or (needed and cur["rate_us"] != want_rate):
            if cfg.set_simple(slot, want_on, want_rate) is None:
                raise RuntimeError(f"pas d'ACK pour SET_SIMPLE slot={slot}")
            sent.append(f"SET_SIMPLE {slot} {'on' if want_on else 'off'} {want_rate}us")
    supers = {s["slot"]: s for s in state["supers"]}
    for slot in range(8):
        cur = supers.get(slot, {"active": False, "deps": [], "skip_ratio": 1})
        if slot == 0 and super0:
            if not cur["active"] or cur["deps"] != super0 or cur["skip_ratio"] != 1:
                if cfg.set_super(0, super0, 1) is None:
                    raise RuntimeError("pas d'ACK pour SET_SUPER 0")
                sent.append(f"SET_SUPER 0 {super0}")
        elif cur["active"]:
            if cfg.del_super(slot) is None:
                raise RuntimeError(f"pas d'ACK pour DEL_SUPER {slot}")
            sent.append(f"DEL_SUPER {slot}")
    return sent


# ── La passe ─────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python3 -m bench.capture",
                                description="Capture hôte d'une passe de banc (#80).")
    p.add_argument("--label", default="passe", help="suffixe du nom de fichier")
    p.add_argument("--duree", type=float, default=300.0, help="secondes enregistrées (défaut 300)")
    p.add_argument("--settle", type=float, default=3.0,
                   help="secondes écartées après la configuration (défaut 3)")
    p.add_argument("--esp", default=config.ESP_HOST, help="hôte de l'ESP (mDNS ou IPv4)")
    p.add_argument("--config-port", type=int, default=config.CONFIG_PORT)
    p.add_argument("--data-port", type=int, default=config.UDP_PORT)
    p.add_argument("--rate-hz", type=float, default=DEFAULT_RATE_HZ)
    p.add_argument("--simples", default=DEFAULT_SIMPLES,
                   help="slots simples émis, ex. '0,6' ; '' pour aucun")
    p.add_argument("--super0", default=DEFAULT_SUPER0,
                   help="dépendances du super-slot 0, ex. '0,6' ; '' pour aucun")
    p.add_argument("--no-config", action="store_true",
                   help="ne rien envoyer à l'ESP hormis SET_HOST (l'état est quand même relevé)")
    p.add_argument("--no-rf", action="store_true", help="sauter l'instantané RF")
    p.add_argument("--no-prompt", action="store_true", help="ne pas demander les conditions")
    for key, label in _MANUAL_FIELDS:
        p.add_argument(f"--{key.replace('_', '-')}", dest=key, default=None, help=label)
    p.add_argument("--out", default=RUNS_DIR, help="dossier de sortie")
    return p.parse_args()


def _open_data_socket(port: int) -> tuple[socket.socket, dict]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF_BYTES)
    except OSError:
        pass
    rcvbuf = {"asked": RCVBUF_BYTES,
              "effective": sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)}
    sock.bind((config.UDP_HOST, port))
    return sock, rcvbuf


def main() -> int:
    args = _parse_args()
    manual = ask_manual({k: getattr(args, k) for k, _ in _MANUAL_FIELDS}, not args.no_prompt)
    simples, super0 = _slots(args.simples), _slots(args.super0)
    rate_us = int(round(1e6 / args.rate_hz))

    try:
        sock, rcvbuf = _open_data_socket(args.data_port)
        cfg = EspConfigurator(args.esp, args.config_port, config.CONFIG_LOCAL_PORT, timeout=3.0)
        cfg.start()
    except OSError as e:
        print(f"Impossible d'ouvrir les ports {args.data_port}/{config.CONFIG_LOCAL_PORT} : {e}\n"
              "L'orchestrateur tourne-t-il ? Le banc le remplace, il ne cohabite pas avec lui.")
        return 2

    kclock = KernelClock()
    kclock.enable(sock, args.data_port)
    print(f"Tampon socket : {rcvbuf['effective']} o · "
          f"date noyau : {'oui' if kclock.enabled else 'non — ' + kclock.check.get('raison', '?')}")

    if cfg.resolve() is None and not args.esp.replace(".", "").isdigit():
        print(f"Résolution de {args.esp} impossible. Passer --esp <IPv4>.")
        return 2
    local_ip = local_ip_towards(cfg.esp_ip)

    rf_start = None if args.no_rf else rf_snapshot()
    if rf_start and "airport" in rf_start:
        a = rf_start["airport"]
        print(f"Hôte sur {a.get('SSID')} canal {a.get('channel')} · "
              f"RSSI {a.get('agrCtlRSSI')} dBm · bruit {a.get('agrCtlNoise')} dBm")

    ack_before = cfg.set_host(local_ip)
    if ack_before is None:
        print(f"Pas d'ACK de {cfg.esp_ip}:{args.config_port}. L'ESP est-il allumé et joignable ?")
        return 2
    commands = [] if args.no_config else apply_target(cfg, ack_before, simples, super0, rate_us)
    ack_after = cfg.get_state() or ack_before
    print(f"ESP {cfg.esp_ip} → {local_ip} · commandes : {commands or 'aucune (déjà conforme)'}")

    os.makedirs(args.out, exist_ok=True)
    started = dt.datetime.now().astimezone()
    path = os.path.join(args.out, f"{started:%Y-%m-%d_%H-%M-%S}_{args.label}.bench")

    header = {
        "format": FORMAT_VERSION,
        "ticket": 80,
        "label": args.label,
        "started_at": started.isoformat(timespec="seconds"),
        "anchor": {"monotonic_ns": time.monotonic_ns(), "time_ns": time.time_ns()},
        "duration_s": args.duree,
        "settle_s": args.settle,
        "host": host_info(),
        "esp": {"host": args.esp, "ip": cfg.esp_ip, "config_port": args.config_port,
                "local_ip": local_ip, "data_port": args.data_port},
        "target": None if args.no_config else
                  {"rate_us": rate_us, "simples": simples, "super0": super0},
        "commands": commands,
        "ack_before": ack_before,
        "ack": ack_after,
        "so_rcvbuf": rcvbuf,
        "kernel_ts": {"enabled": kclock.enabled, "timebase": kclock.timebase,
                      "check": kclock.check},
        "manual": manual,
        "rf": rf_start,
    }

    counts: dict[str, int] = {}
    invalid = foreign = 0
    ancsize = socket.CMSG_SPACE(8)
    reason = "durée atteinte"
    n_last, t_last_report = 0, time.monotonic()

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"#conductor-bench {FORMAT_VERSION}\n")
        f.write("#header " + json.dumps(header, ensure_ascii=False) + "\n")
        f.flush()

        # Écarter le transitoire de reconfiguration du BNO — *après* l'en-tête :
        # tout ce qui précède (git, sous-processus, disque) laisse le noyau
        # empiler des paquets, et le drain est ce qui vide ce retard-là.
        sock.settimeout(0.2)
        settle_end = time.monotonic_ns() + int(max(args.settle, 0.5) * 1e9)
        while time.monotonic_ns() < settle_end:
            try:
                sock.recv(2048)
            except socket.timeout:
                pass
        print(f"\nEnregistrement {args.duree:.0f} s → {path}\n(Ctrl-C arrête proprement)")
        t0 = time.monotonic_ns()
        end = t0 + int(args.duree * 1e9)
        try:
            while True:
                if time.monotonic_ns() >= end:
                    break
                try:
                    data, anc, _, addr = sock.recvmsg(2048, ancsize)
                except socket.timeout:
                    continue
                t_user = time.monotonic_ns()
                if addr[0] != cfg.esp_ip:
                    foreign += 1
                    continue
                t_kern = kclock.from_ancillary(anc) if kclock.enabled else None
                kern = "" if t_kern is None else str(t_kern)
                if len(data) < protocol.DATA_HEADER_SIZE:
                    invalid += 1
                    continue
                _, type_id, size, seq, ts_esp = protocol.DATA_HEADER.unpack_from(data)
                if size != len(data) or type_id not in protocol.TYPE_NAME:
                    invalid += 1
                    continue
                name = protocol.TYPE_NAME[type_id]
                counts[name] = counts.get(name, 0) + 1
                if type_id == protocol.HB_TYPE:
                    if len(data) != protocol.DATA_HEADER_SIZE + protocol.HEARTBEAT.size:
                        invalid += 1
                        continue
                    hb = protocol.HEARTBEAT.unpack_from(data, protocol.DATA_HEADER_SIZE)
                    f.write(f"H,{t_user},{kern},{seq},{ts_esp},{hb[0]},{hb[1]},{hb[2]},"
                            f"{hb[3]},{hb[4]:.2f},{hb[5]:.2f}\n")
                else:
                    f.write(f"U,{t_user},{kern},{type_id},{seq},{ts_esp},{size}\n")

                now = time.monotonic()
                if now - t_last_report >= 5.0:
                    total = sum(counts.values())
                    rates = " ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
                    print(f"  {(t_user - t0) / 1e9:6.0f} s · {(total - n_last) / (now - t_last_report):5.0f} paq/s · {rates}")
                    f.flush()
                    n_last, t_last_report = total, now
        except KeyboardInterrupt:
            reason = "interrompu (Ctrl-C)"

        footer = {
            "ended_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "anchor": {"monotonic_ns": time.monotonic_ns(), "time_ns": time.time_ns()},
            "reason": reason,
            "recorded_s": (time.monotonic_ns() - t0) / 1e9,
            "counts": counts,
            "invalid": invalid,
            "foreign": foreign,
            "loadavg": os.getloadavg(),
            "ack": cfg.get_state(),
        }
        f.write("#footer " + json.dumps(footer, ensure_ascii=False) + "\n")
        if not args.no_rf:
            # Après le pied minimal : un system_profiler lent ne doit pas coûter la passe.
            f.write("#rf_end " + json.dumps(rf_snapshot(), ensure_ascii=False) + "\n")

    cfg.stop()
    sock.close()
    print(f"\n{reason} · {sum(counts.values())} datagrammes · invalides {invalid} · "
          f"étrangers {foreign}\n→ python3 -m bench.analyse {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
