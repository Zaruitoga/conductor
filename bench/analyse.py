"""
bench/analyse.py — Analyse d'une passe de banc (#80, carte #49).

    python3 -m bench.analyse                 # la dernière passe de bench_runs/
    python3 -m bench.analyse <fichier.bench> [--seuil-ms 20]

Lit un fichier de `bench.capture`, imprime un rapport et écrit
`<fichier>.analyse.json` à côté — la forme que la passe 1 et la passe 20 de la
campagne partagent, pour se comparer sans refaire le calcul à la main.

Ce que l'analyse tient pour établi, et pourquoi :

- **Le retard est ancré sur la médiane, jamais sur le minimum.** C'est la mise en
  garde payée pendant la reconnaissance : l'ancrage sur `min(offset)` a fait
  d'une queue étroite un « σ ≈ 10 ms » qui n'existait pas.
- **La dérive est retirée avant l'ancrage.** Le quartz de l'ESP et celui de
  l'hôte divergent de quelques dizaines de ppm : sur cinq minutes, des
  millisecondes, qu'une médiane globale étalerait dans la queue. Une droite par
  les médianes de fenêtres de 5 s l'absorbe sans voir les épisodes (rares, donc
  sans poids sur une médiane). Sa pente est rapportée : c'est la dérive relative
  des deux horloges, une première lecture pour #65.
- **Un calage de la `loop()` de l'ESP ne se voit pas dans le retard.**
  `ts_esp_us` est posé à l'envoi (`_sendPacket`) : un paquet retenu dans la
  boucle part tard *et* estampillé tard, donc avec un retard normal. Il se voit
  dans l'**intervalle côté ESP** entre deux envois. Les deux familles sont donc
  détectées séparément : *retard* (après l'estampille — pile WiFi, air, AP,
  hôte) et *calage* (avant l'estampille — la boucle).
- **Le retard de l'hôte se lit directement**, quand le noyau date : écart entre
  la date noyau et la date de lecture, sur la même horloge, sans ancrage. Un
  épisode dont le retard *noyau* reste sous le seuil a été fabriqué par l'hôte.
"""

import argparse
import glob
import json
import os
import sys

import numpy as np

from bench.capture import RUNS_DIR
from transport import protocol

PCTS = (50, 95, 99, 99.9)


# ── Lecture ──────────────────────────────────────────────────────────────────

def load(path: str) -> dict:
    meta: dict = {}
    data_rows, hb_rows = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                tag, _, rest = line[1:].partition(" ")
                if rest.startswith("{"):
                    meta[tag] = json.loads(rest)
                continue
            parts = line.rstrip("\n").split(",")
            kern = int(parts[2]) if parts[2] else -1
            if parts[0] == "U":
                data_rows.append((int(parts[1]), kern, int(parts[3]), int(parts[4]),
                                  int(parts[5]), int(parts[6])))
            elif parts[0] == "H":
                hb_rows.append((int(parts[1]), kern, int(parts[3]), int(parts[4]),
                                int(parts[5]), int(parts[6]), int(parts[7]),
                                int(parts[8]), float(parts[9]), float(parts[10])))
    d = np.array(data_rows, dtype=np.int64).reshape(-1, 6)
    h = np.array(hb_rows, dtype=np.float64).reshape(-1, 10)
    return {
        "header": meta.get("header", {}), "footer": meta.get("footer"),
        "t_user": d[:, 0], "t_kern": d[:, 1], "type": d[:, 2], "seq": d[:, 3],
        "ts_esp": d[:, 4], "size": d[:, 5], "hb": h,
    }


def unwrap_u32(ts: np.ndarray) -> np.ndarray:
    """`ts_esp_us` est un uint32 qui reboucle toutes les 71 min 35 s."""
    if ts.size == 0:
        return ts.astype(np.int64)
    d = np.diff(ts.astype(np.int64))
    d[d < -(1 << 31)] += 1 << 32
    d[d > (1 << 31)] -= 1 << 32
    return np.concatenate(([0], np.cumsum(d))) + int(ts[0])


# ── Grandeurs ────────────────────────────────────────────────────────────────

