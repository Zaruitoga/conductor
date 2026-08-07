# Naviguer dans un take : une piste de pose précalculée, le reste en mise en régime

Naviguer dans un take se fait par **deux mécanismes distincts**, et non par un seul cache :

- **Le balayage** lit une **piste de pose** précalculée, stockée à côté du take. Rien
  n'atteint le bus : ni frame, ni événement, ni OSC.
- **Le saut** (reprendre la lecture à un instant) passe par une **mise en régime** : le
  modèle est réalimenté sur quelques secondes de take avant l'instant visé, à fond de CPU
  et à vide, puis la lecture reprend par le chemin normal.

La lecture elle-même ne change pas : elle reste sur la queue, `processing_loop` et
`model.feed()`. Le cache ne nourrit jamais l'OSC.

## Pourquoi deux mécanismes plutôt qu'un

Parce que l'état du modèle à l'instant T ne se déduit pas de ses **valeurs** à l'instant T,
et parce que presque tout cet état s'oublie tout seul.

Mesuré sur le take 002 (58,9 s, 2942 ticks) : modèle alimenté depuis le rang 0 pour
référence, puis réalimenté seulement depuis N secondes avant l'instant visé.

| mise en régime | signaux à 0,1 % de la référence |
|---|---|
| 0,5 s | 14 sur 18 — **dont le quaternion entier, exactement** |
| 1 s | 16 sur 18 |
| 8 s | 17 sur 18 |
| jamais | **`pos_x`, `pos_y`** |

Ce n'est pas une chance, c'est la structure. L'attitude est **sans état** — elle sort du
paquet. Toute enveloppe s'écrit `ctx.alpha(tau)` et oublie donc exponentiellement ; la
fenêtre nécessaire vaut environ 5 τ, et τ est **déclaré** (`motion_tau_slow_s`, défaut
2,5 s), donc dérivable plutôt que deviné. Seule la position horizontale est une intégrale
de chemin, irrécupérable par construction.

D'où le partage : **on ne précalcule que ce qui ne s'oublie pas**, c'est-à-dire la pose.
Tout le reste coûte ~0,2 s de mise en régime, et arrive **toujours avec les paramètres
courants** — ce qu'un cache ne peut pas offrir.

C'est la raison décisive, plus que le coût. Le modèle tourne à **50–77× le temps réel**
(762 ms pour 58,9 s de take, mesuré sur les trois takes réels), donc un cache complet
serait abordable ; mais il mourrait à chaque mouvement de curseur dans l'onglet Signaux,
puisque tout sauf la pose dépend d'un paramètre réglable. Une piste de pose, elle, traverse
une séance de réglage sans jamais s'invalider.

## Ce que contient la piste de pose

Un fichier **à côté du take**, jamais dans `raw.csv` : `processing_loop` écrit le CSV
*avant* le modèle, précisément pour que l'enregistrement ne dépende jamais du modèle de
calcul du jour. `pos_x`/`pos_y` sont une sortie du modèle.

Un enregistrement par tick : `t`, le quaternion, `x`, `y`, `z`. La pose **résolue**, pas
les colonnes brutes — le CSV range un slot simple dans des colonnes anonymes (`qw…qz`) et
un super slot dans des colonnes nommées (`game_rv_qw`), si bien qu'un lecteur qui irait la
chercher lui-même devrait rejouer le travail de `QuantityResolver` et connaître les deux
dispositions. 15 min à 100 Hz font ~3,2 Mo.

Écrite par le calcul lancé à `stop()`, en tâche de fond ; à défaut, au premier ouverture du
take. **Un seul producteur** : capter les valeurs en direct pendant l'enregistrement
paraissait gratuit — le modèle les calcule déjà — mais rien ne réinitialise le modèle au
début d'un take, si bien que l'intégrateur y entre avec un décalage accumulé qu'un calcul
depuis le rang 0 n'a pas. Deux producteurs du même fichier, différant subtilement.

