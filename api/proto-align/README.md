# PROTOTYPE JETABLE — interface d'alignement (issue #9)

> Trois mises en page de l'interface d'alignement vidéo/IMU, sur la même page,
> commutées par `?variant=`. **Code jetable** : aucune validation de chemin, aucun
> test, aucune persistance. Il sert à réagir, pas à livrer la page.

```bash
python3 api/proto-align/server.py     # → http://127.0.0.1:8077/
```

Le serveur trouve `sessions/` en remontant l'arborescence (il est gitignoré, donc
absent d'un worktree) ; sinon `PROTO_SESSIONS=<chemin>`.

**À ouvrir dans Chrome ou Safari**, pas dans un navigateur intégré :
`requestVideoFrameCallback` n'y rappelle jamais, et la page bascule alors sur un
repli `currentTime` qu'elle signale en bandeau — le geste frame par frame n'y est
pas jugeable.

## Ce qui est vrai dans cette maquette

- Les vraies vidéos et les vrais `raw.csv` de `sessions/` (endpoint `Range` via
  `FileResponse`, cf. #5).
- La vraie détection de #7 (`|ω|` brute, silence < 0,5 rad/s pendant ≥ 2 s),
  recalculée à la volée côté serveur.
- Le vrai pas-à-pas de #4 : on vise, on lit le `mediaTime` rapporté, on renudge
  (boucle fermée, le nombre d'aller-retours est affiché).
- La cadence du fichier est **mesurée** en pause, en poussant le seek de 8 ms
  jusqu'à ce que le `mediaTime` change.

Ce qui est faux exprès : l'alignement confirmé n'est gardé qu'**en mémoire** du
serveur (effacé au redémarrage) ; le take 001 n'a réellement pas de vidéo.

## Les trois variantes

| | Pari | Carte des touches |
|---|---|---|
| **A — Table de montage** | La vidéo *est* le travail ; la courbe est un ruban qu'on regarde du coin de l'œil. Aucune étape. | `← →` frame · `⇧← ⇧→` 10 frames · `↑ ↓` 1 s · `Espace` lecture · `Entrée` confirmer · clic sur le ruban = ancre IMU |
| **B — Deux ancres** | Vérifier la proposition IMU est une vraie étape. La courbe mérite la moitié de l'écran, et la corriger se fait sur place. | `← →` frame · `⇧← ⇧→` 1 s · `↑ ↓` échantillon IMU · `A` poser l'ancre vidéo · `+ −` zoom · `Entrée` confirmer |
| **C — Assistant + roue** | La page a un ordre et devrait le dire. Et la roue 3D est là pour qu'on tranche en la voyant. | `← →` selon l'étape · `⇧` ×10 · `Entrée` étape suivante · `⌫` revenir |

Le commutateur n'est **pas** sur `← →` : ces touches sont exactement l'objet du
test. Il est en haut, hors du design évalué.

## Les états dégradés

- **sans vidéo** — réel : sélectionner le take 001.
- **`&broken=onset`** — la détection ne propose rien (le take 001 le fait aussi
  pour de vrai : trop court pour contenir 2 s de silence suivies d'un geste).
- **`&broken=unreadable`** — le fichier ne se décode pas.

## Ce qu'il faut en tirer

Les questions posées par #9 : la mise en page, la carte des touches, ce qu'on
montre quand ça manque, et si la roue 3D a sa place. Le retour utile est du genre
« la courbe de B avec le plein écran de A ».