def dist(x: np.ndarray, unit: float = 1e3) -> dict:
    """Distribution en ms (x en µs par défaut)."""
    if x.size == 0:
        return {"n": 0}
    out = {"n": int(x.size)}
    for p in PCTS:
        out[f"p{p:g}"] = round(float(np.percentile(x, p)) / unit, 3)
    out["max"] = round(float(x.max()) / unit, 3)
    out["min"] = round(float(x.min()) / unit, 3)
    out["frac_gt_5ms"] = round(float(np.mean(x > 5 * unit)), 5)
    out["frac_gt_20ms"] = round(float(np.mean(x > 20 * unit)), 5)
    return out


def detrended_delay(t_rx_ns: np.ndarray, esp_us: np.ndarray,
                    window_s: float) -> tuple[np.ndarray, float]:
    """Retard en µs, dérive retirée, ancré sur la médiane. Retourne (retard, ppm)."""
    t_us = (t_rx_ns - t_rx_ns[0]) / 1e3
    off = t_us - (esp_us - esp_us[0])
    edges = np.arange(0.0, t_us[-1] + window_s * 1e6, window_s * 1e6)
    idx = np.digitize(t_us, edges)
    centers, medians = [], []
    for k in np.unique(idx):
        m = idx == k
        if m.sum() >= 10:
            centers.append(np.median(t_us[m]))
            medians.append(np.median(off[m]))
    if len(centers) >= 2:
        slope, icpt = np.polyfit(centers, medians, 1)
    else:
        slope, icpt = 0.0, float(np.median(off))
    resid = off - (slope * t_us + icpt)
    return resid - np.median(resid), float(slope * 1e6)


def seq_report(seq: np.ndarray) -> dict:
    """Perte et désordre de `seq`. La perte se compte sur les numéros *uniques*,
    jamais dans l'ordre d'arrivée : une rafale qui ressort dans le désordre
    n'a rien perdu, et la compter comme un trou fabriquerait une perte."""
    if seq.size < 2:
        return {"recus": int(seq.size)}
    uniq = np.unique(seq)
    gaps = np.diff(uniq) - 1
    return {
        "recus": int(seq.size),
        "attendus": int(uniq[-1] - uniq[0] + 1),
        "perdus": int(gaps.sum()),
        "trous": int(np.sum(gaps > 0)),
        "plus_long_trou": int(gaps.max()) if gaps.size else 0,
        "desordres": int(np.sum(np.diff(seq) < 0)),
        "doublons": int(seq.size - uniq.size),
    }


def group(times_s: np.ndarray, join_s: float) -> list[tuple[int, int]]:
    """Indices (début, fin inclus) de grappes d'instants séparés de moins de join_s."""
    if times_s.size == 0:
        return []
    cuts = np.where(np.diff(times_s) > join_s)[0]
    starts = np.concatenate(([0], cuts + 1))
    ends = np.concatenate((cuts, [times_s.size - 1]))
    return list(zip(starts.tolist(), ends.tolist()))


def periodicity(starts_s: list[float]) -> dict:
    if len(starts_s) < 3:
        return {"n_intervalles": max(0, len(starts_s) - 1)}
    iv = np.diff(np.sort(starts_s))
    med = float(np.median(iv))
    q1, q3 = np.percentile(iv, [25, 75])
    near = lambda k: float(np.mean(np.abs(iv - k * med) <= 0.15 * k * med))
    return {
        "n_intervalles": int(iv.size),
        "mediane_s": round(med, 3),
        "iqr_s": [round(float(q1), 3), round(float(q3), 3)],
        "frac_a_15pct_de_la_mediane": round(near(1), 3),
        "frac_multiple_1_2_3": round(float(np.mean(
            np.min([np.abs(iv - k * med) / (k * med) for k in (1, 2, 3)], axis=0) <= 0.15)), 3),
    }