Elle est **diffusée au fil du calcul** : le balayage est vivant jusqu'à la limite atteinte.
À 50–77× le temps réel, une seconde de calcul achète une minute de take — le calcul
distance le curseur avant qu'on ait fini de le saisir.

## Deux détails qui se paient cher si on les rate

- **Le saut est isolé dans une instance privée.** La mise en régime fait tirer les
  détecteurs ; ces événements ne doivent pas atteindre le bus, sous peine d'envoyer une
  poignée de chocs dans Live à chaque saut. On chauffe un `Model(bus=None)` — mode que
  `Model.__init__` prévoit déjà explicitement « so a batch run can drive an isolated graph
  without disturbing the live model » — puis on le substitue. **`_event_id` doit être
  reporté** : il est monotone sur toute la vie du processus, `reset()` le préserve
  délibérément, et une instance neuve repart de zéro.
- **La position est amorcée depuis la piste.** La mise en régime ne peut pas reconstituer
  `pos_x`/`pos_y` ; sans amorçage, la roue se téléporte à l'instant précis où l'on appuie
  sur lecture, entre la position lue au balayage et celle où l'intégrateur a atterri. La
  piste **est** l'état que l'oubli exponentiel ne rend pas.

## Options écartées

- **Tout précalculer, frames comprises, et lire depuis le cache.** Licite (voir ADR 0003) et
  écartée pour l'invalidation : chaque réglage de paramètre tuerait le cache, dans
  l'onglet Signaux qui est exactement l'endroit où l'on règle.
- **Points de reprise de l'état** (`Model._states` sérialisé périodiquement). Rendrait le
  saut instantané, au prix d'un contrat de sérialisation que **tout signal futur** devrait
  honorer — alors qu'ajouter un signal est aujourd'hui un décorateur et `ctx.state[…] = …`.
  Un signal qui y rangerait un tableau numpy casserait la reprise en silence. Cher et
  permanent, pour 0,2 s.
- **Avance rapide depuis le rang 0.** Aucune architecture nouvelle, mais un coût
  proportionnel à la distance parcourue (~25 s pour 15 min à 100 Hz) là où la mise en
  régime est **constante**.
- **Ne rien précalculer et attendre.** Contredit le besoin : ouvrir un take et le balayer
  tout de suite.

## Conséquences

- **Un saut en arrière refait tirer les événements déjà tirés.** Attendu, pas un défaut :
  un événement dit « le modèle vient de détecter ceci », et rejouer un passage le
  redétecte. `Event.id` détecte la *perte*, pas la duplication — il n'a jamais eu à
  répondre à cette question, le live n'allant que vers l'avant.
- **Le balayage ne produit rien.** Ni frame, ni événement, ni OSC ; `LiveMonitor` et le
  scope ne voient rien. Publier une frame de balayage supposerait un `signals` vide ou
  périmé — exactement l'ambiguïté que `model/types.py` s'emploie à écarter — et un
  glissement vers l'arrière ferait reculer `frame.t`, ce que rien en aval n'attend.
- **La géométrie sort de la surface réglable.** `wheel_R_m`/`wheel_r_m` ne sont pas des
  réglages mais des mesures ; les laisser sur un curseur invaliderait la piste de pose au
  milieu d'une séance. Elles restent dans `config.py`, et sont **estampillées dans la
  piste** : mesuré, un écart de 5 % sur les deux rayons déplace de 5 % `pos_x/y/z`,
  `height_m`, `contact_offset_m` et les deux enveloppes de mouvement. C'est **l'échelle
  absolue** qui compte, pas le rapport R/r — seul `heading_deg` est sensible au rapport.
  L'estampille coûte seize octets et rend détectable le seul cas qui survit : un
  `config.py` modifié, et des pistes anciennes à l'ancienne échelle.
- **Le calcul de fond ne bloque pas la boucle.** Mesuré : un ticker à 100 Hz passe d'une
  p95 de 16 ms à 24 ms pendant un calcul lancé dans `asyncio.to_thread`, sans décrochage —
  CPython rend le GIL toutes les 5 ms. Pas besoin de sous-processus.
