"""
bench/ — Instrument de banc de la carte #49 (lien WiFi de l'ESP32), ticket #80.

Temporaire par construction : retiré d'un `git rm` quand la campagne est finie.
Rien dans `core.py` ne l'apprend — pas de route, pas de lifespan, pas
d'abonnement au bus. Le port 4210 est le seul point de contact : **l'orchestrateur
ne tourne pas pendant une passe de banc, le script le remplace.**

    python3 -m bench.capture --label temoin            # une passe, 5 min
    python3 -m bench.analyse                           # analyse la dernière passe

Les passes vont dans `config.data_path("bench_runs")` : ce sont des données de
la machine, pas de la branche, au même titre que `sessions/` (gitignorées, et
visibles depuis un worktree).
"""
