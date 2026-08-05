# Conductor

Orchestrateur d'un projet de roue Cyr instrumentée : il reçoit les données d'une centrale
inertielle, les interprète en signaux artistiques, et les publie vers des sorties créatives.
Ce glossaire fixe le vocabulaire du domaine — il ne décrit aucune implémentation.

## Language

### Captation

**Take** :
Un enregistrement, du début à la fin d'un geste ou d'un passage. Unité de base de tout ce
qui est rejoué, aligné et analysé.
_Éviter_ : prise, run, capture, session (une session en contient plusieurs).

### Alignement vidéo

**Début de mouvement** :
L'événement de référence qui situe un take dans sa vidéo : la roue est immobile au sol,
puis levée d'un geste vif. Choisi parce qu'il est détectable automatiquement côté inertiel
et identifiable à la frame près côté vidéo.
_Éviter_ : clap, top, départ, marqueur.

**Alignement** :
La paire d'ancres qui situe le début de mouvement d'un take dans sa vidéo. C'est un fait
statique, écrit une fois et confirmé par un humain.
_Éviter_ : synchronisation, calage, offset.

**Synchronisation** :
Le suivi de la timeline du take par la vidéo pendant la lecture. C'est un comportement,
recalculé en continu — à ne pas confondre avec l'alignement, qui est la donnée dont il part.
_Éviter_ : alignement.

**Ancre IMU** :
L'instant du début de mouvement sur la timeline du take.
_Éviter_ : marqueur, timestamp de sync.

**Ancre vidéo** :
L'instant du même début de mouvement dans la vidéo. C'est une valeur *mesurée* sur la frame
que l'humain a désignée, jamais un numéro de frame recalculé.
_Éviter_ : temps de sync, offset vidéo.

**Proposition** :
Le résultat de la détection automatique du début de mouvement. Recalculable à volonté et
sans autorité tant qu'un humain ne l'a pas confirmée — c'est ce qui permet de retoucher la
détection sans jamais invalider un alignement déjà validé.
_Éviter_ : détection (qui est le procédé, pas son résultat), estimation, suggestion.