def dominant_period(times_s: np.ndarray, p_min: float = 1.0, p_max: float = 60.0) -> dict:
    """Période qui aligne le mieux des instants, par repliement de phase.

    Les intervalles successifs ne suffisent pas : un événement périodique
    entouré de petits épisodes rapproche tous les intervalles de zéro, et la
    médiane ne voit plus rien (la première passe réelle avait ses grands
    épisodes à 30 s d'écart, et une médiane des intervalles de 0,6 s).
    `R` est la longueur du vecteur moyen des phases (1 = tous en phase, ~1/√n
    au hasard). Les sous-multiples d'une vraie période alignent aussi bien
    qu'elle : on garde la **plus longue** période à 5 % du meilleur R.
    """
    n = times_s.size
    if n < 4:
        return {"n": int(n)}
    periods = np.arange(p_min, p_max, 0.01)
    ph = 2 * np.pi * times_s[None, :] / periods[:, None]
    R = np.hypot(np.cos(ph).mean(axis=1), np.sin(ph).mean(axis=1))
    best = periods[R >= 0.95 * R.max()].max()
    r = float(R[np.argmin(np.abs(periods - best))])
    phase = float((np.angle(np.exp(2j * np.pi * times_s / best).mean()) % (2 * np.pi))
                  / (2 * np.pi) * best)
    # Les multiples disent si un événement sur deux ou trois domine : un R
    # presque aussi haut à ×3 qu'à ×1 est un rythme long avec des répliques.
    multiples = {}
    for k in (2, 3):
        ph_k = 2 * np.pi * times_s / (k * best)
        multiples[f"x{k}"] = {"periode_s": round(float(k * best), 2),
                              "R": round(float(np.hypot(np.cos(ph_k).mean(), np.sin(ph_k).mean())), 3)}
    return {"n": int(n), "periode_s": round(float(best), 2), "R": round(r, 3),
            "multiples": multiples,
            "R_hasard": round(1 / np.sqrt(n), 3), "p_rayleigh": float(np.exp(-n * r * r)),
            "phase_s": round(phase, 2)}


# ── Analyse ──────────────────────────────────────────────────────────────────

