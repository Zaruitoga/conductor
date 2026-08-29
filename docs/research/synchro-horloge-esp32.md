# Quel échange met l'horloge de l'ESP et celle de l'hôte d'accord, et à quel rythme le refaire ?

Recherche pour le ticket [#52](https://github.com/Zaruitoga/conductor/issues/52), sous la carte
[#49](https://github.com/Zaruitoga/conductor/issues/49) « performances du lien WiFi en conditions
d'installation ».
Recherche seulement : **aucun correctif n'est appliqué ici**, ni dans ce dépôt ni dans le firmware.

**Enjeu.** Chaque paquet porte deux estampilles — `ts_esp_us`, le `micros()` de l'ESP à l'émission
(`report_manager.cpp:208`), et `ts_rx_us`, l'heure de l'hôte à la réception
(`transport/protocol.py:192`) — et elles viennent de deux compteurs qui n'ont jamais été mis
d'accord. Leur écart contient donc une constante inconnue, et seule sa *variation* est exploitable :
la reconnaissance de #49 a dû ancrer sur la médiane faute de savoir où était le zéro. La latence
absolue est la seule grandeur du verdict qu'aucune mesure passive ne donne, et c'est celle qui dirait
si un paquet met 3 ms ou 40 ms à traverser la salle. Une synchro épisodique la donne pour **tous** les
paquets de la séance, sans rien ajouter au fil : quelques secondes d'échanges transforment le
`ts_esp_us` de chaque paquet normal en latence unidirectionnelle réelle.

---

## Verdict

**Quatre estampilles par aller-retour (la forme de la RFC 5905 §8), une dizaine d'échanges, on garde
celui dont le délai aller-retour est le plus petit. Deux estampilles à poser dans le firmware — deux
appels à `micros()` — et le biais de la boucle disparaît par construction au lieu d'être rétréci.
La synchro se fait aux deux bouts du take et on interpole linéairement : le quartz du module est
spécifié à ±10 ppm, ce qui fait 6 ms sur dix minutes, et interpoler retire le terme linéaire
gratuitement là où une resynchro périodique le poursuivrait toutes les 100 s. Les deux ancres
s'écrivent dans `take.json`, jamais leur différence — exactement comme l'alignement vidéo — donc le
protocole du fil ne change pas d'un octet en régime : un `CfgType` de plus et un type de réponse
dédié, aucun champ dans `DataHeader`, aucune colonne CSV, aucun décodeur de données touché.**

Les sept points décisifs, et ce qu'il faut accepter avec :

| | Établi | Contrepartie |
|---|---|---|
| **1. Ce que `micros()` compte** | Chaîne complète, sans trou : `micros()` → `esp_timer_get_time()` → compteur systimer ÷ 16 → CNT_CLK = f<sub>XTAL</sub>/2,5 = 16 MHz → **le quartz de 40 MHz du module**. `CONFIG_PM_ENABLE` n'est pas posé et le S3 a un tick systimer *fixe*, donc ni la fréquence CPU, ni le modem-sleep, ni rien d'autre que le quartz ne peut changer la cadence de `micros()`. | Si quelqu'un active un jour le light-sleep, le systimer est rechargé depuis le timer RTC, dont la source configurée ici est **l'oscillateur RC interne** (`sdkconfig:988`). L'hypothèse « seul le quartz compte » tombe alors entièrement. |
| **2. Le quartz réel** | Le Feather 5323 porte un module **ESP32-S3-MINI-1**, dont la fiche technique v1.7 dessine son propre schéma : `Y1 40MHz(±10ppm)` (Fig. 8-1, p. 39). Ce n'est pas la borne générique des guides de conception, c'est le composant qu'Espressif pose dans le module. | ±10 ppm est une **borne de fabrication à la température de réglage**, pas une mesure de cet exemplaire-ci. Et Espressif ne spécifie **nulle part** de coefficient de température : ni la fiche du module, ni celle de la puce, ni les guides de conception ne contiennent le mot. §7 (a). |
| **3. L'algorithme** | Cristian borne l'erreur de lecture à la **moitié de l'aller-retour mesuré**, moins le délai minimal — sous l'hypothèse que ce minimum est le même dans les deux sens. La RFC 5905 §8 donne la même chose en quatre estampilles, et son intervalle de correction λ = δ/2 + ε est la même borne, à `min` près (la RFC ne le retranche pas). | La RFC 5905 **n'emploie jamais le mot « asymétrie »**. Le terme irréductible (d<sub>aller</sub> − d<sub>retour</sub>)/2 n'est pas nommé : il est caché dans la largeur de λ. Aucun protocole à deux sens ne le mesure ; il faut le dire, pas l'estimer. |
| **4. Le biais de la boucle** | `_sendAck` estampille `micros()` **juste avant** `endPacket()` (`config_server.cpp:133-137`), donc l'ACK porte déjà un T3 propre. Ce qui est retardé, c'est T2→T3 par le `DRAIN_BUDGET_MS` de 50 ms. **Estampiller T2 l'annule exactement** : δ = (T4−T1) − (T3−T2) ne contient plus l'attente, et θ non plus. | Sans T2 (l'état d'aujourd'hui, à trois estampilles), il faut **30 pings pour 96 % de chances** que le meilleur ait attendu moins de 5 ms, 50 pour 87 % sous 2 ms — et l'épisode dure 1,5 à 2,5 s au lieu de 0,5 s. §3.3. |
| **5. Réponse immédiate ?** | **Non.** Une réponse hors budget de drain *rétrécit* l'attente ; estampiller la *supprime*. Deux `micros()` et 8 octets contre une restructuration de la boucle que `DRAIN_BUDGET_MS` a précisément été réglé pour équilibrer (commit `66068c1`). | Il reste une raison de vouloir une réponse rapide, et elle n'est pas la précision : `ConfigServer::poll()` ne lit **qu'un datagramme par tour de boucle**, ce qui plafonne la cadence des pings à ~20 Hz et fixe la durée minimale de l'épisode. §3.2. |
| **6. L'asymétrie que rien n'annule** | Le **modem-sleep** est actif par défaut (découverte de [#51](https://github.com/Zaruitoga/conductor/issues/51)) et il ne frappe **qu'un sens** : l'AP tamponne le trafic descendant d'une station endormie et le délivre au DTIM, tandis que la station émet quand elle veut. C'est *exactement* le terme (d<sub>aller</sub> − d<sub>retour</sub>)/2, et il vaut jusqu'à la moitié d'un intervalle DTIM — 51 à 154 ms. | Donc l'épisode de synchro **doit** tourner avec `WiFi.setSleep(false)`, remis après. Ça ne fausse pas le take : le sommeil modem déplace les *délais*, pas l'*offset*, qui est une propriété d'horloge. Mais c'est une ligne de firmware que la synchro rend obligatoire. |
| **7. La cadence de resynchro** | **Aux deux bouts du take, dans l'enregistrement, et on interpole.** Le terme linéaire disparaît sans hypothèse ; ce qui reste est la non-linéarité, que la température pilote et que #65 mesure. Une resynchro périodique seule demanderait un épisode toutes les **100 s** à 10 ppm pour tenir 1 ms. | Une ancre n'a de sens que dans un référentiel : l'épisode doit tomber **à l'intérieur** du take, sinon il n'y a pas de `frame.t` où l'écrire. C'est ce qui rend la question « ça perturbe-t-il le flux ? » structurante et non anecdotique. §5. |

**Deux découvertes incidentes, qui débordent ce ticket :**

1. **L'ACK porte déjà T3, et l'hôte le jette.** `parse_ack` commence par `off = DATA_HEADER.size`
   (`transport/protocol.py:298`) : l'en-tête est sauté en entier, `ts_esp_us` compris. Une synchro de
   Cristian à trois estampilles est donc réalisable **aujourd'hui, sans une ligne de firmware** — il
   suffit de cesser de jeter ce champ. C'est ce qui doit amorcer le banc (#65) : la ligne de base que
   le changement de firmware devra battre.
2. **`ts_rx_us` est l'horloge murale, et elle est disciplinée par le réseau.**
   `time.time_ns()` (`protocol.py:192`) lit `CLOCK_REALTIME`, et cette machine fait tourner
   `/usr/libexec/timed` contre `time.apple.com` (`/etc/ntp.conf`, vérifié). Un pas ou un lissage
   d'Apple pendant une passe est **indiscernable d'une dérive du quartz de l'ESP** : les deux
   apparaissent comme un écart qui glisse. C'est le contre-sens exact que cette recherche existe pour
   éviter, et il vit dans le décodeur de paquets. §1.3.

**Ce qui n'est pas vérifiable ici** : §7.

---

## 0. Les versions qui font foi

Le firmware est `~/Desktop/IMU_project/PIO projects/260524_BNO_super_reports`. `platformio.ini`
déclare `platform = espressif32`, `board = adafruit_feather_esp32s3_nopsram`, `framework = arduino`,
**sans épingler aucune version**. Ce qui fait foi est donc ce qui est installé sur cette machine, et
je l'ai relu plutôt que de reprendre le chiffre de la note voisine :

| | Version | Source lue |
|---|---|---|
| arduino-esp32 | **2.0.17** | `cores/esp32/esp_arduino_version.h:22-26` (2 / 0 / 17) |
| ESP-IDF embarqué | **v4.4.7** | `tools/sdk/esp32s3/include/esp_common/include/esp_idf_version.h:22-26` (4 / 4 / 7) |
| cible | `esp32s3` | `tools/sdk/esp32s3/sdkconfig:7` — `CONFIG_IDF_TARGET="esp32s3"` |
| paquet | `3.20017.241212+sha.dcc1105b` | `~/.platformio/packages/framework-arduinoespressif32/package.json` |

La carte, elle, n'est pas une inconnue : la définition PlatformIO
`~/.platformio/platforms/espressif32/boards/adafruit_feather_esp32s3_nopsram.json` porte
`"url": "https://www.adafruit.com/product/5323"`. C'est **l'Adafruit ESP32-S3 Feather 8 MB Flash, no
PSRAM**, et le module qu'elle embarque est un **ESP32-S3-MINI-1** — la page produit renvoie à la
fiche technique du module, et le module a son propre quartz (§ 4.1).

Les liens amont pointent sur les tags [`arduino-esp32@2.0.17`](https://github.com/espressif/arduino-esp32/tree/2.0.17)
et [`esp-idf@v4.4.7`](https://github.com/espressif/esp-idf/tree/v4.4.7), vérifiés identiques aux
fichiers locaux. Les fiches techniques citées le sont **à leur numéro de version** : ESP32-S3-MINI-1
& MINI-1U v1.7, ESP32-S3 Series v2.2, ESP32-S3 TRM v1.8.

Une exception assumée : les *ESP Hardware Design Guidelines* n'existent qu'en `latest` — j'ai
essayé `v5.5`, `v5.4`, `v5.3` et `v4.4.7`, toutes en 404. C'est la seule URL non versionnée de cette
note, et ce qu'elle apporte (§ 4.2) est de toute façon confirmé par la fiche du module, qui est
versionnée.

---

## 1. Où les deux estampilles sont posées, exactement

La question de #52 est une question de *points d'estampillage* avant d'être une question
d'algorithme. Un protocole à quatre estampilles n'est bon que si chacune est posée près du fil ;
tout ce qui sépare l'estampille du fil est du délai qu'on attribuera au réseau.

### 1.1 Côté ESP : `micros()`, et il est bien placé

Un paquet de données est construit et estampillé dans `_sendPacket` :

```cpp
// report_manager.cpp:201-215
hdr.ts_esp_us = micros();          // :208

_udp.beginPacket(_host, _port);    // :210
_udp.write((const uint8_t*)&hdr, sizeof(hdr));
_udp.write(payload, len);
if (_udp.endPacket() == 0) _udpErrors++;
```

L'estampille est prise **trois lignes avant** la remise à lwIP. Il n'y a rien entre les deux qu'une
copie de 12 à 44 octets. C'est le meilleur point d'estampillage atteignable sans matériel : ce que
`ts_esp_us` nomme est bien l'instant de l'émission, pas celui de la lecture du capteur.

Cette distinction n'est pas rhétorique. Pour un super-slot, les composantes de la charge utile ont
été copiées à des instants antérieurs et différents, quand chaque événement du BNO est arrivé
(`_extractPayload`, appelé depuis `handleSensorEvent`, `report_manager.cpp:132-144`). **La latence
que la synchro rendra mesurable est donc « émission → réception », pas « mouvement → réception ».**
Le délai capteur→émission (SHTP, SPI, position dans le drain) reste hors de portée de ce ticket, et
il faut le dire plutôt que de laisser croire que la chaîne est mesurée de bout en bout.

L'ACK de configuration est estampillé de la même façon :

```cpp
// config_server.cpp:128-137
hdr->ts_esp_us  = micros();        // :133
_udp.beginPacket(ip, port);        // :135
_udp.write(buf, total);
_udp.endPacket();
```

**L'ACK porte donc déjà un T3 utilisable.** C'est la première découverte incidente : les trois quarts
d'un échange de Cristian existent sur le fil aujourd'hui.

Ce qui n'existe pas, c'est T2 — l'instant où l'ESP a *reçu* la requête. `ConfigServer::poll()`
(`config_server.cpp:12-18`) lit le datagramme et appelle `_handle` sans rien estampiller.

### 1.2 Ce que `micros()` compte réellement, jusqu'au composant

La chaîne se remonte sans trou, chaque maillon dans une source qui en est propriétaire.

**Maillon 1 — Arduino ne fait rien.**
[`cores/esp32/esp32-hal-misc.c:166-169`](https://github.com/espressif/arduino-esp32/blob/2.0.17/cores/esp32/esp32-hal-misc.c#L166-L169) :

```c
unsigned long ARDUINO_ISR_ATTR micros()
{
    return (unsigned long) (esp_timer_get_time());
}
```

La troncature à 32 bits est *là*, et c'est elle que `model/clock.py` déroule (`_WRAP = 1 << 32`,
`clock.py:42`) — 71 min 35 s de période. Aucun cache, aucun décalage : tout est dans `esp_timer`.

**Maillon 2 — `esp_timer` est le systimer.** `tools/sdk/esp32s3/sdkconfig:1217` porte
`CONFIG_ESP_TIMER_IMPL_SYSTIMER=y`, et
[`esp_timer_impl_systimer.c:65-70`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_timer/src/esp_timer_impl_systimer.c#L65-L70)
est d'une brièveté qui ne laisse rien à interpréter : la valeur du compteur divisée par
`SYSTIMER_LL_TICKS_PER_US`, que `hal/esp32s3/include/hal/systimer_ll.h:27` fixe à **16**.

**Maillon 3 — le systimer est cadencé par le quartz.** ESP32-S3 TRM v1.8, chapitre 11 « System
Timer », § 11.3 (p. 635) : les compteurs sont pilotés par XTAL_CLK, un diviseur fractionnaire
alterne /3 et /2, et la fréquence moyenne est f<sub>XTAL</sub>/2,5, soit 16 MHz. La fiche technique
de la puce (v2.2, § 4.1.3.6) le redit du côté des caractéristiques : compteurs à 16 MHz.

**Maillon 4 — et rien d'autre ne le touche.** Deux vérifications qui ferment les échappatoires :

- `soc_caps.h:238` pour l'esp32s3 déclare `SOC_SYSTIMER_FIXED_TICKS_US (16)` — « le nombre de ticks
  par microseconde est fixe ». C'est un cas particulier de l'ESP32-S3 : sur S2 et C3, l'IDF doit
  appeler `systimer_hal_set_steps_per_tick()` quand l'horloge APB change
  ([`systimer_hal.c:148-179`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/hal/systimer_hal.c#L148-L179)).
  Ici, non : **la fréquence CPU ne déplace pas `micros()`**.
- `sdkconfig:1147` : `# CONFIG_PM_ENABLE is not set`. Le cadre de gestion d'énergie est absent de
  cette construction, donc pas de DFS automatique et pas de light-sleep automatique. Le modem-sleep
  du § 6 éteint la RF, le PHY et la bande de base — pas le systimer.

**Conclusion du § 1.2 :** entre `micros()` et le quartz, il n'y a aucun élément dont la fréquence
puisse bouger. La seule dérive possible de l'horloge de l'ESP est celle du quartz lui-même, et c'est
ce qui rend le § 4 décisif au lieu d'être une précaution.

*Une réserve d'ordre négligeable, notée pour être complète :* le diviseur fractionnaire alterne /3
et /2, donc le tick n'est uniforme que **sur deux cycles** (TRM § 11.2). L'irrégularité est de l'ordre
de 30 ns. Contre des millisecondes, c'est du bruit de fond ; contre une mesure de dérive en ppm sur
dix minutes, aussi.

### 1.3 Côté hôte : l'estampille est murale, et elle est disciplinée

```python
# transport/protocol.py:192
ts_rx_us = time.time_ns() // 1000
```

`time.time_ns()` est `CLOCK_REALTIME`. C'est ce qui va dans le paquet, dans la première colonne du
CSV (`storage/csv_logger.py:51`), et dans `first_ts_rx_us` / `last_ts_rx_us` de chaque take
(`storage/session_manager.py:104-105`). Sur cette machine, vérifié :

```
/etc/ntp.conf          →  server time.apple.com.
ps -ax                 →  /usr/libexec/timed
```

Une horloge murale disciplinée par le réseau ne dérive pas librement : elle est **corrigée**, par
lissage ou par pas. Et une correction de l'hôte a la même signature qu'une dérive de l'ESP — l'écart
`ts_rx_us − ts_esp_us` glisse. C'est précisément la grandeur que ce ticket veut ancrer.

Deux conséquences, à des adresses différentes :

- **Pour l'épisode de synchro**, T1 et T4 doivent venir d'une horloge *monotone*
  (`time.monotonic_ns()`), avec un seul ancrage sur l'horloge murale pour dater l'épisode. Sinon le
  θ estimé porte les corrections de `timed`.
- **Pour `ts_rx_us` lui-même**, la question est plus ouverte et n'est pas de ce ticket : c'est la
  colonne du CSV, elle est écrite dans quatre takes, et la changer touche `row_to_packet`. À noter
  pour #54 ; à ne pas décider ici.

**Le point d'estampillage côté hôte, lui, n'est pas le problème.** J'ai vérifié sur cette machine que
macOS supporte `SO_TIMESTAMP` (`SOL_SOCKET` = 65535, `SCM_TIMESTAMP` = 2, une `struct timeval` de
16 octets), et mesuré l'écart entre l'estampille du noyau et le retour de `recvmsg` en espace
utilisateur : **23, 58 et 62 µs** sur trois datagrammes en loopback à vide. Deux à trois ordres de
grandeur sous la question. Il ne faut donc pas survendre `SO_TIMESTAMP` : à vide il ne corrige rien
de perceptible. En charge, en revanche, l'écart n'est plus borné — c'est le mécanisme même que #49 a
observé —, et pour **T4** cela compte, parce que T4 doit nommer l'arrivée *sur le fil* et non
l'instant où notre boucle a bien voulu la regarder. Pour un `ts_rx_us` de paquet de données, au
contraire, inclure notre propre ordonnancement est légitime : c'est du délai réel jusqu'au modèle.

Note pratique : `asyncio.DatagramProtocol` n'utilise pas `recvmsg` et n'expose donc pas la donnée
auxiliaire. L'épisode de synchro doit passer par sa propre socket, ce que le § 6 impose de toute
façon pour d'autres raisons.

### 1.4 Ce que l'écart contient aujourd'hui

En posant Δ = `ts_rx_us` − `ts_esp_us` :

```
Δ  =  θ            offset des deux compteurs à l'origine (inconnu, ~ arbitraire)
    + ρ · t        dérive relative (le quartz de l'ESP contre l'horloge disciplinée de l'hôte)
    + d(t)         la latence unidirectionnelle réelle — la seule chose qu'on veut
    + s(t)         les corrections de `timed` sur l'hôte  (§ 1.3)
```

Aujourd'hui, seule la variation de Δ est lisible, et elle mélange les trois derniers termes. La
synchro épisodique donne θ ; deux synchros donnent ρ ; et il reste s(t), qui est la découverte
incidente n° 2 et qu'il faut retirer en changeant d'horloge côté hôte, pas en mesurant mieux.

---

## 2. L'algorithme : trois estampilles suffisent, quatre valent mieux

### 2.1 Cristian, et l'hypothèse exacte

Flaviu Cristian, *Probabilistic clock synchronization*, Distributed Computing 3:146-158, 1989. Le
texte original est chez Springer derrière un péage. La formulation qui fait autorité et qui est
librement accessible est celle du **même auteur** qui la restate pour la généraliser : Cristian &
Fetzer, *Probabilistic Internal Clock Synchronization* (Symposium on Reliable Distributed Systems,
1994 ; version d'archive de mai 2003, § 3.1), qui renvoie explicitement à [1] = l'article de 1989.

Le principe y tient en une phrase : pour lire l'horloge d'un processus distant, il faut mesurer
l'aller-retour sur sa propre horloge, et la connaissance de cet aller-retour permet à la fois
d'estimer l'horloge distante *et de borner l'erreur commise*. L'estimation retenue est le
**milieu de l'intervalle** dans lequel l'horloge distante est encadrée — de sa valeur annoncée plus
le délai minimal `min`, à cette même valeur plus (aller-retour − `min`) —, et le texte conclut que
cela borne l'erreur de lecture au pire cas à *la moitié de la taille de cet intervalle*,
c'est-à-dire

```
erreur  ≤  D/2  −  min        (D = aller-retour mesuré, min = délai unidirectionnel minimal)
```

plus un terme de dérive pendant l'échange, négligeable ici (10 ppm sur 50 ms = 0,5 µs).

**L'hypothèse est là et pas ailleurs :** `min` est *le même* dans les deux sens. C'est ce qui permet
de retrancher `min` deux fois et de placer l'estimation au milieu. Rien dans l'échange ne la vérifie,
et rien ne peut la vérifier — un échange à deux sens ne mesure que leur somme.

Un détail du même article mérite d'être relevé, parce qu'il décrit la forme exacte du problème de
#49 : sur 200 000 aller-retours mesurés sur un réseau Ethernet, le minimum observé était au-dessus
de 1 700 µs, la moyenne sous 1 900 µs, plus de la moitié rentraient en 1 750 µs — et le maximum
observé atteignait ~156 000 µs. Un régime propre, une queue très longue. C'est la distribution pour
laquelle le filtre par minimum est conçu, et c'est celle que la reconnaissance de #49 décrit avec
d'autres chiffres (p50 ≈ 0 ms, p95 ≈ 5 ms, p99 ≈ 65–72 ms).

### 2.2 La forme à quatre estampilles

RFC 5905 (NTPv4), § 8 « On-Wire Protocol », p. 29. Le client émet à T1, le serveur reçoit à T2,
répond à T3, le client reçoit à T4. Les quatre donnent :

```
θ  =  ½ · [ (T2 − T1) + (T3 − T4) ]        offset du serveur par rapport au client
δ  =  (T4 − T1) − (T3 − T2)                aller-retour, temps serveur retiré
```

La RFC 4330 (SNTPv4), § 5, donne exactement les deux mêmes formules et le même tableau T1..T4 — c'est
la forme allégée, sans les algorithmes de filtrage et de sélection.

Ce qu'il faut voir dans θ : **T3 − T2 n'y apparaît pas.** La quantité vaut
(T2 − T1) − (T4 − T3), c'est-à-dire la différence entre le délai aller mesuré au compteur du serveur
et le délai retour mesuré au compteur du client. Le temps que le serveur passe à réfléchir est
absent des deux formules. C'est le résultat central pour le point 2 de #52, et le § 3 en tire la
conséquence.

### 2.3 Ce que le minimum sur N achète, et ce qu'il n'achète pas

Retenir l'échange de δ minimal parmi N est un filtre sur **le bruit de file d'attente** : les
épisodes de retard sont rares (2,5 % au-dessus de 20 ms, selon la reconnaissance), donc le minimum
sur une dizaine d'échanges tombe presque sûrement dans le régime propre.

Il n'achète **rien** contre une asymétrie *constante*. Si le chemin aller est structurellement plus
long que le retour de A, chaque échange porte le même biais A/2 et le minimum le porte aussi. C'est
une propriété de l'estimateur, pas un défaut d'échantillonnage.

La RFC 5905 le formalise sans le nommer : elle définit la **distance de synchronisation**
λ = δ/2 + ε (§ 10, p. 39), et l'algorithme de sélection place l'offset vrai dans un intervalle de
correction centré sur θ et de demi-largeur λ (§ 11.2.1, p. 43). Avec ε ≈ 0 pour un
échange frais, **l'intervalle de correction fait ±δ/2** — la borne de Cristian, à ceci près que la
RFC ne retranche pas `min` et se tient donc du côté conservateur. La largeur de cet intervalle *est*
l'aveu que la répartition de δ entre les deux sens est inconnue.

### 2.4 Ce que la RFC ne dit pas

Le ticket demande ce que la RFC 5905 dit de l'asymétrie du chemin. La réponse honnête : **rien
d'explicite**. Le mot « asymmetry » n'apparaît pas une fois dans les 6 163 lignes du document
(vérifié, recherche insensible à la casse ; RFC 4330 non plus). La RFC ne traite l'asymétrie que
comme la largeur de λ, c'est-à-dire comme une incertitude à propager, jamais comme un terme à
corriger.

Ce n'est pas une lacune de la RFC : c'est la vérité du problème. Un protocole à deux sens ne peut
pas séparer θ de (d<sub>aller</sub> − d<sub>retour</sub>)/2, et aucun raffinement d'estimation ne
changera ça. **La conséquence pour cette note est de ne pas annoncer une précision, mais une
précision sous hypothèse, et de nommer la seule asymétrie systématique connue ici** — le modem-sleep,
§ 3.5.

---

## 3. Le biais de la boucle, chiffré

### 3.1 D'où il vient exactement

`loop()` (`main.cpp:93-124`) fait trois choses dans l'ordre : `cfgServer.poll()` (`:107`), puis le
drain du BNO borné à `DRAIN_BUDGET_MS` = 50 ms (`:27`, `:112-116`), puis le heartbeat toutes les 2 s
(`:120-123`). Le commentaire de `:22-26` dit pourquoi le budget existe : `getSensorEvent()` se cale
sur le flux du capteur, donc sans borne le drain tient la boucle pour sa durée entière.

Un datagramme de configuration arrivé juste après le `poll()` attend donc le tour de boucle suivant.
La période de boucle est **le budget lui-même** dès que le drain sature, ce qui est le cas normal à
100 Hz sur deux flux plus un super : le commentaire dit que la sortie normale du drain est le budget,
pas la file vide.

Le seul chiffre mesuré qui existe côté dépôt est dans `config.py:118-121` : « acked in <100 ms
(measured ~54 ms) ». C'est **une observation**, sans distribution ni commande nommée, et il faut la
traiter comme telle. Elle est cohérente avec une attente uniforme sur [0, ~50 ms] plus le réseau, et
c'est tout ce qu'on peut en dire. Détail à savoir avant de la reproduire : `CFG_SET_SIMPLE`,
`CFG_SET_SUPER`, `CFG_DEL_SUPER` et `CFG_SET_HOST` appellent tous `saveNVS()` — **une écriture
flash** — avant l'ACK. `CFG_GET_STATE` est le seul qui n'écrit rien (`config_server.cpp:71-73`).
C'est donc le seul candidat honnête pour une mesure de RTT, et le seul candidat pour une synchro
bâtie sur le protocole existant.

### 3.2 Le plafond de cadence, qui est une contrainte et pas un réglage

```cpp
// config_server.cpp:12-18
void ConfigServer::poll() {
  int len = _udp.parsePacket();
  if (len <= 0) return;
  ...
  _handle(_rxBuf, len, _udp.remoteIP(), _udp.remotePort());
}
```

**Un datagramme par tour de boucle.** Envoyer des pings plus vite que ~20 Hz ne les fait pas traiter
plus vite : ils s'empilent dans le tampon de réception de lwIP et sortent un par tour. Pire, chacun
mesurerait alors sa propre attente dans cette file, pas le réseau — l'estimation se dégraderait en
mesurant le tampon.

**Cela fixe la durée plancher d'un épisode** : N pings ⇒ N/20 secondes, quoi qu'on fasse. C'est ce qui
donne son sens à « quelques secondes d'échanges » dans #52, et c'est ce qui rend le § 3.4 décisif :
la seule façon d'écourter l'épisode est de réduire N, donc d'améliorer chaque échange.

### 3.3 Combien de pings, pour quelle confiance

Sans T2, l'attente D = T3 − T2 est incluse dans l'aller-retour et on ne peut que l'espérer petite.
Modéliser D comme uniforme sur [0, P] avec P = 50 ms est le modèle le plus simple qui soit fidèle à
`loop()` : la requête arrive à un instant sans rapport avec la phase de la boucle.

Le minimum de N tirages uniformes sur [0, P] a pour espérance P/(N+1), et
P(min ≤ x) = 1 − (1 − x/P)<sup>N</sup> :

| N | E[min] | P(min ≤ 5 ms) | P(min ≤ 2 ms) | P(min ≤ 1 ms) | durée à 20 Hz |
|---:|---:|---:|---:|---:|---:|
| 10 | 4,55 ms | 65,1 % | 33,5 % | 18,3 % | 0,50 s |
| 20 | 2,38 ms | 87,8 % | 55,8 % | 33,2 % | 1,00 s |
| **30** | **1,61 ms** | **95,8 %** | 70,6 % | 45,5 % | **1,50 s** |
| **50** | **0,98 ms** | 99,5 % | **87,0 %** | 63,6 % | **2,50 s** |
| 100 | 0,50 ms | 100 % | 98,3 % | 86,7 % | 5,00 s |
| 200 | 0,25 ms | 100 % | 100 % | 98,2 % | 10,00 s |

L'erreur de Cristian étant D/2 − min, le résidu de boucle attendu vaut la moitié de la colonne
E[min] : **0,8 ms à N = 30, 0,5 ms à N = 50**. Contre le budget de 1 ms que le § 4.4 dérive, la
réponse à « combien de pings » est donc **30 à 50, soit 1,5 à 2,5 s** — et cette durée est un plancher
imposé par le § 3.2, pas un choix.

### 3.4 Réponse immédiate, ou estampiller ? Estampiller.

Le ticket demande si une réponse immédiate pendant l'épisode — hors du budget de drain — change assez
l'estimation pour justifier son coût. **Non, et pour une raison plus forte qu'un arbitrage
coût/bénéfice : ce n'est pas la bonne opération.**

Une réponse immédiate *rétrécit* D. Estampiller T2 *l'élimine de l'algèbre* (§ 2.2) : δ et θ ne
contiennent plus T3 − T2 du tout. Éliminer bat rétrécir, à quelque prix que ce soit — et ici le prix
est plus faible aussi :

| | Réponse immédiate | Estampiller T2 |
|---|---|---|
| ce que ça fait à D | le réduit à ~0 | le retire des formules, exactement |
| coût firmware | restructurer `loop()` ou sortir la config dans une tâche | **un `micros()` dans `poll()`, un dans `_sendAck`** |
| ce que ça casse | l'équilibre drain / config / heartbeat que `DRAIN_BUDGET_MS` règle (commit `66068c1`) | rien |
| N nécessaire | ~10 (il reste le réseau) | ~10 (il reste le réseau) |
| durée d'épisode | 0,5 s | **0,5 s** |

Avec T2 et T3 sur le fil, δ ne mesure plus que le réseau. Vu la distribution de #49 — 95 % des
paquets dans les 5 ms du plancher — le minimum sur **une dizaine** d'échanges est déjà au plancher :
la probabilité que dix échanges consécutifs soient tous dans la queue des 5 % est de 10<sup>−13</sup>.
D'où la recommandation : **N = 10, épisode de 0,5 s**, un cinquième de ce que la forme à trois
estampilles impose, et sans terme de boucle résiduel du tout.

### 3.5 L'asymétrie que rien n'annule : le modem-sleep

Voilà le point qui décide de la validité de tout ce qui précède, et il vient de la note voisine.

[#51](https://github.com/Zaruitoga/conductor/issues/51) a établi que le firmware n'appelle jamais
`WiFi.setSleep()`, donc l'ESP32-S3 tourne en **modem-sleep minimum** (`WIFI_PS_MIN_MODEM`), où la RF,
le PHY et la bande de base sont éteints entre deux DTIM — un défaut d'arduino-esp32 que personne n'a
choisi. Voir `docs/research/cadence-rssi-esp32.md`, § 2.2.

Cette économie d'énergie est **unidirectionnelle par construction**. Une station endormie ne reçoit
pas : l'AP tamponne son trafic descendant et l'annonce dans le TIM/DTIM, la station se réveille au
DTIM et le récupère. À l'émission, rien de tel — elle transmet quand elle veut.

Donc, sur l'échange de synchro : **le ping hôte → ESP attend jusqu'à un intervalle DTIM, la réponse
ESP → hôte n'attend rien.** C'est exactement d<sub>aller</sub> ≫ d<sub>retour</sub>, et le biais sur
θ vaut la moitié de cette attente :

| balise | DTIM | attente maximale du descendant | biais maximal sur θ |
|---|---|---|---|
| 102,4 ms | 1 | 102,4 ms | **51,2 ms** |
| 102,4 ms | 3 | 307,2 ms | **153,6 ms** |

Ce biais est du même ordre que la queue de latence que #49 cherche à caractériser. Il rendrait la
synchro **pire qu'inutile** : elle produirait un chiffre d'apparence précise et faux d'un facteur
qu'aucune répétition ne révélerait, puisque deux synchros successives le porteraient à l'identique.

Le filtre par minimum aide en partie — l'attente n'est pas constante, elle est uniforme sur
l'intervalle DTIM, donc un ping tombant juste avant un réveil attend peu. Mais atteindre le plancher
demanderait un N d'un autre ordre, et le plafond de 20 pings/s du § 3.2 transforme ça en épisode de
plusieurs dizaines de secondes.

**La réponse n'est donc pas d'augmenter N, c'est d'éteindre le sommeil pendant l'épisode :**
`WiFi.setSleep(false)` avant, remis après. Un point important : cela **ne fausse pas** le take.
L'offset est une propriété d'horloge ; le sommeil modem déplace les *délais*, pas l'offset. Un take
enregistré avec le sommeil actif, dont l'offset a été établi avec le sommeil coupé, donne bien les
délais réels du take — sommeil modem compris, ce qui est justement ce que #49 veut mesurer.

C'est aussi la raison pour laquelle #63 (« `WiFi.setSleep(false)` vaut-il d'être imposé ») et #52 se
touchent : la synchro n'exige pas que le spectacle coupe le sommeil, mais elle exige que l'épisode le
coupe.

---

## 4. La dérive du quartz

### 4.1 Ce que le matériel est réellement

Trois niveaux, et il faut descendre jusqu'au dernier.

1. **La carte** — Adafruit ESP32-S3 Feather, produit **5323** (nommé dans le JSON de board
   PlatformIO, § 0). Adafruit publie son schéma en PNG dans le guide *Downloads*, et les fichiers
   EagleCAD sur GitHub. Le schéma de la carte ne peut de toute façon **pas** répondre : le quartz
   n'est pas dessus.
2. **Le module** — `ESP32-S3-MINI-1`. La fiche technique v1.7, § 1.1 « Integrated Components on
   Module », liste **un oscillateur à quartz de 40 MHz** ; les schémas fonctionnels (Fig. 2-1 et 2-2,
   p. 9) le montrent à l'intérieur du périmètre du module. C'est Espressif qui pose et qui règle ce
   composant, pas Adafruit.
3. **Le composant** — et la fiche du module donne son propre schéma, Fig. 8-1 « ESP32-S3-MINI-1
   Schematics », p. 39. Le quartz y est référencé :

   ```
   Y1  40MHz(±10ppm)
   ```

**C'est la source la plus proche du composant qui existe publiquement**, et elle est meilleure que ce
que le ticket espérait : non pas une exigence générique adressée à un concepteur de carte, mais
l'annotation d'Espressif sur son propre schéma de module. Le MINI-1U (Fig. 8-2, p. 40) porte la même.

*Note :* Espressif écrit la même borne dans les *ESP Hardware Design Guidelines* pour l'esp32s3
(§ « External Crystal Clock Source (Compulsory) ») : le firmware n'accepte qu'un quartz de 40 MHz, et
la précision du quartz choisi doit être dans ±10 ppm. Là c'est une **exigence de conception**, avec
une procédure de réglage par les capacités de charge et un contrôle du décalage de fréquence RF. Les
deux sources concordent ; celle du module fait davantage autorité pour *cette* carte, puisqu'elle
décrit l'objet fabriqué et non la contrainte imposée à qui en fabrique un.

### 4.2 Ce que ±10 ppm veut dire, et surtout ce qu'il ne dit pas

**Ce qu'il dit :** l'écart de fréquence du composant est dans ±10 ppm, dans les conditions où la
tolérance est spécifiée. La procédure des guides de conception le confirme comme un réglage de
*décalage de fréquence* : on observe la porteuse à 2,4 GHz et on ajuste les capacités de charge
jusqu'à rentrer dans ±10 ppm.

**Ce qu'il ne dit pas, et j'ai cherché :**

- Aucun **coefficient de température**, nulle part. Recherche du motif `ppm` dans le texte extrait
  des trois documents : deux occurrences dans la fiche du module (les deux schémas, `Y1`),
  **zéro** dans la fiche technique de la puce v2.2 (87 pages), et dans les guides de conception,
  uniquement la borne ±10 ppm. Le module est pourtant annoncé de −40 à +85 °C (fiche v1.7, § 1.1).
- Aucune distinction entre tolérance initiale, stabilité en température et vieillissement — ce qu'une
  fiche de quartz donne d'habitude en trois lignes. La référence du composant elle-même n'est pas
  publiée.

Donc : **±10 ppm est une borne de fabrication à la température de réglage, et le comportement en
température de l'exemplaire posé sur cette carte-ci n'est documenté par personne.** C'est exactement
la règle de la carte #49, et ici elle n'est pas une précaution de forme : c'est le fait dominant. Le
choix du § 4.5 est fait *en connaissance de cette ignorance*, pas malgré elle.

### 4.3 Les chiffres

Décalage accumulé = ppm × durée. La dernière colonne est la période de rebouclage du compteur 32
bits, qui est le seul horizon dur du système (`model/clock.py:41-42`).

| dérive | 1 min | 10 min | 30 min | 71 min 35 s |
|---:|---:|---:|---:|---:|
| 5 ppm | 0,30 ms | 3,0 ms | 9,0 ms | 21,5 ms |
| **10 ppm** (la spécification) | **0,60 ms** | **6,0 ms** | **18,0 ms** | **42,9 ms** |
| 20 ppm | 1,20 ms | 12,0 ms | 36,0 ms | 85,9 ms |
| 50 ppm | 3,00 ms | 30,0 ms | 90,0 ms | 214,7 ms |

Et à l'envers, le temps qu'il faut pour accumuler une erreur donnée :

| dérive | 1 ms | 5 ms |
|---:|---:|---:|
| 5 ppm | 200 s | 1 000 s |
| **10 ppm** | **100 s** | **500 s** |
| 20 ppm | 50 s | 250 s |
| 50 ppm | 20 s | 100 s |

**Face à quoi ?** La reconnaissance de #49 donne p50 ≈ 0 ms, p95 ≈ 5 ms, queue p99 ≈ 65–72 ms. À
10 ppm, six millisecondes sur dix minutes, c'est **plus que le p95 de la latence en régime normal**.
Le ticket avait raison de dire qu'une synchro unique au début ne suffit pas — et il l'avait dit sur
une fourchette 10–50 ppm ; la spécification réelle est le bas de cette fourchette, ce qui ne change
pas la conclusion.

### 4.4 Le budget, et il est dérivé

Le budget ne doit pas être emprunté. Deux dérivations, du dépôt :

**Par la cadence du modèle.** Le modèle tourne à la cadence de la quantité maîtresse — 100 Hz dans la
configuration de la roue —, donc un tick vaut 10 ms. Une latence dont l'incertitude approcherait un
tick ne permettrait plus de dire *à quel tick* un paquet appartenait. Un dixième de tick, **1 ms**,
rend le chiffre digne de la résolution à laquelle tout le reste du système raisonne.

**Par la roue.** Elle tourne à ~2 tr/s, soit 720°/s. Une milliseconde vaut **0,72° de roue**. C'est
en dessous de ce que le repliement du retard sur `spin_deg` (le remplaçant de la condition
« rotation », §7 de la note #51) peut distinguer : à 24 secteurs de 15°, une erreur de 0,72° ne
déplace jamais un échantillon de secteur.

Les deux donnent le même ordre, ce qui est le meilleur signe qu'il est le bon. **Budget : 1 ms
d'erreur sur l'offset, tenue sur toute la durée d'un take.**

Le budget est vérifié aussi par le haut : à 1 ms, on distingue franchement le régime normal
(p50 ≈ 0, p95 ≈ 5 ms) de la queue (p99 ≈ 65–72 ms), qui est la distinction que #49 a construite.

### 4.5 Les trois options, et laquelle

Le ticket en pose trois. À 10 ppm et 1 ms de budget :

**(c) Mesurer la dérive une fois et la corriger — écartée.** Deux raisons, dont la seconde est
la vraie. D'abord la dérive appartient à l'exemplaire *et* à sa température, et l'ESP chauffe : une
constante mesurée en salle froide est fausse en salle chaude et rien ne le dirait. Ensuite, et
surtout : c'est le défaut que l'[ADR 0001](../adr/0001-alignement-video-independant-du-modele.md)
nomme pour la proposition d'onset — un nombre
que personne ne peut dater acquiert une durabilité qui contredit sa définition. Une correction de
dérive stockée une fois est exactement ça.

**(a) Resynchroniser périodiquement — écartée comme réponse principale, gardée comme repli.**
Le tableau du § 4.3 en donne le prix : pour tenir 1 ms à 10 ppm, il faut un épisode **toutes les
100 s**, soit six par take de dix minutes, *à l'intérieur* de la performance. Et chacun ne fait que
remettre le compteur à zéro sans rien apprendre sur la dérive.

**(b) Synchroniser aux deux bouts et interpoler linéairement — retenue.** Le terme linéaire, qui est
tout le § 4.3, disparaît sans hypothèse aucune : deux ancres définissent une droite, et une droite
est exactement le modèle « ρ constant ». Ce qui reste est la **non-linéarité** de la dérive, qui est
d'un ordre inférieur et que la température pilote — et le heartbeat porte déjà `cpu_temp_c`
(`protocol.h:43`), donc #65 peut la lire sans instrumentation supplémentaire.

Deux propriétés de (b) qu'il faut assumer :

- **La seconde ancre n'existe qu'une fois le take fini.** Ce n'est pas un défaut : l'offset n'a
  **aucun consommateur en direct**. Le pont OSC est cadencé sur l'horloge murale et par principe ne
  lit pas `ctx.t_us` pour ça ; la viz suit `frame.t` ; le panneau ne l'affiche pas. La latence
  absolue sert à *caractériser une passe*, ce qui est une lecture d'après coup — et le conductor est
  entièrement bâti pour ça : takes, pistes de pose, alignement, relecture. En direct, on utilise
  l'ancre d'ouverture plus le ρ mesuré au take précédent, ce qui est bon à quelques ppm.
- **L'épisode doit tomber dans le take.** Une ancre a besoin d'un référentiel, et le seul référentiel
  qu'un take possède est sa propre timeline (`frame.t`, celle que `TimeBase` déroule et que
  `onset_imu_s` utilise déjà). Un épisode avant `POST /api/recording/start` n'a pas de `frame.t` où
  s'écrire. Donc : premier épisode dans la première seconde du take, second dans la dernière — ce qui
  tombe bien, ce sont les moments où la roue est encore à plat et où le geste d'onset n'a pas
  commencé (`storage/onset.py` cherche justement ≥ 2 s de silence).

**Et si #65 montre que le résidu n'est pas linéaire ?** Alors (a) revient, et le § 4.3 donne
directement sa période : 1 ms / ρ mesuré. À 10 ppm, 100 s ; à 20 ppm, 50 s. C'est le seul chiffre de
cette note qui attend une mesure pour être posé, et c'est voulu.

---

## 5. Ce que l'épisode coûte au spectacle

Le § 4.5 rend la question structurante : si l'épisode perturbe, il ne peut pas être dans le take, et
(b) s'effondre.

**Ce qu'un épisode ajoute, chiffré.** N = 10 échanges à 20 Hz sur 0,5 s. Chaque échange coûte :

- *au fil* : un datagramme de ~16 octets vers l'ESP, un de ~24 octets en retour. Contre 100 à 200
  paquets de données par seconde, 20 datagrammes/s de plus font **+10 à +20 % du nombre de trames**
  pendant une demi-seconde, et bien moins en octets (les paquets de données font 12 à 56 octets de
  charge utile plus l'en-tête).
- *à la boucle* : le travail existe déjà et il est mesuré par `DRAIN_BUDGET_MS`. Répondre à un ping
  de synchro est **moins** de travail que répondre à un `CFG_GET_STATE` : l'ACK complet fait
  **226 octets** (12 + 1 + 8×12 + 1 + 8×14 + 4) et passe par un `sscanf` sur la chaîne d'hôte
  (`config_server.cpp:122-123`), là où une réponse de synchro est trois `uint32` et rien d'autre.

**Ce qui inquiète vraiment n'est pas la charge, c'est l'air.** #49 a déjà établi la signature du
phénomène : *aucune perte, du retard groupé*, ce qui est la signature d'une retransmission de niveau
liaison. Ajouter des trames en compétition sur le canal peut allonger cette queue. Une demi-seconde
à +15 % de trames est petit, mais ce n'est pas nul, et personne ne l'a mesuré.

**Verdict provisoire, à confirmer au banc :** l'épisode tient dans le take. La mesure qui trancherait
est en §7, et elle est simple — compter les trous de `seq` et comparer la distribution de retard
pendant l'épisode et en dehors, sur la même passe.

**Un coût qui n'est pas au fil et qu'il faut nommer :** `WiFi.setSleep(false)` pendant l'épisode
(§ 3.5) réveille la radio et augmente la consommation. La roue est sur batterie, et #63 pèse
justement ce coût. Une demi-seconde par bout de take est une fraction négligeable de la séance ; c'est
un argument de plus pour (b) à deux épisodes contre (a) à six.

---

## 6. Le format sur le fil, et où l'ancre s'écrit

### 6.1 Sur le fil : un `CfgType`, et rien dans `DataHeader`

C'est la question qui coûte le plus cher dans #54 : toucher `DataHeader` (12 octets, `<BBHII`) touche
`transport/protocol.py`, `simulator/wire.py`, le schéma CSV, `row_to_packet` — et rend les takes
existants illisibles, `DataHeader.version` valant 1 partout.

**Rien de tout ça n'est nécessaire.** La proposition :

```c
// protocol.h — un CfgType de plus
CFG_SYNC = 0x06,          // corps : uint32 t1_lo  (écho, pour apparier requête et réponse)

// et un PacketType de plus pour la réponse, à côté de PKT_CFG_ACK
PKT_SYNC_REPLY = 0x31,    // DataHeader + uint32 t1_echo + uint32 t2 + uint32 t3   → 24 octets
```

Ce que ça donne :

- **Zéro octet ajouté aux paquets de données.** La question du coût en octets/s de #54, pour ce
  candidat, est réglée : elle est nulle en régime.
- **`parse_packet` ne change pas.** Il renvoie déjà `None` pour `ACK_TYPE` (`protocol.py:171-172`) ;
  `0x31` prend le même chemin. Rien n'entre dans `PACKET_FIELDS`, donc rien n'entre dans le CSV, donc
  `row_to_packet` ne change pas.
- **La compatibilité descendante est gratuite dans le bon sens.** Un firmware ancien recevant
  `CFG_SYNC` tombe dans le `default:` de `_handle` et **ne répond pas** (`config_server.cpp:75-78`) —
  le client de synchro voit un timeout et sait qu'il parle à un firmware qui ne sait pas. Aucune
  réponse erronée possible.

Côté firmware, l'ajout est de trois lignes utiles :

```cpp
// dans poll(), avant _handle : un micros() pour T2
// dans le nouveau cas CFG_SYNC : un micros() pour T3, juste avant endPacket
```

`micros()` renvoyant un `uint32` déjà tronqué (§ 1.2), les trois champs sont des `uint32` et le
rebouclage est le problème connu — voir § 6.3.

**Pourquoi pas réutiliser `CFG_GET_STATE` ?** Parce que sa réponse est l'ACK de 226 octets construit
avec un `sscanf`, ce qui met du travail entre T2 et T3. Avec quatre estampilles ce travail est
soustrait par δ, donc il ne fausse rien — mais il rallonge le temps où la boucle ne draine pas le
BNO, à répéter dix fois. Une réponse dédiée est plus petite et plus simple ; et surtout elle rend le
ping distinguable dans les compteurs, ce qui compte pour le § 5.

**Ce qui reste vrai sans firmware du tout**, et par quoi #65 doit commencer : la forme de Cristian à
trois estampilles sur `CFG_GET_STATE`, avec T3 = le `ts_esp_us` que l'ACK porte déjà et que
`parse_ack` jette (`protocol.py:298`). C'est la ligne de base contre laquelle mesurer le gain de
l'ajout.

### 6.2 Côté hôte : pas par `EspConfigurator`

Trois propriétés de `EspConfigurator` le rendent inutilisable tel quel pour un épisode, et ce ne sont
pas des défauts — ce sont exactement les bonnes décisions pour son travail :

- `_send` **vide la socket avant chaque envoi** (`esp_configurator.py:202`) pour ne jamais lire un
  ACK périmé. Dans une rafale, ça jetterait une réponse légitime en vol.
- `_recv_ack` **ne garde que le datagramme le plus frais** quand plusieurs sont en attente
  (`:237-246`). Une synchro a besoin d'apparier chaque réponse à *sa* requête, pas de garder la
  dernière.
- Tout y est bloquant et appelé via `run_in_executor` : dix aller-retours coûteraient dix passages
  par un thread, avec l'ordonnancement du GIL entre T1 et T4.

L'épisode veut sa propre socket, un `time.monotonic_ns()` pour T1 et T4 (§ 1.3), et si possible
`SO_TIMESTAMP` pour T4. C'est du code d'hôte, pas du protocole : il n'entre pas dans la décision de
#54.

### 6.3 Où l'ancre est écrite : dans `take.json`, et jamais leur différence

C'est le précédent de l'alignement vidéo, et il s'applique mot pour mot. `onset_imu_s` et
`onset_video_s` sont **tous deux stockés, jamais leur différence** : le décalage est un résidu que
chaque côté recalcule, tandis que les ancres sont des faits. Ici, pareil :

```
sync_open   = { t_take_s, offset_us }      épisode du début, daté sur la timeline du take
sync_close  = { t_take_s, offset_us }      épisode de la fin
```

et ρ = (offset_close − offset_open) / (t_close − t_open) est le résidu. Trois conséquences, toutes
héritées de dispositifs qui existent :

- **Une synchro est indivisible**, comme un alignement. `TakeUpdate` applique déjà la règle aux deux
  ancres de l'alignement — un corps qui n'en porte qu'une est refusé — et c'est ce qui garde
  « pas encore aligné » un état sans champ à lui, ni booléen, ni horodatage de confirmation. La même
  règle sur les deux ancres de synchro donne « pas encore synchronisé » au même prix : zéro.
- **Un `take.json` antérieur reste lisible.** `load_take` filtre sur les champs déclarés de
  `TakeMeta`, donc un take enregistré avant cette addition se lit et se signale simplement comme non
  synchronisé. C'est la propriété qui a permis de supprimer le dispositif de marqueur de synchro
  d'un bloc sans faire disparaître les takes du panneau.
- **Aucun marqueur d'époque dans les paquets de données n'est nécessaire** — ce que #54 envisageait
  comme un candidat — *à une condition* : que l'ESP n'ait pas redémarré pendant le take. Un
  redémarrage remet `micros()` à zéro et invalide les deux ancres. `TimeBase` détecte l'événement
  (il le classe `DISCONTINUITY`, `clock.py:154-160`) et compte les discontinuités, mais un take ne
  garde pas ce compteur, et le heartbeat qui porte `uptime_ms` **n'est pas enregistré**
  (`PACKET_FIELDS` en est la liste, et il en est absent). **Donc : ou bien un take conserve le
  compteur de discontinuités de son `TimeBase`, ou bien le heartbeat devient enregistrable
  ([#56](https://github.com/Zaruitoga/conductor/issues/56)).** C'est une exigence que ce ticket pose
  et que #54 doit trancher ; c'est un entier de plus dans `take.json`, ou la révision d'une décision
  documentée.

### 6.4 Ce que le simulateur ne pourra pas vérifier

`simulator/esp32.py` modélise l'horloge de l'ESP par `time.monotonic() - self._boot` (`:168`, `:213`)
— c'est-à-dire l'horloge monotone de l'hôte lui-même. Une synchro développée contre le simulateur
lirait donc toujours θ ≈ 0 et ρ = 0, **et ne testerait jamais l'estimation**. Elle testerait le
format et l'appariement, ce qui est déjà utile (c'est ce que `simulator/wire.py` fait pour tout le
reste).

La correction est de deux lignes et vaut d'être notée pour #66 : donner au simulateur un offset et
un skew délibérés, `(time.monotonic() - boot) * (1 + skew) + offset`, avec des valeurs réglables. Un
skew de 10 ppm et un offset de quelques secondes rendent l'estimateur testable hors matériel — et
rendent testable, surtout, le déroulage du `uint32` sur deux épisodes séparés par un rebouclage.

---

## 7. Ce qui n'est pas vérifiable ici, et les mesures qui trancheraient

Quatre points où la note s'arrête, et la règle de #49 s'applique : rien de ce qui suit ne compte tant
que la carte sur la table ne l'a pas montré.

**(a) Le quartz de cet exemplaire-ci.** ±10 ppm est ce qu'Espressif annote sur son propre schéma de
module (§ 4.1). Ce n'est pas une mesure de la pièce posée sur cette carte, et **aucun coefficient de
température n'est publié** — ni par le module, ni par la puce, ni par les guides de conception
(§ 4.2). C'est le point où l'écart entre le papier et le matériel est le plus probable, et c'est la
raison d'être de #65.

**(b) La symétrie du chemin.** Elle ne se prouve pas depuis un échange à deux sens (§ 2.4). Le banc
ne pourra pas la prouver non plus. Ce qu'il peut montrer, et qui suffit, c'est la **répétabilité** :
deux synchros successives doivent donner le même θ à quelques centaines de microsecondes près. Un θ
répétable et biaisé reste utilisable — le biais est constant, il se soustrait des *variations*, qui
est ce que #49 lit déjà. Un θ non répétable est du bruit.

**(c) La période réelle de la boucle.** Le § 3.3 modélise l'attente comme uniforme sur [0, 50 ms],
sur la foi du commentaire de `main.cpp:22-26`. Si le drain sort régulièrement sur file vide, la
période est plus courte et tous les N du tableau baissent. Se lit au port série en trois lignes.

**(d) Le DTIM de la box.** Le § 3.5 raisonne sur 1 à 3, comme la note #51. C'est ce qui fixe le biais
maximal du modem-sleep, entre 51 et 154 ms. Se lit depuis un ordinateur du réseau (`wdutil info` sur
macOS, ou une capture en mode moniteur), et c'est déjà une sortie prévue du banc (#61).

### Mesure A — la ligne de base, sans une ligne de firmware

À faire **en premier**, parce qu'elle ne dépend de rien et qu'elle donne le chiffre que le changement
de firmware devra battre. Côté hôte seulement : une socket UDP sur le port de config, N envois de
`CFG_GET_STATE` espacés de 50 ms, et pour chacun T1 = `time.monotonic_ns()` avant l'envoi,
T4 = idem après réception, T3 = le champ `ts_esp_us` de l'en-tête de l'ACK — **celui que `parse_ack`
jette aujourd'hui**.

```python
version, pkt_type, size, seq, t3 = protocol.DATA_HEADER.unpack_from(data)   # t3 : uint32, µs ESP
```

Pour chaque échange, δ = T4 − T1 et θ_Cristian = t3 − (T1 + T4)/2. Sortie : la distribution de δ sur
N échanges, et le θ de l'échange de δ minimal.

**Lecture attendue.** Un δ minimal de l'ordre de quelques millisecondes, un δ médian autour de 25 à
30 ms (la moitié du budget de drain), et une queue. Refaire la passe **deux fois de suite** : les
deux θ doivent tomber à moins d'une milliseconde l'un de l'autre, sans quoi le point (b) est déjà
tranché par la négative. Et refaire les deux passes **avec et sans `WiFi.setSleep(false)`** : c'est
la mesure du § 3.5, et elle est décisive — si le θ se déplace de plusieurs dizaines de
millisecondes entre les deux, le sommeil modem est bien la grande asymétrie et l'épisode doit le
couper.

### Mesure B — la dérive de cet exemplaire, sur la durée d'un take

Deux passes de la mesure A encadrant dix minutes de fonctionnement normal. ρ = Δθ / Δt en ppm.
À refaire trois ou quatre fois dans la même séance, en notant `cpu_temp_c` (que le heartbeat porte
déjà) au début et à la fin de chaque intervalle.

**Lecture attendue** : |ρ| ≤ 10 ppm si la spécification tient sur cet exemplaire, soit |Δθ| ≤ 6 ms
sur dix minutes. Trois cas et trois conclusions :

- ρ **constant** d'une passe à l'autre et indépendant de la température ⇒ l'option (b) du § 4.5 est
  confirmée telle quelle, et l'interpolation linéaire est exacte.
- ρ **suivant la température** ⇒ (b) tient toujours, mais son résidu est à chiffrer : intercaler une
  troisième synchro au milieu et mesurer l'écart entre le θ mesuré et le θ interpolé. C'est **la**
  mesure qui décide, et elle coûte un épisode de plus.
- |ρ| **au-delà de 20 ppm** ⇒ la fiche du module est démentie sur cet exemplaire, et le § 4.3 donne
  la période de resynchro à appliquer (1 ms / ρ).

Une passe de contrôle est indispensable et facile à oublier : **mesurer aussi la dérive de l'hôte**.
Comparer `time.monotonic_ns()` et `time.time_ns()` sur les dix mêmes minutes isole les corrections
de `timed` (découverte incidente n° 2). Sans elle, un pas d'Apple pendant la passe se lirait comme
une dérive du quartz.

### Mesure C — ce que l'épisode coûte au flux

Sur une passe de deux minutes à 100 Hz avec le super-slot habituel, déclencher un épisode de 10
pings au milieu, et comparer les trente secondes qui l'entourent :

- trous de `seq` par flux, avant / pendant / après ;
- distribution de (`ts_rx_us` − `ts_esp_us`) − médiane, aux mêmes trois fenêtres ;
- `packets_sent` du heartbeat contre le compte reçu, qui sépare la perte de l'air de la perte de
  l'hôte.

**Lecture attendue** : rien de discernable. Si l'épisode fait apparaître des trous ou déplace le p95,
il ne peut pas vivre dans le take, l'option (b) perd sa timeline (§ 4.5) et il faut alors soit
enregistrer le take autour des épisodes, soit revenir sur le stockage de l'ancre.

---

## 8. Ce que ça décide pour #54

**La latence absolue entre dans le protocole, et elle n'y coûte pas un octet en régime.** C'est le
résultat le plus utile de cette note pour la décision : le candidat que #54 qualifie de « plus
solide » se paie avec un `CfgType`, un `PacketType` de réponse, et deux `micros()` — et **aucun**
décodeur de données n'est touché : ni `DataHeader`, ni `PACKET_FIELDS`, ni le schéma CSV, ni
`row_to_packet`, ni `simulator/wire.py` du côté des données.

Ce que #54 hérite, ligne à ligne :

| | Décision | Où ça vit | Ce que ça touche |
|---|---|---|---|
| **Échange** | quatre estampilles, RFC 5905 §8 ; N = 10 ; on garde le δ minimal | nouveau `CFG_SYNC` (0x06) + réponse `0x31`, 24 octets | `protocol.h`, `protocol.py` (build/parse), `config_server.cpp` |
| **Estampilles côté ESP** | T2 dans `poll()`, T3 avant `endPacket` | deux `micros()` | `config_server.cpp` |
| **Régime radio** | `WiFi.setSleep(false)` pendant l'épisode, remis après | firmware | croise #63 |
| **Cadence** | deux épisodes, aux deux bouts du take, dans l'enregistrement ; interpolation linéaire | hôte | rien sur le fil |
| **Ancre** | deux ancres dans `take.json`, jamais leur différence ; indivisible | `TakeMeta` + `TakeUpdate` | précédent : `onset_imu_s` / `onset_video_s` |
| **Marqueur d'époque** | **inutile**, à condition qu'un take sache dire « l'ESP a redémarré ici » | compteur de discontinuités de `TimeBase` dans le take, **ou** heartbeat enregistrable | croise #56 |
| **Estampille hôte** | `time.monotonic_ns()` pour T1/T4, socket dédiée, pas `EspConfigurator` | hôte | rien sur le fil |

Et une exigence que ce ticket ajoute et qui n'était pas dans #54 : **`ts_rx_us` est aujourd'hui une
horloge murale disciplinée par le réseau** (§ 1.3). Tant qu'elle l'est, la latence mesurée sur les
paquets de données porte les corrections de `timed` en plus de tout le reste. Changer cette colonne
est un choix de schéma CSV, avec la migration que ça implique — donc une décision de #54, pas une
correction de passage.

---

## Sources

**Code effectivement installé sur cette machine** (autorité pour « ce qui tourne sur la roue ») :
`~/.platformio/packages/framework-arduinoespressif32/` — `package.json`
(3.20017.241212+sha.dcc1105b) · `cores/esp32/esp_arduino_version.h` ·
`cores/esp32/esp32-hal-misc.c:166-173` (`micros`, `millis`) ·
`tools/sdk/esp32s3/include/esp_common/include/esp_idf_version.h` ·
`tools/sdk/esp32s3/include/hal/esp32s3/include/hal/systimer_ll.h:27`
(`SYSTIMER_LL_TICKS_PER_US = 16`) ·
`tools/sdk/esp32s3/include/hal/include/hal/systimer_hal.h:96-100`
(`systimer_hal_set_steps_per_tick`, sous `#if !SOC_SYSTIMER_FIXED_TICKS_US`) ·
`tools/sdk/esp32s3/include/soc/esp32s3/include/soc/rtc.h:157-158` (`RTC_XTAL_FREQ_40M`) ·
`tools/sdk/esp32s3/sdkconfig:7` (cible), `:988-991` (source d'horloge RTC = RC interne),
`:1147` (`# CONFIG_PM_ENABLE is not set`), `:1217` (`CONFIG_ESP_TIMER_IMPL_SYSTIMER=y`).
`~/.platformio/platforms/espressif32/boards/adafruit_feather_esp32s3_nopsram.json` (produit 5323).

**Amont, aux versions consultées :**
[arduino-esp32 2.0.17 — `esp32-hal-misc.c:166-169`](https://github.com/espressif/arduino-esp32/blob/2.0.17/cores/esp32/esp32-hal-misc.c#L166-L169) ·
[esp-idf v4.4.7 — `esp_timer_impl_systimer.c:65-70`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_timer/src/esp_timer_impl_systimer.c#L65-L70) ·
[esp-idf v4.4.7 — `hal/systimer_hal.c:148-179`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/hal/systimer_hal.c#L148-L179) ·
[esp-idf v4.4.7 — `soc/esp32s3/soc_caps.h:234-240`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/soc/esp32s3/include/soc/soc_caps.h#L234-L240)
(`SOC_SYSTIMER_FIXED_TICKS_US`)

**Fiches techniques Espressif, à leur numéro de version** (téléchargées et lues, pas résumées de
seconde main) :
[ESP32-S3-MINI-1 & MINI-1U Datasheet **v1.7**](https://documentation.espressif.com/esp32-s3-mini-1_mini-1u_datasheet_en.pdf)
— § 1.1 (quartz 40 MHz intégré, −40~85 °C), Fig. 2-1/2-2 p. 9 (schémas fonctionnels),
**Fig. 8-1 p. 39 et Fig. 8-2 p. 40 : `Y1 40MHz(±10ppm)`** ·
[ESP32-S3 Series Datasheet **v2.2**](https://documentation.espressif.com/esp32-s3_datasheet_en.pdf)
— § 4.1.3.3 (sources d'horloge), § 4.1.3.6 (System Timer, compteurs à 16 MHz) ·
[ESP32-S3 Technical Reference Manual **v1.8**](https://documentation.espressif.com/esp32-s3_technical_reference_manual_en.pdf)
— chapitre 11 § 11.2 et **§ 11.3 p. 635** (CNT_CLK = f<sub>XTAL</sub>/2,5 = 16 MHz) ·
[ESP Hardware Design Guidelines — esp32s3, Schematic Checklist](https://docs.espressif.com/projects/esp-hardware-design-guidelines/en/latest/esp32s3/schematic-checklist.html)
(« External Crystal Clock Source (Compulsory) » : 40 MHz obligatoire, ±10 ppm exigés — seule URL non
versionnée de cette note, les versions `v5.5`/`v5.4`/`v5.3`/`v4.4.7` répondant toutes 404)

**Algorithme, textes propriétaires :**
[RFC 5905 (NTPv4)](https://www.rfc-editor.org/rfc/rfc5905.txt) — § 8 « On-Wire Protocol », figure 15
p. 28 et les formules p. 29 (T1..T4, θ et δ), § 10 p. 39 (λ = δ/2 + ε), § 11.2.1 p. 43 (intervalle
de correction). *Le mot « asymmetry » n'apparaît pas dans le document ; vérifié.* ·
[RFC 4330 (SNTPv4)](https://www.rfc-editor.org/rfc/rfc4330.txt) — § 5 p. 14 (tableau T1..T4, mêmes
formules) ·
Flaviu Cristian, *Probabilistic clock synchronization*, Distributed Computing 3(3):146-158, 1989,
[doi:10.1007/BF01784024](https://doi.org/10.1007/BF01784024) — **sous péage** ; la formulation citée
ici vient de sa reprise par le même auteur, librement accessible :
[Cristian & Fetzer, *Probabilistic Internal Clock Synchronization*](https://www.cs.cornell.edu/courses/cs614/2004sp/papers/pics.pdf)
(SRDS 1994, version d'archive du 1<sup>er</sup> mai 2003), § 3.1 « Probabilistic Remote Clock
Reading », qui renvoie explicitement à [1] = l'article de 1989, et § 2.1 « Communication » pour la
distribution mesurée sur 200 000 aller-retours.

**Firmware** (`~/Desktop/IMU_project/PIO projects/260524_BNO_super_reports`, **lu, non modifié**) :
`platformio.ini` · `src/main.cpp:19-27` (ports, `DRAIN_BUDGET_MS`), `:93-124` (`loop`) ·
`src/config_server.cpp:12-18` (`poll`, un datagramme par tour), `:20-81` (`_handle`, `saveNVS` par
commande, `default:` sans ACK), `:90-141` (`_sendAck`, `micros()` à `:133`) ·
`src/report_manager.cpp:132-144` (`handleSensorEvent`), `:201-215` (`_sendPacket`, `micros()` à
`:208`), `:217-226` (heartbeat) · `src/protocol.h:38-45` (`HeartbeatPayload`), `:49-55` (`CfgType`),
`:59-73` (`DataHeader`, `CfgHeader`), `:107-125` (corps de l'ACK) · `git log` : `66068c1`
(introduction de `DRAIN_BUDGET_MS`).

**Dépôt courant** (**lu, non modifié**) :
`transport/protocol.py:57` (`DATA_HEADER`), `:146-254` (`parse_packet`, `ts_rx_us` à `:192`),
`:291-333` (`parse_ack`, en-tête sauté à `:298`) ·
`transport/esp_configurator.py:198-220` (`_send`, `_flush`), `:222-269` (`_recv_ack`) ·
`transport/udp_receiver.py:48-66` · `core.py:159-173` (`accept_live`), `:262-315`
(`processing_loop`) · `model/clock.py:41-42`, `:90-98`, `:119-160` · `config.py:97-126` (ports,
`CONFIG_ACK_TIMEOUT_S` et le « measured ~54 ms ») · `storage/csv_logger.py:51` (schéma) ·
`storage/session_manager.py:104-105` · `simulator/esp32.py:168`, `:213`, `:371-407` ·
`docs/research/cadence-rssi-esp32.md` (§ 2.2, le modem-sleep par défaut).

**Vérifications faites sur cette machine** (reproductibles) :
`/etc/ntp.conf` (`server time.apple.com.`) et `ps -ax` (`/usr/libexec/timed` actif) ·
`SO_TIMESTAMP` sur une socket UDP en loopback : `SOL_SOCKET` = 65535, `SCM_TIMESTAMP` = 2,
`struct timeval` de 16 octets, écart noyau → `recvmsg` de 23 / 58 / 62 µs sur trois datagrammes à
vide.
