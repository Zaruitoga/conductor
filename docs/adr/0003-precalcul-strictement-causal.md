# Le précalcul est licite tant qu'il reste strictement causal

Un résultat calculé à l'avance peut nourrir n'importe quelle sortie, y compris l'OSC, **à
la seule condition d'avoir été produit par une boucle `model.feed()` en avant**. Ce qui
reste interdit n'est pas le cache : c'est la lecture du futur.

Cet ADR **corrige l'acquis n°9** de la carte #2, qui plaçait la ligne ailleurs — « le
précalcul est légitime pour ce qu'on inspecte, jamais pour ce qui sort vers l'OSC ».

## Pourquoi

L'invariant « même code live et rejoué » ne tient pas au *moment* où le modèle tourne. Il
tient à l'API de `Context` : une fonction de signal reçoit `ctx.values` (ce tick) et
`ctx.prev` (le tick précédent), et **il n'existe aucun accesseur vers une valeur future**.
Toute boucle qui appelle `model.feed()` en avant est donc causale par construction,
qu'elle avance au rythme d'une horloge murale ou à fond de CPU.

Un cache rempli ainsi n'est pas un autre calcul : c'est **le même calcul, décalé dans le
temps**. Aucun consommateur ne peut faire la différence, et c'est vérifiable plutôt que
promis — `OscBridge` s'abonne à `FRAME`, `EVENT` et `META`, jamais à `RAW` : il n'existe
aucun chemin par lequel il pourrait distinguer une frame calculée il y a une seconde d'une
frame calculée il y a une heure.

Ce que l'acquis n°9 protégeait reste protégé mot pour mot : « ce qui est réglé sur un
replay est atteignable en spectacle ». Le danger n'a jamais été le stockage — c'est
l'**affordance** qu'un chemin batch installerait : filtrage à phase nulle, événement
replacé sur son vrai pic plutôt qu'au franchissement, normalisation par un maximum que le
live ne connaîtra jamais. Ces trois-là restent interdits, et ils le sont indépendamment de
l'existence d'un cache.

Formulé comme il l'était, l'acquis interdisait la conception correcte la moins chère pour
une raison qui ne s'y appliquait pas.

## Ce qui reste interdit

- **Filtrer en non-causal** (`filtfilt` et apparentés) : la sortie dépendrait d'échantillons
  postérieurs, inatteignables en live.
- **Replacer un événement** sur son instant vrai après coup. Un détecteur franchit un
  seuil ; il ne sait pas qu'un pic plus haut arrive dans 200 ms.
- **Normaliser sur le take entier.** Un maximum global n'est pas connaissable en spectacle.
- Plus généralement : **tout second chemin de calcul** qui ne passerait pas par
  `model.feed()`. La règle n'est pas « pas de cache », elle est « pas de deuxième modèle ».

## Options écartées

- **Garder l'acquis tel quel** (précalcul réservé à l'inspection). Cohérent avec la carte,
  et faux : il interdit un cache que le modèle a lui-même rempli en avant, dont la sortie
  est indistinguable du live, tout en n'interdisant pas plus efficacement le non-causal —
  qui reste possible dans un chemin « d'inspection » si personne ne l'a écrit noir sur blanc.
- **Interdire tout précalcul.** Rend le balayage impossible sans rien acheter : la causalité
  vient de `Context`, pas de l'absence de mémoire.
- **Autoriser le non-causal en rejeu, puisque le fichier est là.** L'argument est réel — un
  rejeu *possède* le futur, et le lui interdire jette de l'information. Écarté parce que la
  finalité du rejeu ici est de **répéter un spectacle** : une sortie plus belle en rejeu
  qu'en live n'est pas un progrès, c'est un piège silencieux. Un jour où l'analyse hors-ligne
  (et non la répétition) deviendra un besoin, ce sera un autre outil, nommé comme tel.

## Conséquences

- **La règle est vérifiable par lecture**, pas par intention : un code qui n'accède au
  passé que par `ctx.prev` est causal. Il n'y a rien à faire respecter en plus.
- **Le contrat de `Context` devient l'endroit à défendre.** Ajouter un accesseur qui rendrait
  une valeur future — même « juste pour l'inspection » — supprimerait la garantie d'un coup,
  partout à la fois. C'est le point de vigilance, pas les caches.
- L'acquis n°9 de la carte #2 est à lire comme corrigé par ce document.