def analyse(run: dict, seuil_ms: float, fenetre_s: float, join_ms: float,
            fort_ms: float = 150.0) -> dict:
    order = np.argsort(run["t_user"], kind="stable")
    t_user = run["t_user"][order]
    t_kern = run["t_kern"][order]
    types = run["type"][order]
    seq = run["seq"][order]
    esp = unwrap_u32(run["ts_esp"][order])
    has_kern = bool(t_kern.size) and bool(np.all(t_kern >= 0))
    thr = seuil_ms * 1e3

    delay_user, ppm = detrended_delay(t_user, esp, fenetre_s)
    delay_kern = detrended_delay(t_kern, esp, fenetre_s)[0] if has_kern else None
    host_lag = (t_user - t_kern) / 1e3 if has_kern else None

    duration_s = (t_user[-1] - t_user[0]) / 1e9 if t_user.size else 0.0
    report: dict = {
        "fichier": run.get("path"),
        "label": run["header"].get("label"),
        "started_at": run["header"].get("started_at"),
        "duree_s": round(duration_s, 2),
        "parametres": {"seuil_ms": seuil_ms, "fenetre_s": fenetre_s, "join_ms": join_ms},
        "derive_relative_ppm": round(ppm, 2),
        "date_noyau": has_kern,
        "retard_total": dist(delay_user),
        "retard_noyau": dist(delay_kern) if has_kern else None,
        "retard_hote": dist(host_lag) if has_kern else None,
        "flux": {},
    }

    # Par flux : débit, perte, retard, intervalle côté ESP.
    nominal_us = {}
    stalls = []
    for tid in np.unique(types):
        m = types == tid
        name = protocol.TYPE_NAME.get(int(tid), hex(int(tid)))
        by_seq = np.argsort(seq[m], kind="stable")
        # Intervalle d'envoi entre numéros *consécutifs* seulement : autour d'un
        # paquet perdu, l'intervalle double sans que la boucle ait calé.
        consecutive = np.diff(seq[m][by_seq]) == 1
        esp_iv_all = np.diff(esp[m][by_seq])
        esp_iv = esp_iv_all[consecutive]
        nominal = float(np.median(esp_iv)) if esp_iv.size else 0.0
        nominal_us[int(tid)] = nominal
        long_iv = np.where(consecutive & (esp_iv_all > nominal + thr))[0]
        for i in long_iv:
            stalls.append((float((esp[m][by_seq][i] - esp[0]) / 1e6), float(esp_iv_all[i]), name))
        report["flux"][name] = {
            "debit_hz": round(m.sum() / duration_s, 2) if duration_s else None,
            "intervalle_esp_ms": {"nominal": round(nominal / 1e3, 3),
                                  **{k: v for k, v in dist(esp_iv).items()
                                     if k in ("p99.9", "max")}},
            "seq": seq_report(seq[m]),
            "retard_total": dist(delay_user[m]),
        }

    # Épisodes de retard : grappes de paquets retardés, groupés par instant d'envoi.
    flagged = np.where(delay_user > thr)[0]
    send_s = (esp - esp[0]) / 1e6
    fl_order = flagged[np.argsort(send_s[flagged], kind="stable")]
    episodes = []
    for a, b in group(send_s[fl_order], join_ms / 1e3):
        idx = fl_order[a:b + 1]
        ep = {
            "debut_s": round(float(send_s[idx].min()), 3),
            "duree_ms": round(float(send_s[idx].max() - send_s[idx].min()) * 1e3, 1),
            "paquets": int(idx.size),
            "flux": sorted({protocol.TYPE_NAME.get(int(t), hex(int(t))) for t in types[idx]}),
            "pic_total_ms": round(float(delay_user[idx].max()) / 1e3, 1),
        }
        if has_kern:
            ep["pic_noyau_ms"] = round(float(delay_kern[idx].max()) / 1e3, 1)
            ep["pic_hote_ms"] = round(float(host_lag[idx].max()) / 1e3, 1)
            ep["origine"] = "hôte" if delay_kern[idx].max() <= thr else "avant l'hôte"
        episodes.append(ep)
    n_streams = len(report["flux"])
    report["episodes_retard"] = {
        "n": len(episodes),
        "par_minute": round(len(episodes) / (duration_s / 60), 2) if duration_s else None,
        "touchent_tous_les_flux": sum(len(e["flux"]) == n_streams for e in episodes),
        "origine_hote": sum(e.get("origine") == "hôte" for e in episodes) if has_kern else None,
        "duree_ms": dist(np.array([e["duree_ms"] for e in episodes]), unit=1.0)
                    if episodes else {"n": 0},
        "periodicite": periodicity([e["debut_s"] for e in episodes]),
        "periode_des_forts": {"seuil_ms": fort_ms, **dominant_period(np.array(
            [e["debut_s"] for e in episodes if e["pic_total_ms"] >= fort_ms]))},
        "liste": episodes,
    }

    # Calages côté ESP : intervalles d'envoi anormalement longs, groupés entre flux.
    stalls.sort()
    st_groups = group(np.array([s[0] for s in stalls]), join_ms / 1e3)
    stall_eps = [{
        "debut_s": round(stalls[a][0], 3),
        "pire_intervalle_ms": round(max(s[1] for s in stalls[a:b + 1]) / 1e3, 1),
        "flux": sorted({s[2] for s in stalls[a:b + 1]}),
    } for a, b in st_groups]
    report["calages_esp"] = {
        "n": len(stall_eps),
        "periodicite": periodicity([e["debut_s"] for e in stall_eps]),
        "liste": stall_eps,
    }

    report["heartbeat"] = heartbeat_report(run["hb"], run["ts_esp"])
    report["erreurs_esp_par_intervalle"] = errors_vs_delay(run["hb"], run["ts_esp"][order],
                                                           delay_user, int(esp[0]))
    report["hote"] = {
        "loadavg_debut": run["header"].get("host", {}).get("loadavg"),
        "loadavg_fin": (run["footer"] or {}).get("loadavg"),
        "so_rcvbuf": run["header"].get("so_rcvbuf"),
    }
    return report


