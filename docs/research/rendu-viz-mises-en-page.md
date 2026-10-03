# Coût de rendu des trois mises en page du viz

Mesure pour [#28](https://github.com/Zaruitoga/conductor/issues/28), le 2026-08-17. Question posée :
la superposition (scène transparente composée par-dessus la vidéo) fait-elle perdre des images ou
des paquets ?

## Conditions

Fenêtre au premier plan, 1280×800 à dpr 2 (donc `pixelRatio` 1,5 et MSAA coupé), dix secondes par
mise en page, lecture à ×1 du take de référence avec sa vidéo. Mesuré avec `fps()` exporté par
`api/viz/_harness.js` — à lancer dans un onglet visible : `requestAnimationFrame` est suspendu dans
un onglet caché, et la fonction refuse alors de produire des chiffres.

| vue | fps | `renderer.render` | p95 entre images | max | paquets/s |
|---|---|---|---|---|---|
| incrustation | 60,1 | 0,17 ms | 17,7 ms | 17,9 ms | 50 |
| incrustation permutée | 60,0 | 0,15 ms | 17,7 ms | 18,0 ms | 50 |
| côte à côte | 60,0 | 0,16 ms | 17,7 ms | 17,9 ms | 50 |
| superposition | 60,0 | 0,11 ms | 17,7 ms | 17,8 ms | 50 |

## Lecture

**La superposition reste.** Par ordre d'importance :

- **La colonne `max`.** À 60 Hz une image perdue se lit 33 ms ; sur quarante secondes de campagne,
  aucun intervalle n'a atteint deux périodes de vsync, quelle que soit la mise en page.
- **Le dessin coûte environ 1 % d'un budget de 16,7 ms**, et la superposition est la *moins* chère :
  le sol et la grille y sont masqués, donc la géométrie limitée par le remplissage (ce pour quoi le
  rendu est plafonné) est justement ce qu'elle ne dessine pas ; l'image derrière est composée par le
  navigateur.
- **Le débit de paquets tient à 50 Hz** — l'autre mode de défaillance, un fil principal saturé qui ne
  vide plus le socket, d'où les deux chiffres côte à côte dans le HUD.

`fps` seul ne tranche rien (plafonné par le vsync) : la colonne décisive est `rendu_ms`, le temps
passé *dans* `renderer.render`.

## Limites

- Un écran, une taille de fenêtre. Le coût de remplissage croît avec la surface : un plein écran 4K
  est à remesurer (une ligne dans la console).
- La passe de référence sans décodeur (`{repere: true}` sur un take sans vidéo) n'a pas été faite, donc
  la part propre du décodeur n'est pas isolée — elle ne peut pas l'être via `rendu_ms`, le décodage
  se faisant hors du fil de rendu. Ce qui la borne : rien n'a été perdu nulle part.
