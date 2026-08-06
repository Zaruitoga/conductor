# PROTOTYPE JETABLE — interface d'alignement (issue #9)

> Une seule maquette, née d'un grillage : A, B et C (trois variantes de mise en
> page) ont été rejetées en bloc parce qu'elles partageaient un **modèle de tâche
> faux**. **Code jetable** : aucune validation de chemin, aucun test, aucune
> persistance. Il sert à réagir, pas à livrer la page.

```bash
python3 api/proto-align/server.py     # → http://127.0.0.1:8077/
```

Le serveur trouve `sessions/` en remontant l'arborescence (il est gitignoré, donc
absent d'un worktree) ; sinon `PROTO_SESSIONS=<chemin>`.

**À ouvrir dans Chrome ou Safari**, pas dans un navigateur intégré :
`requestVideoFrameCallback` n'y rappelle jamais, et la page bascule alors sur un
repli `currentTime` qu'elle signale en bandeau — le geste frame par frame n'y est
pas jugeable.

## Ce que les trois premières variantes rataient

1. **Elles faisaient corriger à l'œil ce que le serveur savait déjà.** La règle de
   #7 produit **2 à 4 candidats par take** (11,34 / 19,68 / 28,45 / 35,35 s sur le
   take 002) ; elles n'en affichaient qu'un et laissaient cliquer n'importe où sur
   la courbe. La vraie question est un **choix entre quatre**, pas un pointage.
2. **Rien pour atteindre l'endroit intéressant.** Le pas le plus large était 1 s,
   sur une vidéo de 50 s.
3. **Rien pour voir bouger 3 px** — la résolution même sur laquelle #7 a calibré
   son seuil (17 mm par frame, ~3 px).

## Ce que D fait

**Une seule disposition**, du début à la fin ; la vérification s'ajoute en bas.

- **Côté IMU — choisir.** Tous les candidats sont marqués sur la courbe et listés
  en pastilles, chacun annoté de la **durée du repos qui le précède** (l'indice qui
  les départage). Le premier est retenu, `↑` `↓` passent au suivant.
- **Côté vidéo — arriver, puis voir.** Un scrubber traverse le take ; `←` `→`
  entrent **d'eux-mêmes** en mode détail (frame par frame), sans touche de mode à
  apprendre.
- **Comparateur à bascule.** `E` épingle une frame où la roue est au repos, `B`
  maintenu la flashe à la place de la courante. L'écart se **cumule depuis le
  repos**, donc un départ mou finit par crever l'œil — là où une comparaison
  N ↔ N-1 compare à une cible mobile.
- **Vérification.** Pas de deuxième réglette : une timeline en temps de take a
  été construite puis **retirée** — elle ne disait rien que la réglette vidéo ne
  dise déjà. Vérifier, c'est se promener avec la même réglette et regarder le
  **curseur de lecture courir sur la courbe**. Une seule question : *la détection
  a-t-elle désigné le bon départ ?* Si non, l'erreur est de plusieurs secondes :
  on change de candidat et on repose. Pas de correction fine, **pas de roue 3D**.
- **Les ancres se figent.** Une fois posées elles affichent la valeur
  enregistrée, verrouillée, et le bouton devient *Reposer sur la frame courante* —
  elles défilaient pendant la vérification, donc elles montraient autre chose que
  ce qui est stocké. Un marqueur vert sur la réglette montre l'ancre vidéo.

## Carte des touches

Les flèches se répartissent les deux axes : **horizontal = temps vidéo, vertical =
choix IMU.**

| | |
|---|---|
| `←` `→` | une frame *(entre en mode détail)* |
| `⇧←` `⇧→` | dix frames |
| `↑` `↓` | candidat IMU précédent / suivant |
| `E` | épingler la frame de repos |
| `B` *(maintenu)* | flasher l'épinglée |
| `Entrée` | confirmer l'alignement |
| `Espace` | lecture / pause |
| `Échap` | sortir du mode détail |

## Ce qui est vrai dans cette maquette

- Les vraies vidéos et les vrais `raw.csv` de `sessions/` (`Range` via
  `FileResponse`, cf. #5).
- La vraie règle de #7 (`|ω|` brute, silence < 0,5 rad/s pendant ≥ 2 s), rendue
  ici en **liste de candidats** plutôt qu'en proposition unique.
- Le vrai pas-à-pas de #4, corrigé : viser `media ± dt` sautait de 1 à 4 frames,
  parce que `dt` est une médiane et que la cadence n'est pas régulière. On part
  maintenant d'un décalage **trop petit** et on l'agrandit jusqu'à ce que le
  `mediaTime` change, puis on s'arrête — la première frame atteinte est forcément
  la voisine. `⇧` reste un saut approximatif, il sert à traverser.
- La cadence du fichier est **mesurée** en pause, en poussant le seek de 8 ms
  jusqu'à ce que le `mediaTime` change.

Faux exprès : l'alignement confirmé n'est gardé qu'**en mémoire** du serveur.

## Les états dégradés

- **`&broken=onset`** — aucun candidat.
- **`&broken=unreadable`** — le fichier ne se décode pas.
- **take 001** — ni vidéo, **ni flux gyro** (171 lignes, que du `GAME_RV`). Deux
  états distincts : « rien détecté » et « la méthode ne s'applique pas ».