def heartbeat_report(hb: np.ndarray, ts_esp: np.ndarray) -> dict:
    """La vérité terrain de l'ESP, sous les deux sémantiques de `packets_sent`.

    Avant #82, le compteur comptait les *tentatives*, heartbeats compris ; après,
    les seuls paquets de données envoyés. Le fichier ne dit pas quel firmware
    tournait : les deux lectures sont données, celle qui s'applique est celle du
    firmware flashé.
    """
    if hb.shape[0] < 2:
        return {"n": int(hb.shape[0])}
    a, b = hb[0], hb[-1]
    ts_a, ts_b = int(a[3]), int(b[3])
    span = (ts_b - ts_a) % (1 << 32)
    rel = (ts_esp.astype(np.int64) - ts_a) % (1 << 32)
    received = int(np.sum(rel < span))
    d_sent, d_err = int(b[5] - a[5]), int(b[6] - a[6])
    n_hb = int(b[2] - a[2])
    return {
        "n": int(hb.shape[0]),
        "heartbeats_perdus": int(b[2] - a[2] + 1 - hb.shape[0]),
        "fenetre_s": round(span / 1e6, 2),
        "delta_packets_sent": d_sent,
        "delta_udp_errors": d_err,
        "donnees_recues": received,
        "ecart_firmware_avant_82": d_sent - n_hb - d_err - received,
        "ecart_firmware_apres_82": d_sent - received,
        "rssi_dbm": {"min": int(hb[:, 7].min()), "mediane": float(np.median(hb[:, 7])),
                     "max": int(hb[:, 7].max())},
        "cpu_temp_c": {"debut": float(a[8]), "fin": float(b[8])},
        "batterie_pct": {"debut": float(a[9]), "fin": float(b[9])},
    }


def errors_vs_delay(hb: np.ndarray, ts_esp: np.ndarray, delay: np.ndarray,
                    esp0: int) -> dict:
    """`udp_errors` n'arrive qu'au heartbeat (2 s) : on le confronte au retard
    des paquets envoyés dans le même intervalle. Une corrélation forte dit que
    l'ESP échoue à envoyer *pendant* les épisodes de retard — une même cause en
    amont de l'hôte, et non une perte dans l'air indépendante du retard."""
    if hb.shape[0] < 3:
        return {"n": int(hb.shape[0])}
    hts = hb[:, 3].astype(np.int64)
    d_err, d_max, t_end = [], [], []
    for i in range(hb.shape[0] - 1):
        span = (int(hts[i + 1]) - int(hts[i])) % (1 << 32)
        m = ((ts_esp.astype(np.int64) - int(hts[i])) % (1 << 32)) < span
        if not m.any():
            continue
        d_err.append(hb[i + 1, 6] - hb[i, 6])
        d_max.append(delay[m].max() / 1e3)
        t_end.append(((int(hts[i + 1]) - esp0) % (1 << 32)) / 1e6)
    d_err, d_max = np.array(d_err), np.array(d_max)
    corr = (float(np.corrcoef(d_err, d_max)[0, 1])
            if d_err.std() > 0 and d_max.std() > 0 else None)
    hit = np.where(d_err > 0)[0]
    return {
        "intervalles": int(d_err.size),
        "avec_erreurs": int(hit.size),
        "corr_erreurs_retard_max": None if corr is None else round(corr, 3),
        "liste": [{"fin_s": round(float(t_end[i]), 1), "erreurs": int(d_err[i]),
                   "retard_max_ms": round(float(d_max[i]), 1)} for i in hit],
    }


# ── Rapport ──────────────────────────────────────────────────────────────────

def _line(name: str, d: dict | None) -> str:
    if not d or not d.get("n"):
        return f"  {name:<14} —"
    return (f"  {name:<14} p50 {d['p50']:7.2f} · p95 {d['p95']:7.2f} · p99 {d['p99']:7.2f}"
            f" · p99,9 {d['p99.9']:7.2f} · max {d['max']:7.2f} ms · >20 ms {d['frac_gt_20ms']:.2%}")


