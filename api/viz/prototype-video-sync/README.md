# Prototype #8 — la vidéo dans le viz pendant la lecture

**Jetable.** Rien ici n'a vocation à être fusionné dans `main`. Le prototype
existe pour faire réagir devant quelque chose qui tourne, puis pour être capturé
comme source primaire sur sa branche (`proto/8-video-dans-le-viz`).

## La question

[Issue #8](https://github.com/Zaruitoga/conductor/issues/8) en noue deux :

1. **Mise en page** — comment cohabitent la scène Three.js et la vidéo ?
   Ça se tranche devant une maquette : d'où **trois variantes** commutables.
2. **Comportement** — la synchronisation de l'acquis n°7 tient-elle vraiment ?
   Ça ne se tranche qu'en la faisant tourner : d'où **un instrument** qui mesure
   au lieu de supposer.

Le ticket demande explicitement de mesurer et non de supposer : la dérive réelle
sur un take entier, le seuil de recalage qui corrige sans saccader, le
comportement aux vitesses extrêmes, et si le décodage vidéo dégrade le fps de la
scène ou le débit de la socket.

## Comment le lancer

```bash
python3 main.py
```

Puis <http://localhost:8000/viz/?variant=A>.

Les takes vivent dans `sessions/`, qui est **gitignoré et donc absent d'un
worktree**. Cette branche porte un lien symbolique vers le checkout principal ;
si le lien manque, passer le chemin en clair :

```bash
PROTO_SESSIONS_DIR=~/Desktop/IMU_project/conductor/sessions python3 main.py
```

Aucun ESP n'est nécessaire : tout part d'un take enregistré.

## Comment s'en servir

1. Choisir le take **002**, **003** ou **004** de la session `test-1` dans le
   bloc « Lecture » (ce sont les trois qui ont une vidéo à côté de leur CSV).
   La vidéo se précharge sans attendre le bouton *Lire*.
2. **Poser l'ancre vidéo.** L'ancre IMU est proposée automatiquement (détection
   du début de mouvement, décision #7) ; l'ancre vidéo, elle, n'est calculable
   par aucune machine. Avec la lecture à l'arrêt, chercher le début du mouvement
   avec les contrôles natifs de la vidéo, puis **« caler ici (mediaTime) »**.
   Affiner avec les boutons ±. C'est une **béquille** : la vraie interface
   d'alignement est le ticket #9, et ce prototype n'a pas à la préfigurer.
   L'alignement est mémorisé par take dans le `localStorage`.
3. *Lire.* La vidéo suit.
4. Basculer de variante avec les **flèches ← →** ou la barre jaune en bas.

## Ce qu'il faut regarder

**Sur la mise en page** (chaque variante mise sur autre chose) :

| | pari | ce qu'elle met en jeu |
|---|---|---|
| **A** Incrustation permutable | on ne regarde qu'une image à la fois, l'autre sert de contrôle | la vignette est-elle assez grande pour vérifier quoi que ce soit — et le HUD survit-il à une fenêtre posée dessus ? |
| **B** Côte à côte | rien ne se recouvre, deux vues de même taille se comparent | deux moitiés d'un écran de portable, c'est beaucoup de noir autour d'une image 16/9 |
| **C** Superposition | on ne compare plus, on vérifie : la roue de synthèse décolle visiblement de la roue filmée | sans champ ni pose de la caméra (que rien n'enregistre), la superposition ne peut pas coïncider — l'idée mérite-t-elle qu'on aille chercher ces données ? |

Et dans les trois : le HUD reste-t-il lisible, l'état survit-il à un
rechargement (il est dans l'URL et le `localStorage`), la vidéo est-elle
masquable ?

**Sur le comportement**, tout est dans le bloc « Prototype #8 » de la colonne
de gauche :

- **Le tracé de dérive.** Trait plein = la dérive de commande (`currentTime`) ;
  points = la dérive réellement observée (`mediaTime`, le seul temps qui porte
  le PTS de l'image affichée — acquis n°10) ; traits jaunes = le seuil ; barres
  rouges = les recalages. Si les points s'écartent du trait, `currentTime` ment
  et c'est un résultat, pas un détail.
- **Le seuil**, réglable en direct. L'acquis n°7 avance ~100 ms. Le baisser
  jusqu'à ce que ça saccade, le monter jusqu'à ce que ça décroche : la bonne
  valeur est entre les deux, et c'est ce qu'on cherche.
- **Le mode de recalage.** `recalage dur seul` est ce que l'acquis prescrit ;
  `trim de vitesse` est le prétendant — au lieu de sauter, on joue 5 % plus vite
  jusqu'à ce que l'écart se referme. Ça ne saccade jamais ; reste à voir si
  c'est assez rapide.
- **Les vitesses extrêmes.** Régler la vitesse de lecture à 0,25 puis à 4, et
  lire la ligne « vitesse demandée / obtenue » : un navigateur qui refuse un
  taux le fait en silence.
- **Le coût.** Cocher *vidéo détachée* pendant une lecture : le décodeur
  s'arrête, la mise en page ne bouge pas, et les deux lignes fps · paq/s se
  remplissent l'une après l'autre. L'écart entre elles **est** la réponse à
  « le décodage vidéo dispute-t-il le budget du thread principal ». Les images
  vidéo perdues disent le coût vu de l'autre bout.
- **« copier le relevé »** met tout ça en JSON dans le presse-papier, à coller
  dans le ticket.

## Ce que ça touche, et ce qu'il faudra retirer

| fichier | rôle |
|---|---|
| `api/viz/prototype-video-sync/` | tout le prototype frontend |
| `api/prototype_video_sync.py` | sert la vidéo, propose l'ancre IMU |
| `api/app.py` | un bloc `PROTOTYPE`, monte le routeur |
| `api/viz/viz.js` | quatre crochets, `alpha: true`, `?types=frame,meta` |
| `sessions` (lien symbolique) | pointe vers les données du checkout principal |

`?variant=off` désarme le prototype entier : le viz se comporte exactement comme
sans lui, ce qui est aussi la référence dont l'A/B du coût a besoin.

**La seule pièce qui vaut d'être relevée** le jour de l'implémentation est
[`sync-clock.js`](sync-clock.js) — l'acquis n°7 mis en œuvre à la lettre,
isolé du DOM autour de lui. Le reste est un échafaudage.

## Ce que le prototype ne fait pas, exprès

- **Il n'écrit rien sur le disque.** L'alignement va dans le `localStorage`, pas
  dans `take.json` : le modèle de données (décision #6) n'est pas encore
  implémenté, et un prototype n'a pas à créer des fichiers qu'il faudra
  démigrer.
- **Il ne préfigure pas l'interface d'alignement** (#9).
- **Il ne construit aucun chemin** à partir de ce que le navigateur envoie : le
  backend scanne le disque et le client ne fait que désigner une entrée du
  catalogue. La carte #2 signale un trou réel de traversée de chemin ; ce n'est
  pas un prototype qui doit l'élargir.
