# Quelles grandeurs de lien la pile WiFi de l'ESP32-S3 expose-t-elle, à quelle cadence, et à quel coût d'appel ?

Recherche pour le ticket [#60](https://github.com/Zaruitoga/conductor/issues/60), sous la carte
[#49](https://github.com/Zaruitoga/conductor/issues/49) « performances du lien WiFi en conditions
d'installation ». Elle débloque [#54](https://github.com/Zaruitoga/conductor/issues/54), qui arrête
le protocole permanent.
Recherche seulement : **aucun correctif n'est appliqué ici**, ni dans ce dépôt ni dans le firmware.

**Enjeu.** [#51](https://github.com/Zaruitoga/conductor/issues/51) a répondu pour le RSSI, et a
écarté au passage tout ce qu'il croisait — mais contre un critère unique : *cette grandeur
remplace-t-elle le RSSI pour lire la rotation de la roue ?* Contre ce critère-là, une grandeur qui
bat à 3 Hz ne vaut rien, et c'est ce qui a rangé les compteurs de pile et le CSI en impasse. Le
critère de #54 est autre : *qu'est-ce qui mérite un champ dans le protocole permanent ?* — et une
grandeur qui bat à 0,5 Hz peut parfaitement mériter une place dans le heartbeat si elle dit quelque
chose que rien d'autre ne dit. Ce ticket ne conclut donc rien sur la roue : il produit **la liste
complète**, chaque entrée portant sa cadence de rafraîchissement *réelle* (la leçon de #51 : la
cadence de lecture n'est pas celle d'écriture), son coût d'appel, et le mode qu'elle exige.

---

## Verdict

**La pile n'expose que six grandeurs de lien lisibles sans changer de comportement radio, et cinq
d'entre elles sont des réglages déguisés ou battent à la balise. La seule qui batte à notre cadence
d'émission est `esp_wifi_set_tx_done_cb` — API privée, mais dont la déclaration n'a pas bougé d'un
octet de l'IDF v4.3 à la v5.5, et que deux collaborateurs Espressif recommandent nommément sur le
traqueur. Son booléen reste aveugle au retard, et on sait maintenant de combien : le MAC réessaie
une trame jusqu'à trente-deux fois avant de le mettre à `false`. Ce que le callback porte vraiment,
ce n'est pas un taux d'échec — c'est une *date de fin d'émission par trame*, que rien d'autre ne
donne.**

Les sept points décisifs, et ce qu'il faut accepter avec :

| | Établi | Contrepartie |
|---|---|---|
| **1. La taille de l'inventaire** | Six grandeurs lisibles en mode station sans rien changer : RSSI de balise, RSSI moyenné (par événement de seuil), RSSI de déconnexion, mode PHY négocié, puissance d'émission maximale, TSF. Deux d'entre elles — puissance et mode PHY — sont des **réglages relus**, pas des mesures. | L'inventaire est court parce que la pile est fermée, pas parce que la radio ne mesure rien : le § 5 liste les grandeurs que le blob calcule et n'expose pas. |
| **2. `esp_wifi_set_tx_done_cb`, statut réel** | Déclarée dans `esp_private/wifi.h`, pas dans un `private_include` : le header **est installé** et s'inclut sans bricolage. Déclaration **identique au caractère près** de v4.3.7 à v5.5 (§ 2.1). Deux collaborateurs Espressif la recommandent sur le traqueur, nommés et datés. | Le header porte en tête « espressif customers are **not recommended** to use them […] otherwise you may get unexpected behavior!!! ». Et dans tout `espressif/esp-idf`, l'unique occurrence du symbole est sa propre déclaration : **aucun appelant**, aucun test, aucun exemple. |
| **3. Dans quelle tâche il tourne** | Dans **`ppTask`** — la tâche WiFi, cœur 0, priorité `configMAX_PRIORITIES − 2` = **23**, contre la priorité **1** de `loopTask` sur le cœur 1. Chaîne établie au désassemblage : `ppTask → ppProcTxDone → g_tx_done_cb_func` (§ 2.2). | Il existe un second chemin, direct depuis `lmacTxDone`, pris sous condition. Il ne change rien à un compteur ; il interdit d'y bloquer. |
| **4. Ce que `txStatus` vaut** | C'est **`(octet_de_statut == 1)`**. L'octet, lui, prend au moins sept valeurs distinctes selon la cause (succès, abandon après retransmissions courtes, longues, AMPDU, MSDU périmée…), toutes écrites dans `lmac.o`. Le booléen est un écrasement, pas la grandeur. | L'octet est à un offset d'une structure non documentée d'un blob fermé. Il ne se lit pas depuis le callback : il n'est pas dans ses arguments. |
| **5. Aveugle au retard, et le chiffre** | `lmacInit` pose les deux limites de retransmission à **32** (`lmacConfMib[20]` et `[21]`) — confirmé par un collaborateur Espressif en 2023 (« maximum value should not exceed 32 »). Une trame acquittée au 32ᵉ essai est un `txStatus == true`. | C'est exactement la signature que la reconnaissance de #49 a vue : perte nulle, retard groupé. Le booléen ne verrait rien de ce phénomène — #51 avait raison, on sait maintenant *de combien*. |
| **6. Ce que le callback porte quand même** | Il s'exécute **quand la trame a fini d'être émise**. Horodaté contre le `micros()` de `_sendPacket`, il donne un **délai d'émission par trame**, à la cadence d'émission, sans changer un bit du comportement radio. C'est la grandeur que ni le RSSI ni le booléen ne donnent, et que l'hôte ne peut pas déduire. | Qu'on puisse **rattacher** un rappel à *son* datagramme n'est pas établi : le callback reçoit `data` et `data_len`, et ce que `data` pointe n'est documenté nulle part. Mesure F, § 6 — c'est elle qui ouvre ou ferme toute la piste. |
| **7. `udp_errors`, déjà sur le fil** | Il compte les `sendto()` qui échouent sur une socket **non bloquante** (`WiFiUdp.cpp:178-190`), donc la perte **avant l'air** : huit tampons d'émission WiFi statiques (`sdkconfig:1229`), et `esp_wifi_internal_tx` qui refuse. C'est le pendant ESP exact de la question d'hôte de #59/#76. | Il est bien parsé, bien affiché — une tuile du panneau — mais il **n'entre dans aucun verdict** et n'est **jamais enregistré** (le heartbeat n'est pas dans `PACKET_FIELDS`). C'est un compteur cumulé depuis le démarrage : « 3 » ne dit pas *quand*. |

**Ce qui n'est pas vérifiable ici** : § 6, avec sept mesures au banc (D à K, la numérotation continuant
celle de #51).

---

## 0. La version qui fait foi

Le firmware est `~/Desktop/IMU_project/PIO projects/260524_BNO_super_reports`, `platformio.ini` :
`platform = espressif32`, `board = adafruit_feather_esp32s3_nopsram`, `framework = arduino`, sans
épinglage. Ce qui est **effectivement installé** sur cette machine fait foi :

| | Version | Source lue |
|---|---|---|
| arduino-esp32 | **2.0.17** | `cores/esp32/esp_arduino_version.h:22-26` |
| ESP-IDF embarqué | **v4.4.7** | `tools/sdk/esp32s3/include/esp_common/include/esp_idf_version.h:22-26` |
| cible | `esp32s3` | `tools/sdk/esp32s3/sdkconfig:7` |
| FreeRTOS | `configMAX_PRIORITIES = 25` | `tools/sdk/esp32s3/include/freertos/include/esp_additions/freertos/FreeRTOSConfig.h:81` |

**Vérifié cette fois plutôt que supposé** : `tools/sdk/esp32s3/include/esp_wifi/include/esp_private/wifi.h`
installé est **identique octet pour octet** au fichier du tag
[`esp-idf@v4.4.7`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_private/wifi.h)
(615 lignes, `diff` vide) — donc les numéros de ligne cités valent des deux côtés. La documentation
citée est la version [v4.4.7 / esp32s3](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/),
jamais `latest`. Là où une API plus récente est mentionnée, c'est dit explicitement et elle **n'est
pas dans la version installée**.

Options de compilation qui décident de plusieurs réponses ci-dessous, lues dans
`tools/sdk/esp32s3/sdkconfig` :

```
1228  CONFIG_ESP32_WIFI_TX_BUFFER_TYPE=0            ← tampons d'émission statiques
1229  CONFIG_ESP32_WIFI_STATIC_TX_BUFFER_NUM=8      ← huit, pas un de plus
1235  CONFIG_ESP32_WIFI_CSI_ENABLED=y               ← le CSI est compilé
1236  CONFIG_ESP32_WIFI_AMPDU_TX_ENABLED=y          ← ferme esp_wifi_internal_set_fix_rate (§ 3.5)
1242  CONFIG_ESP32_WIFI_TASK_PINNED_TO_CORE_0=y     ← la tâche WiFi est sur le cœur 0
1468  # CONFIG_LWIP_STATS is not set                ← aucun compteur lwIP n'existe (§ 3.6)
```

**Une part du WiFi est un blob fermé** : `libnet80211.a` et `libpp.a` dans `tools/sdk/esp32s3/lib/`.
Là où la réponse s'y trouve, elle est obtenue par **désassemblage du binaire livré**
(`xtensa-esp32s3-elf-objdump` / `-nm` / `-readelf`, toolchain PlatformIO). C'est littéralement le
code qui tourne sur la roue, mais ce n'est pas du source.

**Et il n'y a pas d'autre recours** : le
[*ESP32-S3 Technical Reference Manual*](https://documentation.espressif.com/esp32-s3_technical_reference_manual_en.pdf)
**ne contient aucun chapitre sur le MAC ni sur la bande de base WiFi.** Le sommaire va de
« 1 Processor Instruction Extensions » à « 39 On-Chip Sensors », et la seule entrée où « Wi-Fi »
apparaît est **§ 7.2.4.3 « Wi-Fi and Bluetooth LE Clock »** — une horloge, pas un registre de radio
(sommaire extrait des signets du PDF, 1 150 titres, une seule occurrence). Aucun registre de
compteur de retransmission, de RSSI ou de plancher de bruit n'est documenté par le fabricant. C'est
ce qui fait que, pour ce ticket, **le banc n'est pas une précaution : c'est la seule instance
d'appel**.

---

## 1. L'inventaire

Mode **station**, connecté, ce que la roue est. Les lignes `esp_wifi.h` et `esp_wifi_types.h` sont
celles du SDK installé (identiques à v4.4.7 amont). « Cadence de rafraîchissement » est celle de
*l'écriture* de la valeur, jamais celle à laquelle on peut l'appeler.

| Grandeur | API (`header:ligne`) | Ce qu'elle mesure | Rafraîchissement | Coût d'un appel | Mode exigé | Verdict protocole |
|---|---|---|---|---|---|---|
| **RSSI de la dernière balise** | `esp_wifi.h:1400` `esp_wifi_sta_get_rssi` | puissance reçue de la trame de gestion la plus récente, **brute** | à la balise : **9,8 Hz** au plafond, **3,3 Hz** en modem-sleep DTIM 3 (#51 § 2) | un verrou récursif, trois lectures | aucun | **déjà là** (heartbeat, 0,5 Hz) — au bon endroit ; remplacer l'appel `WiFi.RSSI()` (#51 § 7) |
| RSSI, même octet par un autre chemin | `esp_wifi.h:505` `esp_wifi_sta_get_ap_info` | identique | identique | `zalloc(24)` + mutex + **post inter-cœurs bloquant sans timeout** + `free` (#51 § 3.2) | aucun | **non** — jamais à cadence |
| **RSSI moyenné** (13/16 · 3/16) | aucune lecture ; seuil seul : `esp_wifi.h:1197` `esp_wifi_set_rssi_threshold` → `WIFI_EVENT_STA_BSS_RSSI_LOW`, `esp_wifi_types.h:748` | le champ voisin (`bss[164]`) que #51 a trouvé et que rien n'expose en lecture | à la balise, et l'événement est **à un coup** (il faut réarmer) | l'armement seul ; l'événement est poussé | aucun | **non** comme champ ; éventuellement comme *réglage* d'alerte |
| RSSI de déconnexion | `esp_wifi_types.h:691` `wifi_event_sta_disconnected_t.rssi` | le RSSI au moment où le lien tombe | une fois par déconnexion | poussé | aucun | **oui, si** #54 veut dater les coupures — un octet dans un futur paquet d'événement, pas dans le flux |
| **RSSI par trame reçue** | `esp_wifi_types.h:387` (`wifi_pkt_rx_ctrl_t.rssi`) | la trame elle-même | = trafic **reçu** sur le canal | callback dans la tâche WiFi, par trame | **promiscuous** ou **CSI** | **non** — comportement radio, pas champ (§ 5.1) |
| Plancher de bruit par trame reçue | `esp_wifi_types.h:416` (branche S3) | bruit RF au moment de la réception | idem | idem | idem | **non** — même mur |
| Plancher de bruit courant | *aucun header* — `wDev_GetNoiseFloor` (`libpp/wdev.o`, symbole global) | valeur **mise en cache** dans `wDevCtrl[47]`, relue du matériel par un timer interne de gestion d'énergie (`pm_noise_check`) | **non documentée** — mesure G | deux lectures, aucun verrou | aucun | **non** — offset de blob, pas un contrat |
| Débit PHY / MCS de la trame reçue | `esp_wifi_types.h:388` (`rate`), `:392` (`mcs`), `:394-401` (`cwb`, `sgi`, `stbc`, `aggregation`) | modulation de ce qui **arrive** | = trafic reçu | par trame | **promiscuous** ou **CSI** | **non** — et c'est la réception, pas notre émission |
| **Mode PHY négocié** | `esp_wifi.h:1386` `esp_wifi_sta_get_negotiated_phymode` | `LR / 11B / 11G / HT20 / HT40` — cinq valeurs | à la (ré)association ; c'est une **capacité négociée**, pas un débit instantané | `esp_wifi_ipc_internal` : **aller-retour inter-cœurs bloquant** | aucun | **oui, dans le heartbeat** — un octet, et il dit contre quoi une passe s'interprète. **Pas** dans le flux |
| Canal / largeur négociés | `esp_private/wifi.h:471`, `:482` | canal primaire/secondaire et largeur effectivement retenus | à la (ré)association | non mesuré ; privé | aucun | **à ranger avec le mode PHY** — même fait, même cadence |
| Puissance d'émission maximale | `esp_wifi.h:965` `esp_wifi_get_max_tx_power` | `phy_get_most_tpw()` — le **plafond** réglé, pas la puissance employée | ne change que si on l'écrit | direct, aucun verrou | aucun | **non, ce n'est pas une mesure** — c'est un réglage relu ; il a sa place dans le dump d'état, pas dans le flux |
| **TSF** (horloge MAC de l'AP) | `esp_wifi.h:1136` `esp_wifi_get_tsf_time` | compteur µs du MAC, asservi aux balises | continu (matériel) ; la doc prévient : « enabling power save may cause the return value inaccurate, except WiFi modem sleep » | depuis une ISR : direct ; **sinon `zalloc(24)` + ioctl bloquant** | aucun | **à renvoyer à [#52](https://github.com/Zaruitoga/conductor/issues/52)** — c'est une horloge, pas une qualité de lien |
| **Fin d'émission par trame** | `esp_private/wifi.h:550` (type), `:562` (enregistrement) | `txStatus` = « octet de statut == 1 » ; **et l'instant où la trame quitte le MAC** | **notre cadence d'émission**, 100–200 Hz | enregistrement : deux appels, aucun verrou. Callback : dans `ppTask`, prio 23, cœur 0 | aucun | **le seul candidat réel** — mais pour la **date**, pas pour le booléen (§ 2.4) |
| Nombre de retransmissions d'une trame | **n'existe nulle part** (§ 3.3) | — | — | — | — | **hors d'atteinte** |
| Compteurs LMAC / HMAC | `esp_wifi.h:1181` `esp_wifi_statis_dump` ; globaux `g_lmac_cnt` (192 o), `g_hmac_cnt` (64 o) | trames émises/reçues par catégorie, erreurs FCS, files pleines | écriture continue sur le chemin de données | le dump **n'écrit que dans le log série** et renvoie 0 | aucun | **non** — et les globaux sont des offsets non documentés (§ 3.4) |
| RTT / distance (FTM) | `esp_wifi.h:1213` `esp_wifi_ftm_initiate_session` → `esp_wifi_types.h:775-782` (`rtt_raw`, `rtt_est` en ns, `dist_est` en cm) et `:763-771` (par trame : `rssi`, `rtt` en ps, `t1..t4`) | un vrai aller-retour mesuré, plus un RSSI par trame FTM | par **rafale** demandée (`frm_count` ≤ 64, `burst_period` en centaines de ms) | une session, un événement | aucun — mais **l'AP doit être répondeur FTM** | **suspendu au banc** — mesure H le tranche en une passe |
| CSI | `esp_wifi.h:1048` `esp_wifi_set_csi_rx_cb` | réponse du canal par sous-porteuse | = trafic **reçu** | lourd | **CSI** (compilé : `sdkconfig:1235`) | **non** — mur structurel de #51 § 4.2 : on ne reçoit presque rien |
| Compteurs lwIP / netif | — | — | — | — | — | **inexistants** : `CONFIG_LWIP_STATS` n'est pas posé (`sdkconfig:1468`) |
| **`udp_errors`** | firmware : `report_manager.cpp:213` ; fil : `protocol.h:41` | `sendto()` refusé par la pile — perte **avant l'air** | à chaque refus ; transporté à 0,5 Hz | déjà payé | aucun | **déjà là, et sous-exploité** (§ 4) |

Trois entrées volontairement hors tableau, pour mémoire. `esp_wifi_ap_get_sta_list`
(`esp_wifi.h:862`) donne un RSSI **moyenné** par station (`wifi_sta_info_t.rssi`, `:308`) — mode
point d'accès, ce que la roue n'est pas. `esp_wifi_sta_get_aid` (`:1376`) est constant sur la durée
d'une association. Et le statut d'émission d'ESP-NOW (`esp_now.h:56-59`,
`ESP_NOW_SEND_SUCCESS/FAIL`) est exactement le même booléen que `txStatus`, au prix d'un changement
de transport complet. *(En IDF 5.x — **pas** la version installée — le callback de réception
d'ESP-NOW reçoit un `esp_now_recv_info_t` porteur du `rx_ctrl`, donc un RSSI par trame reçue sans
mode promiscuous. Cela ne nous servirait pas davantage : nous ne recevons rien.)*

---

## 2. `esp_wifi_set_tx_done_cb`, au complet

C'est la question que #60 pose nommément, parce que #51 l'avait classée seconde sans en donner le
prix.

### 2.1 Le statut de l'API : privée, mais stable et recommandée

**Elle n'est pas dans un `private_include`.** Elle est dans
`tools/sdk/esp32s3/include/esp_wifi/include/esp_private/wifi.h`, c'est-à-dire un répertoire du
chemin d'inclusion installé : `#include "esp_private/wifi.h"` compile sans rien bricoler. La
distinction compte, parce qu'un `private_include` d'IDF n'est pas exporté aux composants clients,
et celui-ci l'est.

Ce que dit le header lui-même, en tête de fichier (`esp_private/wifi.h:7-16`) :

> « All the APIs declared here are internal only APIs, it can only be used by espressif internal
> modules, such as SSC, LWIP, TCPIP adapter etc, **espressif customers are not recommended to use
> them.** If someone really want to use specified APIs declared in here, please contact espressif
> AE/developer to make sure you know the limitations or risk of the API, **otherwise you may get
> unexpected behavior!!!** »

C'est la seule déclaration de stabilité qui existe, et elle est négative. En face, trois faits
mesurés :

**(a) La déclaration n'a pas bougé.** Fichier récupéré à chaque tag et comparé :

| IDF | `esp_private/wifi.h` | ligne du `typedef` | signature |
|---|---|---|---|
| v4.0.4 · v4.1.4 · v4.2.5 | présent | — | **absente** |
| **v4.3.7** | présent | 550 | `void (*)(uint8_t ifidx, uint8_t *data, uint16_t *data_len, bool txStatus)` |
| **v4.4.7** *(la nôtre)* | présent | 550 | identique |
| v5.0.7 | présent | 550 | identique |
| v5.1.5 · v5.2.3 | présent | 584 | identique |
| v5.3.2 · v5.4.1 · v5.5 | présent | 580 | identique |

Apparue en v4.3, **inchangée au caractère près sur huit versions mineures et cinq ans**, la faute de
frappe `@breif` comprise. Ce n'est pas une garantie — c'est un historique, et c'est tout ce qu'on
peut opposer à un avertissement de header.

**(b) Personne ne l'appelle dans le source ouvert.** Recherche de code sur
`repo:espressif/esp-idf` : **une seule occurrence du symbole dans tout le dépôt**, sa propre
déclaration. Aucun appelant, aucun test, aucun exemple. Idem dans l'arbre installé :
`grep -rn esp_wifi_set_tx_done_cb ~/.platformio/packages/framework-arduinoespressif32` ne trouve que
les quatre copies du header (esp32, esp32s2, esp32s3, esp32c3), **toutes à la ligne 562**. La
fonction n'existe que pour le blob et pour qui va la chercher.

**(c) Deux collaborateurs Espressif la recommandent, nommés et datés.** Le critère de méthode du
ticket — « une issue ne compte que si un mainteneur répond » — est ici satisfait, contrairement aux
deux fils sur le RSSI de #51 où il ne l'était pas :

- [esp-idf#7904](https://github.com/espressif/esp-idf/issues/7904) « How to tell a udp packet was
  sent? » : `xueyunfei998` (2022-06-07) répond « Included in this header file in your main.c file,
  `#include "esp_private/wifi.h"` […] call this function to register a data sending completion
  callback function. `esp_wifi_set_tx_done_cb` ». *Son `author_association` est `NONE`*, donc à
  ranger comme corroboration, pas comme autorité — mais le fil est **clos par `Alvin1Zhang`
  (`COLLABORATOR`, 2022-07-13)** sans rectification.
- [esp-idf#9605](https://github.com/espressif/esp-idf/issues/9605) « Method to know if the radio has
  stopped transmitting » : **`MaxwellAlan` (`COLLABORATOR`, 2022-08-24)** — « Yes, you can try this
  callback, this callback will trigger when your pkts tx success. » C'est **la** phrase officielle,
  et noter comme elle est étroite : *tx success*. Elle ne dit rien de l'échec, rien des
  retransmissions, rien des trames internes.
- Dans le même fil, l'auteur (`doragasu`, 2022-08-25) rapporte avoir vérifié que le callback est
  invoqué **une fois par appel** de `esp_wifi_80211_tx` et que les trames internes (balises en mode
  AP) ne le déclenchent pas. Rapport d'utilisateur, non confirmé par un mainteneur — mais il
  concorde avec le désassemblage (§ 2.2), où l'appel est conditionné par un drapeau du descripteur.

### 2.2 Dans quelle tâche il tourne

L'enregistrement est direct et gratuit. `esp_wifi_set_tx_done_cb`
(`libnet80211.a:ieee80211_api.o`, vingt-six octets) :

```
entry
  wifi_init_completed()            ; si faux -> retourne l'erreur
  ic_register_pp_tx_done_cb(cb)    ; libpp/if_hwctrl.o
     └─ ppRegisterTxDoneUserActionCallback(cb)   ; libpp/pp.o
          └─ g_tx_done_cb_func = cb ; return 0
retw
```

Pas d'ioctl, pas de verrou, pas d'allocation — l'exact contraire de `esp_wifi_sta_get_ap_info`.

La chaîne d'appel, établie en croisant les relocations de `libpp.a` avec la table des sections :

```
ppTask                       libpp/pp.o, section .wifi0iram.9   <- la tâche WiFi
  └─ ppProcTxDone()          libpp/pp.o, .text.ppProcTxDone
       ├─ ppDequeueTxDone_Locked()        ; boucle sur les descripteurs terminés
       ├─ si (eb->flags & 8) et g_tx_done_cb_func :
       │     cb(ifidx, data, &data_len, txStatus)      <<<< ici
       └─ esf_buf_recycle()
```

`ppTask` est bien le symbole de `.wifi0iram.9` (`readelf` : `ppTask` → section 293 =
`.wifi0iram.9`), et `ppProcTxDone` y est appelé. `ppTask` est créée dans `pp_create_task` par
`g_osi_funcs_p->_task_create_pinned_to_core` (offset 144 de `wifi_osi_funcs_t`) avec la priorité
`g_osi_funcs_p->_task_get_max_priority() − 2` (offset 168), soit **23** avec
`configMAX_PRIORITIES = 25`, sur le cœur donné par `config_get_wifi_task_core_id()` — **0**
(`sdkconfig:1242`, et `esp_wifi.h:191-193` `WIFI_TASK_CORE_ID`).

En face, `loopTask` est créée priorité **1** sur le cœur **1**
([`cores/esp32/main.cpp:71`](https://github.com/espressif/arduino-esp32/blob/2.0.17/cores/esp32/main.cpp#L71)).
Donc **le callback tourne sur l'autre cœur, vingt-deux crans au-dessus de `loop()`**. Conséquence
pratique, et c'est la seule règle d'écriture qui en découle : le callback ne doit rien faire d'autre
qu'écrire dans des `volatile` (ou des atomiques), jamais prendre un verrou ni appeler `Serial`.

Un second chemin existe : `lmacTxDone` appelle `ppProcTxDone` **directement** (et non par la file)
quand le bit 6 du mot d'état du descripteur est posé. `lmacTxDone` est elle-même atteinte depuis
`lmacRecycleMPDU` / `lmacDiscardMSDU`, sous `lmacProcessTxComplete`, que `ppTask` appelle aussi :
le contexte reste donc la tâche WiFi dans les cas lus. Que ce soit vrai de **tous** les chemins
n'est pas établi — mesure D.

### 2.3 Ce que coûte le callback à 100–200 paquets/s

Trois coûts à séparer.

- **L'enregistrement** : deux appels et un rangement de pointeur, une fois pour toutes. Nul.
- **Le chemin d'appel dans le blob** : dans `ppProcTxDone`, quatre instructions (un `l32r`, un
  `l32i`, un test de nullité, un `l8ui`) s'ajoutent par descripteur *quand un callback est
  enregistré*, plus le `callx8`. Le reste de la fonction s'exécutait déjà.
- **Le corps du callback** : entièrement à notre charge. Un `uint32_t` incrémenté et un
  `esp_timer_get_time()` rangé dans un tableau circulaire sont de l'ordre de la microseconde. À
  200 Hz, c'est 0,02 % d'un cœur — mais c'est du temps pris à la tâche qui pilote la radio, donc la
  question n'est pas la moyenne mais le **pire cas**, et c'est la mesure J.

Il n'y a **aucun coût par appel côté `loop()`** : rien n'est appelé depuis `loop()`. C'est la
différence structurelle avec toutes les autres grandeurs du tableau, et c'est ce qui range cette
API à part : les autres se *lisent* (et le prix est un aller-retour inter-cœurs), celle-ci se
*reçoit*.

### 2.4 Ce que `txStatus` distingue, et ce qu'il écrase

**Le booléen est calculé, pas transporté.** Dans `ppProcTxDone`, juste avant l'appel :

```
a13 = octet[desc + 19]        ; le statut réel
a8  = a13 - 1
a13 = 1
movnez a13, 0, a8             ; si a8 != 0 alors a13 = 0
...
callx8 cb                     ; txStatus = a13 = (octet == 1)
```

Donc **`txStatus == (octet_de_statut == 1)`**. Et l'octet, lui, prend plusieurs valeurs, toutes
écrites dans `libpp/lmac.o` — recherche exhaustive du motif `s8i …, 19` sur `lmac.o`, `pp.o`,
`trc.o`, `rate_control.o` et `hal_mac_tx.o` :

| valeur | écrite par | ce que c'est |
|---|---|---|
| **1** | `lmacProcessTxSuccess` (`.text…:0xfa`) et `lmacRecycleMPDU` (`.wifi0iram.15:0x14`) | **succès** — la seule valeur qui donne `true` |
| 2 | `lmacEndFrameExchangeSequence` (`.wifi0iram.17:0x1d9`) | fin d'échange, cause distincte |
| 3 | idem (`:0x20b`) | idem, autre cause |
| 7 | idem (`:0x2af`) | idem, autre cause |
| 8 | `lmacEndRetryAMPDUFail` (`.text…:0x7c`) | **abandon d'un AMPDU après retransmissions** |
| 10 | `lmacEndFrameExchangeSequence` (`:0xbc`) | idem |
| *variable* | `lmacDiscardMSDU` (`.text…:0xe9`) | rejet (MSDU périmée, file vidée…) |

**La réponse à la question du ticket est donc : non — pas dans le callback.** L'information qui
distinguerait l'abandon après retransmissions du reste *existe* et est écrite à l'octet 19 du
descripteur, mais `wifi_tx_done_cb_t` ne reçoit ni le descripteur ni cet octet : ses quatre
arguments sont `ifidx`, `data`, `&data_len`, `txStatus`. Un champ de protocole adossé à cet octet
supposerait de le lire à un offset d'une structure non documentée d'un blob fermé — exactement ce
que #51 a refusé pour `trc[3]`, et pour la même raison.

Et la question symétrique — « un succès au premier essai se distingue-t-il d'un succès au
septième ? » — se referme sur un chiffre. `lmacInit` (`libpp/lmac.o`, `.text.lmacInit:0x48-0x4d`) :

```
movi.n a8, 32
s8i    a8, a2, 21      ; lmacConfMib[21] = 32   <- short retry limit
s8i    a8, a2, 20      ; lmacConfMib[20] = 32   <- long retry limit
```

Ce sont bien les deux mêmes octets que `esp_wifi_internal_set_retry_counter(src, lrc)` écrit
(`.text.esp_wifi_internal_set_retry_counter` : `s8i a2,a8,21` puis `s8i a3,a8,20`), et
`lmacReachShortLimit` / `lmacReachLongLimit` les comparent à un compteur passé en argument.
Corroboration par un mainteneur : sur
[esp-idf#12070](https://github.com/espressif/esp-idf/issues/12070) « Configurable TX retry count for
Wi-Fi data frames », **`xuxiao111` (`COLLABORATOR`, 2023-08-28)** écrit :

> « you can use this internal API `int esp_wifi_internal_set_retry_counter(int src, int lrc)` to set
> the TX retry number at runtime. And the parameter "src" and "lrc" means short-frame retry and
> long-frame retry. **The maximum value should not exceed 32.** »

**Le MAC réessaie donc jusqu'à trente-deux fois avant de renoncer**, et `txStatus` reste `true`
pour les trente-deux. Sur un lien qui, d'après la reconnaissance de #49, ne perd rien et retarde par
épisodes, un compteur d'échecs passerait sa vie à zéro. Le verdict de #51 tient, avec son ordre de
grandeur.

**Ce qui survit — et c'est la trouvaille de ce ticket.** Le callback ne dit pas *comment* la trame
est partie, mais il dit **quand elle a fini de partir**. Le firmware connaît déjà l'instant où il l'a
remise à la pile (`_sendPacket` appelle `micros()` pour `hdr.ts_esp_us`,
`report_manager.cpp:208`) ; la différence est le **temps passé dans la file d'émission et dans
l'air, retransmissions comprises**, par trame, à 100–200 Hz, sans mode radio et sans rien changer au
comportement. C'est la grandeur dont #49 dit qu'elle est le sujet — *du retard, pas de la perte* —
et c'est la seule que l'hôte ne peut pas déduire : un retard mesuré côté hôte mélange la file de
l'ESP, l'air et la réception ; celui-ci isole les deux premiers. Elle ne tient qu'à une chose, non
vérifiée : pouvoir rattacher un rappel à *son* datagramme (mesure F).

---

## 3. Le débit PHY, et le nombre de retransmissions

### 3.1 Le mode PHY négocié : grossier, et cher à lire

`esp_wifi_sta_get_negotiated_phymode` (`esp_wifi.h:1386`) remplit un `wifi_phy_mode_t`
(`esp_wifi_types.h:362-370`), **cinq valeurs** : `LR`, `11B`, `11G`, `HT20`, `HT40`. Ce n'est ni un
MCS, ni un débit instantané : c'est ce sur quoi les deux parties se sont entendues à l'association.
Il ne change qu'à une réassociation.

Le désassemblage confirme le coût annoncé par #51 § 4.3 :

```
esp_wifi_sta_get_negotiated_phymode(phymode)
  param = { handler = esp_wifi_sta_get_negotiated_phymode_local, arg = phymode, blocking = 1 }
  esp_wifi_ipc_internal(&param)        ; <- appel inter-cœurs, bloquant
```

À 0,5 Hz dans le heartbeat, c'est indolore et ça dit quelque chose : si une passe est enregistrée en
`11G` et la suivante en `HT20`, les deux ne sont pas comparables, et rien d'autre ne le dirait.
À cadence, c'est le piège du § 3.2 de #51.

`esp_wifi_internal_get_negotiated_channel` et `…_get_negotiated_bandwidth`
(`esp_private/wifi.h:471`, `:482`) sont le même fait sous un autre angle, et privées.

### 3.2 Le débit d'une trame : seulement en réception

`wifi_pkt_rx_ctrl_t` porte `rate` (5 bits, « only valid for non HT(11bg) packet »,
`esp_wifi_types.h:388`), `sig_mode`, `mcs` (7 bits, `:392`), `cwb`, `sgi`, `stbc`, `aggregation`,
`ampdu_cnt`. C'est complet — et c'est la modulation de ce qui **arrive**. Nous n'arrivons à rien :
un STA qui émet 100–200 paquets/s d'UDP unidirectionnel ne reçoit que des balises et des ACK. Le
mur est celui de #51 § 4.2, inchangé par le nouveau critère.

### 3.3 Le nombre de retransmissions d'une trame : nulle part

Recherche complète, et le résultat est un **non** net :

- **aucune API publique** : rien dans `esp_wifi.h` ;
- **aucune API privée** : rien dans `esp_private/wifi.h` — on y trouve le *réglage* de la limite
  (`esp_wifi_internal_set_retry_counter`, confirmé par `xuxiao111`, § 2.4) mais aucun lecteur ;
- **pas d'accesseur non déclaré non plus** : le balayage des symboles globaux de `libnet80211.a`
  (989 symboles) et `libpp.a` (755) ne donne, pour tout ce qui ressemble à une lecture de retry,
  que `lmacReachShortLimit` / `lmacReachLongLimit`, qui **comparent** un compteur reçu en argument
  à la limite, et `esp_wifi_internal_get_mib` (`libpp/lmac.o`), qui n'est qu'un
  `memcpy(dst, &lmacConfMib, 48)` — c'est-à-dire la **configuration** (limites, seuil RTS, durée de
  vie MSDU), pas des compteurs. Cette fonction n'est déclarée dans **aucun** header installé
  (vérifié : `grep -rn esp_wifi_internal_get_mib` sur tout `tools/sdk/esp32s3/include` ne donne
  rien) ;
- **rien dans le dump de statistiques non plus** : voir § 3.4 ;
- **rien dans le callback** : `txStatus` est un booléen (§ 2.4).

Le compteur *existe* — il est passé à `lmacReachShortLimit` — il vit dans le descripteur de trame
et meurt avec lui. **Aucune API, publique, privée ou non déclarée, ne le fait sortir.**

Une piste demeure, et elle est fragile : le callback reçoit `data`, un pointeur vers la trame émise.
Le bit *Retry* du champ Frame Control 802.11 (bit 11) est posé par le matériel sur une
retransmission. Si `data` pointe bien l'en-tête MAC — ce qui n'est documenté nulle part — on aurait
un **booléen « cette trame a été retransmise au moins une fois »**, ce qui n'est pas un compte mais
n'est pas rien. La mesure F le dit en même temps qu'elle dit si le rattachement est possible.

### 3.4 `esp_wifi_statis_dump` : confirmé impasse, et on sait maintenant ce qu'il contient

#51 § 4.3 l'avait classé sans regarder ce qu'il imprime. Le désassemblage de
`.text.esp_wifi_statis_dump` confirme le verdict — deux appels, `dbg_lmac_statis_dump(modules)` et
`dbg_hmac_statis_dump(modules)`, puis `return 0`, **aucune sortie structurée** — et les chaînes des
deux objets de débogage disent ce qui serait imprimé :

- `libpp/pp_debug.o`, depuis `g_lmac_cnt` (**192 octets**) : `tx_all`, `mdpu`, `ampdu`, `frag`,
  `ctrl`, `mgmt` côté émission ; `rx_end`, `rx_suc`, `rxcckerr`, `rxbufbk`, `fifofull`, `rx_fcs`,
  `rx_abort`, `rx_agc`, `ofdmerr`, `afull` côté réception ; plus des compteurs de gestion d'énergie
  (`g_pm_cnt`, 72 octets : `beacon`, `tbtt`, `waket`, `sleept`, `bcndly`, `null0`, `null1`…) ;
- `libnet80211/ieee80211_debug.o`, depuis `g_hmac_cnt` (**64 octets**) : `bcn/probe`, `mgmt`,
  `data`, `auth`, `amsdu`, `action`, `deauth`, `assoc`, `ap_tx`, `sta_tx`, `reorder`, `psq_uc`…

**Aucun compteur de retransmission dans l'une ni l'autre liste** — ce qui est la réponse à la
question 3 par un troisième chemin. Les compteurs sont bien écrits en continu sur le chemin de
données (relocations vers `g_lmac_cnt` depuis `ieee80211_output.o`, `ieee80211_ht.o`, `esf_buf.o`,
`lmacDiscardMSDU`…), et `g_pm_cnt` est un mouchard intéressant pour le modem-sleep de #51 § 2.2 —
mais y accéder voudrait dire déclarer `extern` un tableau d'offsets non documentés d'un blob fermé.
Même refus que pour `ic_get_rssi`. **Verdict inchangé**, avec l'inventaire de ce qu'on renonce à
lire.

### 3.5 Figer le débit : fermé par la configuration installée

`esp_wifi_internal_set_fix_rate(ifx, en, rate)` (`esp_private/wifi.h:268`) permettrait d'émettre à
débit constant — ce qui rendrait un retard attribuable, l'adaptation de débit cessant d'être une
variable cachée. Le header ferme la porte lui-même :

> « `ESP_ERR_NOT_SUPPORTED` : do not support to set fixed rate **if TX AMPDU is enabled** »

Et `CONFIG_ESP32_WIFI_AMPDU_TX_ENABLED=y` (`sdkconfig:1236`). Le SDK d'arduino-esp32 étant livré
précompilé, l'éteindre n'est pas une ligne de code mais une recompilation du framework. **À écarter
pour cette carte** — mais c'est le genre de chose que #54 doit savoir avant de promettre un
réglage. (`esp_wifi_config_80211_tx_rate`, `esp_wifi.h:1351`, est réservé aux trames brutes
`esp_wifi_80211_tx`, que nous n'employons pas.)

### 3.6 Les compteurs lwIP : compilés hors du binaire

`# CONFIG_LWIP_STATS is not set` (`sdkconfig:1468`). Toute la famille `lwip_stats` — paquets
abandonnés par manque de `pbuf`, files pleines, erreurs par protocole — **n'existe pas** dans ce
binaire. Il n'y a donc rien à lire entre `sendto()` et le pilote WiFi, et c'est précisément ce qui
donne sa valeur à `udp_errors` : c'est le seul témoin de cet étage.

---

## 4. Ce que le heartbeat porte déjà, et que personne n'exploite : `udp_errors`

### 4.1 Qui l'incrémente, exactement

Côté firmware, une ligne (`src/report_manager.cpp:201-214`) :

```cpp
  _udp.beginPacket(_host, _port);
  _udp.write((const uint8_t*)&hdr, sizeof(hdr));
  _udp.write(payload, len);
  if (_udp.endPacket() == 0) _udpErrors++;
  _packetsSent++;
```

Et `endPacket()` renvoie 0 dans un seul cas
([`libraries/WiFi/src/WiFiUdp.cpp:178-190`](https://github.com/espressif/arduino-esp32/blob/2.0.17/libraries/WiFi/src/WiFiUdp.cpp#L178-L190)) :

```cpp
int WiFiUDP::endPacket(){
  ...
  int sent = sendto(udp_server, tx_buffer, tx_buffer_len, 0, ...);
  if(sent < 0){ log_e("could not send data: %d", errno); return 0; }
  return 1;
}
```

Deux précisions qui changent la lecture. La socket est **non bloquante** —
`fcntl(udp_server, F_SETFL, O_NONBLOCK)` dans `beginPacket()`, `WiFiUdp.cpp:157` — donc un `sendto`
qui ne peut pas être servi **échoue** au lieu d'attendre. Et pour l'UDP il n'y a pas de file
intermédiaire : lwIP appelle le pilote dans le même appel, si bien que **les erreurs du pilote
remontent** jusqu'à `sendto`. Ce que le pilote peut refuser est documenté
(`esp_private/wifi.h:122-144`, `esp_wifi_internal_tx`) : `ESP_ERR_NO_MEM`,
`ESP_ERR_WIFI_NOT_ASSOC`, `ESP_ERR_WIFI_TX_DISALLOW`, `ESP_ERR_WIFI_POST`…

Donc **`udp_errors` compte les datagrammes que l'ESP n'a jamais émis** — épuisement des tampons
d'émission (il y en a **huit**, statiques : `sdkconfig:1228-1229`), lien momentanément non associé,
émission interdite. Ce n'est **pas** de la perte dans l'air, et c'est exactement pour ça qu'il est
précieux : c'est le seul témoin, côté ESP, de l'étage où #59 a identifié le trou symétrique côté
hôte (`SO_RCVBUF` non réglé, `QueueFull` non compté, ticket #76). Un trou de `seq` vu par l'hôte
avec `udp_errors` qui bouge au même moment n'est pas le même fait qu'un trou avec `udp_errors`
immobile — et aujourd'hui, personne ne peut faire cette comparaison.

Noter aussi ce qu'il **ne** compte pas : `_packetsSent` est incrémenté **même quand `endPacket()`
a échoué**. `packets_sent` est donc « datagrammes tentés », pas « émis ». La vérité terrain de #49
est `packets_sent − udp_errors`, et non `packets_sent`.

### 4.2 Ce que l'hôte en fait aujourd'hui

Contrairement à ce que l'énoncé de #60 laisse entendre, il **n'est pas** ignoré de bout en bout —
il est ignoré là où ça compte :

| étape | ce qui se passe | fichier |
|---|---|---|
| décodage | parsé du `<IIIiff` | `transport/protocol.py:243-249` |
| santé | recopié dans `health.heartbeat.udp_errors` | `transport/esp_health.py:82` |
| **verdict** | **rien** — `_assess(hb_age_ms, online, streams)` ne le reçoit pas | `transport/esp_health.py:61` |
| panneau | tuile « Erreurs UDP », ton `warn` si > 0 | `api/static/js/panels/health.js:46-48` |
| modèle | **rien** | — |
| **enregistrement** | **rien** — le heartbeat n'est pas dans `PACKET_FIELDS`, donc jamais écrit au CSV | `transport/protocol.py:138-141` |

Trois conséquences pour #54 et #56 :

1. **Il est invisible dans un take.** C'est la décision documentée (« `PACKET_FIELDS` fait office de
   liste des types enregistrables »), et elle transforme en *direct seulement* — au sens de la
   taxonomie à cinq états de #59 — le seul témoin de la perte côté ESP.
2. **C'est un cumul depuis le démarrage.** Une tuile qui affiche « 3 » ne dit pas si les trois
   erreurs datent d'il y a une heure ou de la prise en cours. Enregistré, il deviendrait un *taux*
   par différence entre deux heartbeats — ce qui est la seule forme exploitable.
3. **Il ne coûte rien de plus.** Il est déjà dans la charge utile, déjà sur le fil, déjà à 0,5 Hz.
   La question que #54 doit trancher n'est pas « faut-il l'ajouter » mais « faut-il enregistrer le
   heartbeat » — et c'est #56.

Ce qu'il **ne dira jamais** : rien sur le retard. Un datagramme accepté par `sendto` puis retardé de
120 ms dans la file d'émission n'y laisse aucune trace. C'est le § 2.4.

---

## 5. Les quatre verdicts de #51, réexaminés sous le nouveau critère

| | Verdict de #51 (« remplace-t-il le RSSI pour lire la rotation ? ») | Verdict de #60 (« mérite-t-il un champ dans le protocole ? ») |
|---|---|---|
| **§ 4.1 RSSI par paquet en promiscuous** | Non — mode radio déconseillé par Espressif sous trafic soutenu, et incompatible avec le modem-sleep | **Non, et pour une raison plus forte** : ce n'est pas un champ, c'est un état de la radio. #58 a tranché qu'il n'y a pas de mode ; ce serait donc un *réglage* de comportement radio, non persisté — et sa valeur dépend entièrement de [#64](https://github.com/Zaruitoga/conductor/issues/64) (les ACK remontent-ils ?). Inchangé. |
| **§ 4.2 CSI** | Non — mur structurel : on ne reçoit presque rien | **Non, et le mur est le même.** Rien dans le changement de critère ne crée du trafic reçu. Noter seulement que le CSI **est compilé** dans ce binaire (`sdkconfig:1235`), donc la porte est ouverte si quelqu'un trouvait une raison — il n'y en a pas. |
| **§ 4.3 Compteurs de pile** | Impasse — « un dump vers le log série », inexploitable | **Confirmé, et documenté.** Le § 3.4 donne la liste des compteurs qu'on renonce à lire, et le fait décisif : **il n'y a pas de compteur de retransmission dedans**. Ce n'était pas seulement inexploitable, c'est aussi que ça ne portait pas la grandeur qu'on espérait. |
| **§ 4.4 `esp_wifi_set_tx_done_cb`** | Second — API privée, aveugle au retard | **Requalifié.** L'API privée est moins risquée qu'il n'y paraissait (déclaration figée de v4.3 à v5.5, deux collaborateurs Espressif la recommandent) ; l'aveuglement au retard est *pire* qu'il n'y paraissait (32 retransmissions avant un `false`) ; **et ce n'est pas pour ça qu'il faut la prendre.** Elle porte une **date de fin d'émission par trame**, que #51 n'avait pas relevée parce que ce n'était pas sa question. C'est le seul candidat de tout l'inventaire qui batte à notre cadence sans toucher à la radio. |

Le tableau récapitulatif de #51 § 4.4 bis reste juste ligne à ligne. Ce ticket n'en retire rien ; il
ajoute six grandeurs (RSSI de déconnexion, RSSI moyenné par seuil, plancher de bruit, canal et
largeur négociés, TSF, FTM) et déplace une colonne : pour `tx_done`, ce n'est pas la cadence du
booléen qui compte, c'est la date de l'appel.

---

## 6. Ce qui reste non vérifiable, et la mesure qui trancherait

Sept points où l'honnêteté impose de séparer l'établi du plausible. Le § 0 rappelle pourquoi il n'y
a pas d'autre recours que le banc : **le TRM du S3 ne documente pas le MAC WiFi**, et le reste est
un blob. Les mesures sont lettrées D à K, la suite de A/B/C de #51.

**(a) À quelles trames le callback est-il appelé ?** L'appel est gardé par `bnone a7, a9` avec
`a7 = 8`, c'est-à-dire un **drapeau du descripteur d'émission** dont la signification n'est écrite
nulle part. Le rapport d'utilisateur de esp-idf#9605 (les balises en mode AP ne déclenchent pas le
callback) est cohérent avec un drapeau « vient de la pile réseau », mais ce n'est pas une preuve.
**Mesure D.**

**(b) Le rattachement d'un rappel à son datagramme.** Toute la valeur du § 2.4 en dépend, et rien
ne documente ce que pointe `data`. **Mesure F**, qui est la plus importante de la liste.

**(c) L'effet de l'agrégation.** `CONFIG_ESP32_WIFI_AMPDU_TX_ENABLED=y` : plusieurs MPDU peuvent se
terminer ensemble sur un BlockAck, et `lmacEndRetryAMPDUFail` écrit un statut à part. Combien de
rappels pour N datagrammes, et avec quelles dates — non établi. **Mesure D**, même passe.

**(d) Le contexte d'exécution dans tous les cas.** Le chemin direct `lmacTxDone → ppProcTxDone`
(§ 2.2) est pris sous condition ; les chemins lus restent dans `ppTask`, mais l'exhaustivité n'est
pas prouvée. **Mesure D**, même passe, une ligne de plus.

**(e) La cadence de rafraîchissement du plancher de bruit.** `wDev_GetNoiseFloor` renvoie
`wDevCtrl[47]`, réécrit par `pm_noise_check` sur un timer interne dont la période sort de `g_pm`,
structure non documentée. **Mesure G.**

**(f) Le point de comparaison du seuil de RSSI.** `esp_wifi_set_rssi_threshold` promet « if average
rssi gets lower than threshold » et le comparateur vit vraisemblablement sur le chemin balise
(`ieee80211_sta.o` porte `rssi_saved` et `rssi_index` à côté de `sta_recv_mgmt`), mais la fonction
`esp_wifi_set_rssi_threshold` est dans `ieee80211_supplicant.o` et le chemin n'a pas été remonté
jusqu'au bout. Sans incidence sur le verdict — l'événement ne peut pas battre plus vite que la
balise de toute façon.

**(g) Les champs de `lmacConfMib` au-delà des deux octets de retry.** `lmacInit` y pose aussi
`[0]=1536`, `[4]=[8]=1024`, `[12]=[16]=512` (durée de vie des MSDU, unités non documentées) et
`[22..23]=2346` (seuil RTS, valeur classique de désactivation). La durée de vie MSDU borne le temps
qu'une trame peut passer en file avant d'être jetée — donc elle borne le retard — mais l'unité
n'est établie ni par un header ni par la doc. Non utilisé ci-dessus ; à ne pas citer comme un chiffre.

### Mesure D — le callback : cadence, contexte, et taux d'échec

À poser temporairement dans le firmware, laisser tourner **dix minutes** à la cadence de spectacle,
lire le port série, retirer. Elle ne mesure pas si le callback existe (on le sait) : elle mesure
**combien de fois il est appelé par datagramme émis**, et dans quelle tâche.

```cpp
#include "esp_private/wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static volatile uint32_t n_cb = 0, n_fail = 0;
static volatile uint32_t n_task_other = 0;   // appels hors de la tâche attendue
static char first_task[16] = {0};

static void IRAM_ATTR tx_done(uint8_t ifidx, uint8_t *data, uint16_t *len, bool ok) {
  n_cb++;
  if (!ok) n_fail++;
  if (!first_task[0]) {                       // une seule fois : pcTaskGetName n'est pas gratuit
    const char *n = pcTaskGetName(NULL);
    for (int i = 0; i < 15 && n[i]; i++) first_task[i] = n[i];
  }
}
// dans setup(), APRES la connexion :
esp_wifi_set_tx_done_cb(tx_done);

// dans loop(), une fois par seconde, avec les compteurs du ReportManager :
Serial.printf("envoyes/s=%lu  rappels/s=%lu  echecs/s=%lu  udp_err=%lu  tache=%s\n",
              dPacketsSent, dCb, dFail, udpErrors, first_task);
```

**Lecture attendue.** `rappels/s ≈ envoyés/s` ⇒ un rappel par datagramme, et le rattachement du
§ 2.4 est jouable. `rappels/s` sensiblement **supérieur** ⇒ les trames internes (null-data du
modem-sleep, sondes) comptent aussi, et un champ devrait le dire. `rappels/s` **inférieur** ⇒
l'agrégation regroupe, et la date obtenue est celle du groupe. `tache` doit lire `wifi` (ou le nom
que le blob donne à `ppTask`) : toute autre valeur rouvre le point (d). Et `echecs/s` : s'il reste
à **0** sur dix minutes pendant que des épisodes de retard se produisent, le § 2.4 est confirmé au
banc et le booléen est définitivement écarté comme champ.

### Mesure E — le délai d'émission par trame, la grandeur candidate

Ne se tente qu'après F. Elle transforme le callback en mesure de retard côté ESP.

```cpp
static volatile uint32_t t_enq[256];          // µs, indexé par (seq & 0xFF)
static volatile uint32_t d_max = 0, d_sum = 0, d_n = 0, d_over20 = 0;

// dans _sendPacket, juste avant beginPacket :   t_enq[seq & 0xFF] = hdr.ts_esp_us;
// dans le callback, une fois F connue :
//   seq = <lu dans data a l'offset etabli par F>;
//   uint32_t d = micros() - t_enq[seq & 0xFF];
//   d_sum += d; d_n++; if (d > d_max) d_max = d; if (d > 20000) d_over20++;
```

**Lecture attendue.** La reconnaissance de #49 a vu, **côté hôte**, `p50 ≈ 0 ms`, `p95 ≈ 5 ms`,
`p99 ≈ 65–72 ms`, `max 107–168 ms`, et ~2,5 % des paquets au-delà de 20 ms. Si `d` reproduit cette
forme, **le retard est fabriqué avant l'antenne ou dans l'air, et l'ESP peut le dater** : c'est un
champ. Si `d` reste plat — p99 de quelques millisecondes — alors le retard naît après l'émission
(air, AP, hôte) et le champ ne servirait à rien. Dans les deux cas la réponse est un chiffre, pas un
avis. Un tableau circulaire de 256 entrées suffit : à 200 Hz il couvre 1,3 s, bien au-delà de la
plus longue queue observée.

### Mesure F — que pointe `data` ?

La mesure dont tout dépend, et la plus courte. Dans le callback, pour les **cinq premiers** appels
seulement, recopier les 64 premiers octets dans un tampon global ; les imprimer depuis `loop()`
(jamais depuis le callback).

**Lecture attendue.** On cherche notre propre `DataHeader` — `version = 1`, `type` ∈
{0x01…0x08, 0x10…0x17, 0x20}, `size` cohérent, puis un `seq` qui s'incrémente d'un appel à l'autre
(`protocol.h:38-45`). S'il apparaît à un offset **constant**, la mesure E est possible et le § 2.4
tient. S'il n'apparaît pas, `data` pointe autre chose (une copie interne, un descripteur) et toute
la piste « date par trame » se referme — auquel cas il ne reste du callback que le booléen, et donc
rien. Relever au passage **le champ Frame Control**, s'il est là : son bit 11 (*Retry*) est le seul
indice de retransmission qui puisse encore sortir (§ 3.3).

### Mesure G — le plancher de bruit se rafraîchit-il ?

Même forme que la mesure A de #51 : compter **les valeurs qui changent**, pas les lectures.

```cpp
extern "C" int8_t wDev_GetNoiseFloor(void);   // libpp/wdev.o, aucun header

static int8_t last_nf = 127;
static uint32_t nf_reads = 0, nf_changes = 0;
// dans loop() : int8_t nf = wDev_GetNoiseFloor();
//               nf_reads++; if (nf != last_nf) { nf_changes++; last_nf = nf; }
// une fois par seconde : Serial.printf("nf=%d dBm  lectures/s=%lu  changements/s=%lu\n", ...)
```

**Lecture attendue.** `changements/s` proche de **0** ⇒ la valeur est figée après la première
lecture valide, le champ ne porterait rien, dossier clos. Un ou deux par seconde ⇒ c'est le timer
de `pm_noise_check`, et on connaît enfin sa période. Davantage ⇒ le § 1 est à corriger. À faire en
même temps que la mesure A de #51 (deux passes, avec et sans `WiFi.setSleep(false)`) : le
modem-sleep éteint la radio, donc il devrait aussi geler le plancher de bruit, et c'est un
recoupement gratuit. **Attention** : ce symbole n'a pas de header, il peut disparaître à la
prochaine version du SDK ; la mesure sert à décider s'il vaut la peine d'y penser, pas à construire
dessus.

### Mesure H — l'AP répond-il à une requête FTM ?

Une passe, une réponse binaire, et c'est le seul point de tout l'inventaire où un « oui » ouvrirait
une grandeur vraiment neuve : un **temps d'aller-retour mesuré** (`rtt_est`, en ns) et un RSSI par
trame FTM, ni l'un ni l'autre dérivables d'une mesure passive.

```cpp
// après la connexion, une seule fois :
wifi_ap_record_t ap; esp_wifi_sta_get_ap_info(&ap);
wifi_ftm_initiator_cfg_t cfg = {};
memcpy(cfg.resp_mac, ap.bssid, 6);
cfg.channel = ap.primary; cfg.frm_count = 16; cfg.burst_period = 2;
esp_wifi_ftm_initiate_session(&cfg);
// puis, sur WIFI_EVENT_FTM_REPORT, imprimer status, rtt_est, dist_est, num_entries
```

**Lecture attendue.** `FTM_STATUS_UNSUPPORTED` ou `NO_RESPONSE` ⇒ la box n'est pas répondeur FTM
(le cas le plus probable sur du matériel grand public), la ligne sort de l'inventaire et #54 n'a
plus à y penser. `FTM_STATUS_SUCCESS` ⇒ noter `rtt_est` et le nombre d'entrées, et la question
devient : à quelle cadence une rafale peut-elle être relancée sans manger le lien ? Ce serait alors
un ticket à part. Attention, la carte #49 a mis « comparer les points d'accès » hors périmètre : un
FTM qui marche ici ne marchera pas forcément en salle.

### Mesure I — `udp_errors` est-il jamais non nul, et sur quoi ?

Le compteur existe depuis toujours et **personne ne sait s'il a déjà bougé**. Une ligne de plus dans
le firmware donne la cause avec le compte :

```cpp
  if (_udp.endPacket() == 0) { _udpErrors++; _lastErrno = errno; _lastErrAt = millis(); }
// imprimé avec le heartbeat, ou sur le port série une fois par seconde s'il a changé
```

**Lecture attendue.** `udp_errors` immobile sur une passe complète ⇒ l'ESP n'a jamais renoncé à
émettre, et tout trou de `seq` vu par l'hôte vient de l'air ou de l'hôte : c'est la moitié de la
question de #59 répondue pour rien. S'il bouge, `errno` dit laquelle : `ENOMEM` ⇒ les huit tampons
d'émission (`sdkconfig:1229`) sont le goulot, et c'est un réglage, pas une fatalité ;
`EHOSTUNREACH` / `ENOTCONN` ⇒ le lien était tombé. Corréler l'instant avec les épisodes ~3,25 s de
la reconnaissance.

### Mesure J — ce que le callback coûte à 200 paquets/s

Si quelqu'un doute du § 2.3. Encadrer le corps du callback par `esp_timer_get_time()`, garder
`max` et la somme, et surveiller **en même temps, côté hôte**, le débit de paquets et la forme du
retard — avant enregistrement, après enregistrement, sur deux fenêtres de cinq minutes. Le chiffre
qui décide n'est pas la microseconde consommée : c'est que **la distribution du retard côté hôte ne
bouge pas**. Le callback vit dans la tâche qui pilote la radio ; s'il déplace la queue, il fabrique
le défaut qu'il est censé mesurer.

### Mesure K — le mode PHY négocié bouge-t-il pendant une passe ?

Un appel par seconde à `esp_wifi_sta_get_negotiated_phymode`, compter les changements sur une passe
complète en configuration installée. **Zéro changement** ⇒ un octet dans le heartbeat suffit
largement, et il n'a même pas besoin d'être relu souvent. **Des changements** ⇒ deux passes ne sont
pas comparables sans lui, et #54 doit l'inscrire. L'appel étant un aller-retour inter-cœurs bloquant
(§ 3.1), une fois par seconde est déjà une concession : ne pas le mettre dans le chemin des 100 Hz,
même pour la mesure.

---

## Sources

**Code effectivement installé sur cette machine** (autorité pour « ce qui tourne sur la roue ») :
`~/.platformio/packages/framework-arduinoespressif32/` — `package.json` (3.20017.241212+sha.dcc1105b) ·
`cores/esp32/esp_arduino_version.h:22-26` · `cores/esp32/main.cpp:71` ·
`libraries/WiFi/src/WiFiUdp.cpp:133-190` ·
`tools/sdk/esp32s3/include/esp_common/include/esp_idf_version.h:22-26` ·
`tools/sdk/esp32s3/include/freertos/include/esp_additions/freertos/FreeRTOSConfig.h:81` ·
`tools/sdk/esp32s3/include/esp_wifi/include/esp_wifi.h` (505, 593, 630, 862, 965, 1048, 1136, 1181,
1197, 1213, 1351, 1376, 1386, 1400 ; `WIFI_TASK_CORE_ID` 190-194) ·
`tools/sdk/esp32s3/include/esp_wifi/include/esp_wifi_types.h` (190-210, 303-315, 327, 362-370,
386-433, 470-509, 570-586, 592-627, 683-692, 745-748, 763-782, 784-789) ·
`tools/sdk/esp32s3/include/esp_wifi/include/esp_private/wifi.h` (7-16, 122-144, 250-268, 447-485,
543-562) · `tools/sdk/esp32s3/include/esp_wifi/include/esp_private/wifi_os_adapter.h` ·
`tools/sdk/esp32s3/include/esp_wifi/include/esp_now.h` (56-59, 89, 96) ·
`tools/sdk/esp32s3/sdkconfig:7, 1225-1258, 1350-1370, 1447-1498`.

**Binaire livré, désassemblé** (`xtensa-esp32s3-elf-objdump` / `-nm` / `-readelf`, toolchain
PlatformIO) : `tools/sdk/esp32s3/lib/libnet80211.a` — `ieee80211_api.o`
(`esp_wifi_set_tx_done_cb`, `esp_wifi_get_tsf_time`, `esp_wifi_statis_dump`,
`esp_wifi_sta_get_negotiated_phymode`, `esp_wifi_get_max_tx_power`), `ieee80211_ioctl.o`
(`wifi_get_tsf_time_process`), `ieee80211_debug.o` (`g_hmac_cnt`, `dbg_hmac_statis_dump`),
`wl_cnx.o` (`cnx_rc_update_rssi`), `ieee80211_supplicant.o` (`esp_wifi_set_rssi_threshold`),
`ieee80211_sta.o` (`rssi_saved`, `rssi_index`) ·
`tools/sdk/esp32s3/lib/libpp.a` — `pp.o` (`ppTask` / `.wifi0iram.9`, `ppProcTxDone`,
`ppRegisterTxDoneUserActionCallback`, `g_tx_done_cb_func`, `pp_create_task`), `lmac.o`
(`lmacInit`, `lmacTxDone`, `lmacProcessTxSuccess`, `lmacEndFrameExchangeSequence`,
`lmacEndRetryAMPDUFail`, `lmacDiscardMSDU`, `lmacRecycleMPDU`, `lmacReachShortLimit`,
`lmacReachLongLimit`, `lmacConfMib`, `esp_wifi_internal_get_mib`,
`esp_wifi_internal_set_retry_counter`, `esp_wifi_internal_set_msdu_lifetime`),
`if_hwctrl.o` (`ic_register_pp_tx_done_cb`), `wdev.o` (`wDev_GetNoiseFloor`), `pm.o`
(`pm_noise_check`), `pp_debug.o` (`g_lmac_cnt`, `g_pm_cnt`).

**Amont, aux versions consultées** (le fichier v4.4.7 a été récupéré et comparé au fichier installé :
identiques) :
[esp-idf v4.4.7 — `esp_private/wifi.h`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_private/wifi.h#L543-L562) ·
[esp-idf v4.4.7 — `esp_wifi.h`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_wifi.h) ·
[esp-idf v4.4.7 — `esp_wifi_types.h`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_wifi_types.h) ·
tags comparés pour la stabilité de la déclaration :
[v4.2.5](https://github.com/espressif/esp-idf/blob/v4.2.5/components/esp_wifi/include/esp_private/wifi.h) (absente) ·
[v4.3.7](https://github.com/espressif/esp-idf/blob/v4.3.7/components/esp_wifi/include/esp_private/wifi.h#L550) ·
[v5.0.7](https://github.com/espressif/esp-idf/blob/v5.0.7/components/esp_wifi/include/esp_private/wifi.h#L550) ·
[v5.1.5](https://github.com/espressif/esp-idf/blob/v5.1.5/components/esp_wifi/include/esp_private/wifi.h#L584) ·
[v5.5](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_wifi/include/esp_private/wifi.h#L580) ·
[arduino-esp32 2.0.17 — `WiFiUdp.cpp`](https://github.com/espressif/arduino-esp32/blob/2.0.17/libraries/WiFi/src/WiFiUdp.cpp#L178-L190) ·
[arduino-esp32 2.0.17 — `main.cpp`](https://github.com/espressif/arduino-esp32/blob/2.0.17/cores/esp32/main.cpp#L71)

**Documentation Espressif, version v4.4.7 / esp32s3 :**
[Wi-Fi API Reference](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-reference/network/esp_wifi.html) ·
[Wi-Fi Driver (Sniffer Mode, Power-saving Mode)](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-guides/wifi.html) ·
[*ESP32-S3 Technical Reference Manual*](https://documentation.espressif.com/esp32-s3_technical_reference_manual_en.pdf)
— sommaire lu dans les signets du PDF : **aucun chapitre MAC ni bande de base WiFi**, une seule
entrée « Wi-Fi » (§ 7.2.4.3, l'horloge).

**Issues GitHub — avec réponse de mainteneur, nommée et datée :**
[esp-idf#9605](https://github.com/espressif/esp-idf/issues/9605) — `MaxwellAlan`, `COLLABORATOR`,
2022-08-24 : « Yes, you can try this callback, this callback will trigger when your pkts tx
success. » ·
[esp-idf#12070](https://github.com/espressif/esp-idf/issues/12070) — `xuxiao111`, `COLLABORATOR`,
2023-08-28 : `esp_wifi_internal_set_retry_counter(int src, int lrc)`, « The maximum value should not
exceed 32 » ·
[esp-idf#7904](https://github.com/espressif/esp-idf/issues/7904) — `xueyunfei998`, `NONE`,
2022-06-07 (corroboration, pas autorité), fil clos par `Alvin1Zhang`, `COLLABORATOR`, 2022-07-13.

**Recherche de code** (`gh api search/code`, `repo:espressif/esp-idf`) : `esp_wifi_set_tx_done_cb`
n'apparaît qu'en `components/esp_wifi/include/esp_private/wifi.h` — **aucun appelant dans le dépôt**.

**Firmware** (`~/Desktop/IMU_project/PIO projects/260524_BNO_super_reports`, lu, non modifié) :
`platformio.ini` · `src/protocol.h:28, 33-45` · `src/report_manager.cpp:201-226` ·
`src/report_manager.h:93-95` · `src/main.cpp:22-27, 93-124`.

**Dépôt courant** (lu, non modifié) : `transport/protocol.py:29-31, 138-141, 243-249` ·
`transport/esp_health.py:44-68, 72-88` · `api/static/js/panels/health.js:44-48`.

**Note antérieure reprise et non refaite** :
[`docs/research/cadence-rssi-esp32.md`](cadence-rssi-esp32.md) (#51) — §§ 1 à 5 pour le RSSI, son
écrivain unique, sa cadence, le coût des deux API, et les mesures A/B/C.
