# La courbe d'un take enregistré part brute, la réduction se fait au tracé

Pour tracer un signal d'un take enregistré, l'API renvoie **tous les échantillons**, et c'est
le navigateur qui réduit en enveloppes min/max par colonne de pixels au moment du tracé. C'est
l'inverse de ce que fait `GET /api/model/history`, qui réduit côté serveur — et cet écart est
délibéré, pas un oubli.

## Pourquoi

La réduction min/max n'est pas remise en cause : une décimation naïve lisserait exactement le
front qu'on cherche à voir, un début de mouvement ne durant que quelques échantillons. Ce qui
change, c'est **où** elle a lieu.

`ScopeRing` réduit côté serveur parce qu'il répond à un problème vivant : un anneau de 24 000
échantillons à 400 Hz qui se rafraîchit en continu, interrogé pendant que de nouvelles données
arrivent. Un take enregistré est l'exact opposé — **un fichier figé, chargé une fois**, sur
localhost.

L'argument décisif est le **zoom**. Vérifier un alignement est fondamentalement une activité de
zoom : l'erreur qui compte se joue à quelques dizaines de millisecondes, soit une poignée de
frames vidéo. Avec une réduction côté serveur, chaque niveau de zoom est un aller-retour, et la
résolution du tracé est choisie par le serveur plutôt que par ce que l'œil regarde. Avec la
courbe entière en mémoire, le zoom est instantané et toujours à pleine fidélité.

Le coût mesuré autorise ce choix : la norme du gyro d'un take de 59 s tient en **40 Ko** de JSON
(2942 échantillons, ~14 octets chacun), soit environ **400 Ko** pour 5 minutes à 100 Hz.

## Options écartées

- **Réduire côté serveur, comme `/api/model/history`.** Cohérent avec l'existant et une seule
  forme à maintenir — mais un aller-retour par zoom, sur le geste précisément qu'on va répéter
  le plus.
- **Décimer (un échantillon sur N).** Le moins cher, et faux pour la raison même qui a fait
  choisir les enveloppes dans `scope.py` : le front recherché fait quelques échantillons de
  large et disparaîtrait.
- **Rejouer le take pour alimenter `ScopeRing`.** C'est le seul moyen aujourd'hui de voir la
  courbe d'un take, et il coûte une lecture en temps réel — plusieurs minutes pour regarder une
  demi-seconde.

## Conséquences

- **La réduction devient la responsabilité du client.** Tout consommateur de cet endpoint doit
  la faire, sous peine d'afficher une courbe qui ment. Ce n'est pas une contrainte lourde — une
  boucle min/max par colonne de pixels — mais elle n'est plus garantie par le serveur.
- **La charge utile croît linéairement avec durée × cadence.** Le choix tient parce qu'un take
  dure des minutes à 50–100 Hz. Un take d'une heure, ou une cadence de 400 Hz, le remettraient
  en cause : c'est la borne à surveiller, et le retour aux enveloppes serveur serait alors la
  bonne réponse.
- **`ScopeRing` n'est pas concerné.** Les deux mécanismes coexistent volontairement, parce
  qu'ils répondent à deux questions différentes : surveiller un flux vivant, ou examiner un
  enregistrement.
- La courbe est un **dictionnaire de canaux nommés**, jamais un tableau nu : en ajouter un est
  une clé de plus, pas un changement de contrat.