def print_report(r: dict) -> None:
    print(f"\n═══ {r['label']} · {r['started_at']} · {r['duree_s']} s")
    print(f"Dérive relative ESP/hôte : {r['derive_relative_ppm']} ppm · "
          f"date noyau : {'oui' if r['date_noyau'] else 'non'}")
    print("\nRetard (dérive retirée, ancré sur la médiane) :")
    print(_line("total", r["retard_total"]))
    print(_line("à l'arrivée", r["retard_noyau"]))
    print(_line("ajouté hôte", r["retard_hote"]))
    print("\nFlux :")
    for name, f in r["flux"].items():
        s = f["seq"]
        print(f"  {name:<10} {f['debit_hz']:7.2f} Hz · perdus {s.get('perdus', 0)}/{s.get('attendus', s['recus'])}"
              f" · désordres {s.get('desordres', 0)} · intervalle ESP nominal "
              f"{f['intervalle_esp_ms']['nominal']} ms, max {f['intervalle_esp_ms'].get('max')} ms")
    e = r["episodes_retard"]
    p = e["periodicite"]
    print(f"\nÉpisodes de retard (> {r['parametres']['seuil_ms']} ms) : {e['n']} "
          f"({e['par_minute']}/min) · touchent tous les flux : {e['touchent_tous_les_flux']}"
          + (f" · nés dans l'hôte : {e['origine_hote']}" if e["origine_hote"] is not None else ""))
    if p.get("mediane_s"):
        print(f"  périodicité : médiane {p['mediane_s']} s, IQR {p['iqr_s']}, "
              f"{p['frac_a_15pct_de_la_mediane']:.0%} à ±15 % de la médiane, "
              f"{p['frac_multiple_1_2_3']:.0%} à ±15 % d'un multiple ×1–3")
    pf = e["periode_des_forts"]
    if pf.get("periode_s"):
        print(f"  épisodes ≥ {pf['seuil_ms']:g} ms : {pf['n']}, période dominante {pf['periode_s']} s "
              f"(R {pf['R']} contre ~{pf['R_hasard']} au hasard, p {pf['p_rayleigh']:.1e}, "
              f"phase {pf['phase_s']} s) · à ×2 : R {pf['multiples']['x2']['R']}"
              f" · à ×3 : R {pf['multiples']['x3']['R']}")
    c = r["calages_esp"]
    print(f"Calages côté ESP (intervalle d'envoi > nominal + seuil) : {c['n']}"
          + (f" · périodicité médiane {c['periodicite']['mediane_s']} s"
             if c["periodicite"].get("mediane_s") else ""))
    h = r["heartbeat"]
    if h.get("n", 0) >= 2:
        print(f"\nHeartbeat ({h['n']}, {h['heartbeats_perdus']} perdus) : Δpackets_sent "
              f"{h['delta_packets_sent']} · Δudp_errors {h['delta_udp_errors']} · données reçues "
              f"{h['donnees_recues']}\n  écart envoyés − reçus : {h['ecart_firmware_avant_82']} "
              f"(firmware avant #82) / {h['ecart_firmware_apres_82']} (après #82)\n"
              f"  RSSI {h['rssi_dbm']['min']}…{h['rssi_dbm']['max']} dBm · "
              f"CPU {h['cpu_temp_c']['debut']:.1f}→{h['cpu_temp_c']['fin']:.1f} °C")
    ev = r["erreurs_esp_par_intervalle"]
    if ev.get("intervalles"):
        print(f"Échecs d'envoi ESP : {ev['avec_erreurs']}/{ev['intervalles']} intervalles de "
              f"heartbeat touchés · corrélation avec le retard max de l'intervalle : "
              f"{ev['corr_erreurs_retard_max']}")
    lo = r["hote"]
    print(f"Charge hôte : {lo['loadavg_debut']} → {lo['loadavg_fin']}")


def main() -> int:
    p = argparse.ArgumentParser(prog="python3 -m bench.analyse")
    p.add_argument("path", nargs="?", help="fichier .bench (défaut : le plus récent)")
    p.add_argument("--seuil-ms", type=float, default=20.0)
    p.add_argument("--fenetre-s", type=float, default=5.0)
    p.add_argument("--join-ms", type=float, default=100.0)
    p.add_argument("--fort-ms", type=float, default=150.0,
                   help="seuil des épisodes dont on cherche la période dominante")
    p.add_argument("--episodes", action="store_true", help="imprimer la liste des épisodes")
    args = p.parse_args()

    path = args.path
    if path is None:
        runs = sorted(glob.glob(os.path.join(RUNS_DIR, "*.bench")))
        if not runs:
            print(f"Aucune passe dans {RUNS_DIR}")
            return 1
        path = runs[-1]
    run = load(path)
    run["path"] = os.path.abspath(path)
    if run["t_user"].size < 100:
        print(f"{path} : {run['t_user'].size} paquets, trop peu pour une distribution.")
        return 1
    report = analyse(run, args.seuil_ms, args.fenetre_s, args.join_ms, args.fort_ms)
    print_report(report)
    if args.episodes:
        for ep in report["episodes_retard"]["liste"]:
            print("  ", ep)
    out = path + ".analyse.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
