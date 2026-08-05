# L'alignement vidéo ne dépend pas du réglage du modèle

Un take est aligné sur sa vidéo par un **début de mouvement** (roue immobile au sol, puis
levée d'un geste vif), repéré des deux côtés. Côté inertiel, cet instant est détecté sur la
**norme du gyroscope brute lue dans le CSV** — jamais sur un signal produit par `model/` —
et la détection ne fait que **proposer** : ce qui fait autorité est la paire d'ancres qu'un
humain a confirmée, et cette paire n'est jamais recalculée en silence.

## Pourquoi

Les paramètres du modèle sont conçus pour être retouchés en permanence, à chaud, y compris
en spectacle (`model/params.py`). Si l'instant d'alignement dérivait d'un signal du modèle,
**déplacer un curseur invaliderait l'alignement de tous les takes déjà calés** — sans erreur,
sans trace, et sans que personne le remarque avant de voir une vidéo décalée des mois plus
tard.

Une norme de gyroscope brute avec un seuil fixe est immunisée : c'est un fait de cinématique
mesuré une fois, qui ne bouge plus. Et le découpage proposition / confirmation prolonge la
même garantie côté outillage — **le seuil de détection peut être retouché indéfiniment sans
qu'aucun alignement confirmé ne bouge**, puisque la détection ne fait jamais autorité.

## Options écartées

- **Détecter sur un signal du modèle.** Plus expressif, et cohérent avec le reste du système
  — mais suppose de faire tourner le modèle hors ligne sur le CSV (ce runner n'existe pas),
  et rend l'alignement solidaire d'un réglage.
- **Stocker la proposition dans `take.json`.** Donnerait une pastille « détectable » dans la
  liste des takes, mais un nombre que personne ne peut dater : calculé avec quel seuil, avant
  ou après le dernier changement ? Une proposition stockée acquiert une durabilité qui
  contredit sa définition. Elle est recalculée à la demande, pour environ une seconde de
  lecture de CSV.
- **Détecter à l'arrêt de l'enregistrement.** Le retour arriverait au seul moment où le take
  peut encore être refait — mais suppose quelqu'un devant l'écran, alors qu'en séance
  l'opérateur est sur la roue.

## Conséquences

- L'alignement survit à toute évolution de `model/`, y compris au renommage ou à la
  suppression d'un signal.
- La liste des takes ne peut pas afficher d'indicateur de détection : elle est servie à 4 Hz
  par `SessionManager.active_tree()` et ne peut rien recalculer.
- Les deux ancres sont stockées, jamais leur différence : le décalage est un résidu, les
  ancres sont des faits — et l'ancre vidéo est la seule donnée du dispositif qu'aucune
  machine ne peut reproduire.
