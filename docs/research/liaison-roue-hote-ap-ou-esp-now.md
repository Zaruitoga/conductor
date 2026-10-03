# Relier la roue à l'hôte en tournée : un point d'accès à soi (lequel ?), ou ESP-NOW vers un récepteur USB ?

Recherche pour le ticket [#86](https://github.com/Zaruitoga/conductor/issues/86). Sans rattachement à
la carte [#49](https://github.com/Zaruitoga/conductor/issues/49), qui met « comparer plusieurs
points d'accès » hors de son périmètre, mais appuyée sur ce qu'elle a mesuré.
Recherche seulement : **rien n'est appliqué ici**, ni dans ce dépôt ni dans le firmware, et **rien
n'est acheté**. Tout ce que la note conclut sur le comportement radio d'un appareil ou d'un
protocole reste une **hypothèse jusqu'à une passe de banc** (`bench/capture.py`), comme sur la
carte.

**Enjeu.** Le banc de [#80](https://github.com/Zaruitoga/conductor/issues/80) (2026-10-03) a montré
que le point d'accès, et le lien de l'hôte, pèsent au premier ordre — et qu'un équipement grand
public les dégrade sans rien en dire. Avec la box SFR sur le canal 6 choisi par son mode auto, les
gros épisodes suivent une grille de 10,07 s et l'ESP **échoue à émettre** pendant ces épisodes
(perte 0,46–0,70 %, corrélation 0,90 avec le retard). Avec un partage de téléphone sur le canal 1 :
perte nulle. Avec le Mac passé en 2,4 GHz sur le canal de la box : des rafales de ~29 paquets toutes
les ~145 ms. En tournée, la box de la salle est une inconnue chaque soir. La question n'est pas le
débit — ~200 paquets/s de 24 à 40 octets, c'est trivial — mais **qui tient la radio, et donc la
queue de latence**.

---

## Verdict

**Les deux architectures règlent le problème que #80 a montré — elles retirent la box de la salle
de la chaîne — mais elles ne retirent pas le même reste. Un point d'accès à soi garde le protocole
d'un octet près et ne coûte qu'un appareil ; il laisse le modem-sleep de la roue, le relais par
l'AP, et une radio de plus à régler. ESP-NOW retire l'AP, l'IP, le mDNS, le modem-sleep et la radio
du Mac d'un coup, et il rend à l'hôte ce que #51 et #60 ont jugé inaccessible : un RSSI par trame,
côté récepteur, à la cadence d'émission. Il le paie d'un second firmware, d'un récepteur série côté
conductor, et d'un piège : à son débit par défaut (1 Mbit/s), notre charge occupe ~29 % du canal,
contre ~4 % aujourd'hui. Ce que le banc doit essayer d'abord est l'AP à soi, parce qu'il tranche
sans une ligne de code la question que #80 a laissée ouverte — « la box, ou le canal 6 ? » — et que
la réponse décide si ESP-NOW vaut son prototype.**

Les points décisifs, et ce qu'il faut accepter avec :

| | Établi | Contrepartie |
|---|---|---|
| **1. La version d'IDF décide d'ESP-NOW** | La plateforme PlatformIO **officielle**, jusqu'à sa dernière version (v7.1.3, 2026-09-11), embarque arduino-esp32 **2.0.17 / IDF v4.4.7** — ce que la roue compile aujourd'hui. Là, ESP-NOW est la **v1** : 250 octets de charge utile, et le callback de réception ne reçoit **que l'adresse MAC** (`esp_now.h:51, 89` installé). Le `rx_ctrl` (RSSI par trame) arrive en IDF **v5.1**, la v2 (1470 octets) en **v5.4–v5.5** (§ 0). | Pour l'avoir, il faut arduino-esp32 **3.x** — donc la plateforme communautaire **pioarduino**, puisque l'officielle ne la suit pas. **Mais seul le récepteur en a besoin** : un récepteur v2 reçoit les trames v1 (§ 4.1), donc la roue peut rester en 2.0.17. |
| **2. La taille de trame n'est pas un sujet** | Le plus gros paquet de données possible fait 12 + 8 × 16 = **140 octets** (`MAX_SUPER_DEPS = 8`, quaternions de 16 o), l'ACK de config **226 octets**. Tout tient dans les 250 octets de la v1, ACK compris. | — |
| **3. L'occupation du canal, si** | ESP-NOW émet par défaut à **1 Mbit/s** (doc Espressif). Calcul : **~1,4–1,5 ms d'air par trame**, ~29 % du canal à 200 trames/s. Le même trafic en UDP sur HT20 occupe **~4 %** (MCS7) à ~6 % (MCS0). Le mode **LR** (500 et 250 kbit/s) monte à ~40 et ~63 % : **exclu** à cette charge (§ 4.2). | Le débit se règle (`esp_wifi_config_espnow_rate`, présent dans l'IDF installé) ; qu'il tienne en pratique, et à quel prix de portée, est **à vérifier au banc**. Sur un canal partagé avec trois box voisines, 29 % d'occupation serait une régression. |
| **4. ESP-NOW n'enlève pas l'air** | L'unicast ESP-NOW est acquitté et retransmis au niveau MAC ; le callback d'envoi dit `SUCCESS` « si la donnée est reçue au niveau MAC » (doc). Le CSMA/CA est le même. **Si la grille de 10 s vient du voisinage du canal 6, ESP-NOW sur le canal 6 la subira aussi.** | Ce qu'il enlève : l'AP qui part balayer un autre canal, l'AP qui tamponne, la radio du Mac, et le modem-sleep (une roue non associée ne dort pas, § 4.3). Lequel de ces mécanismes fait la grille n'est **pas établi**, et c'est exactement ce que la passe 1 sépare (§ 8). |
| **5. Ce qu'ESP-NOW rend en observabilité** | Côté récepteur, en IDF ≥ 5.1, chaque trame reçue arrive avec son `wifi_pkt_rx_ctrl_t` : **RSSI, débit, plancher de bruit, horodatage µs** de réception. C'est le RSSI par paquet que #51 a jugé hors d'atteinte côté émetteur — mais mesuré à l'autre bout, ce qui est la même atténuation. Côté roue, le callback d'envoi est une **API publique** (là où l'UDP passe par l'API privée `esp_wifi_set_tx_done_cb` de #60). | Le callback d'envoi porte **le même booléen** que `txStatus` (#60 § 1) : aveugle aux retransmissions réussies. Et en IDF v4.4.7 le récepteur ne voit le RSSI qu'en **mode promiscuous** — d'où le récepteur en arduino-esp32 3.x. |
| **6. L'AP à soi : ce qui compte, et ce qui manque** | OpenWrt expose tout ce que le ticket demande en contrôle radio (`channel`, `htmode HT20`, `beacon_int`, `dtim_period`, `basic_rate`, `legacy_rates`, `txpower`, `wmm`, `isolate`) et, sur les puces MediaTek MT7981 (pilote `mt7915`), **`tx retries`, `tx failed` et le signal des ACK par station** dans `iw station dump`. RouterOS v7 (MikroTik) règle balise et DTIM et a un renifleur et un `spectral-scan`. | Les retransmissions qu'un AP compte sont **les siennes, vers l'ESP** — le sens descendant, celui des commandes. Le sens qui nous importe (ESP → AP) ne s'y lit qu'en creux : un compteur de **doublons** (`rx_duplicates`, mac80211), qui ne voit que les retransmissions dont l'original a été reçu. Le ticket supposait l'inverse. |
| **7. Le coût de migration ESP-NOW** | Côté conductor : `parse_packet` **inchangé** si le récepteur relaie les octets du datagramme ; un récepteur série remplace `UDPReceiver` ; `EspConfigurator` perd `resolve`/`set_host`/le mDNS ; `EspHealth` garde le heartbeat mais son `rssi_dbm` tombe à **0** (`WiFi.RSSI()` renvoie 0 hors association, #51 § 1.1) et doit venir du récepteur. Côté firmware : `_sendPacket`, `ConfigServer` et `setup()` changent, plus un firmware de récepteur d'une centaine de lignes. | Un octet de série n'a pas de frontière de datagramme : il faut un **encadrement** (COBS ou SLIP) et une resynchronisation. `pyserial` est une dépendance de plus (déjà prévue par #81 pour le banc). Le simulateur parle UDP et devra parler à un pseudo-terminal. |

**Ce qui n'est pas vérifiable ici** : § 8, quatre passes.

---

## 0. Les versions qui font foi

Le firmware est `~/Desktop/IMU_project/PIO projects/260524_BNO_super_reports`. `platformio.ini`
déclare `platform = espressif32`, `board = adafruit_feather_esp32s3_nopsram`, `framework = arduino`,
**sans épingler aucune version**. Relu sur cette machine plutôt que repris des notes voisines :

| | Version | Source lue |
|---|---|---|
| plateforme PlatformIO installée | `espressif32` **6.10.0** | `~/.platformio/platforms/espressif32/platform.json:21` |
| paquet framework | `3.20017.241212+sha.dcc1105b` | `~/.platformio/packages/framework-arduinoespressif32/package.json` |
| arduino-esp32 | **2.0.17** | idem, et notes #51 / #52 § 0 |
| ESP-IDF embarqué | **v4.4.7** | `tools/sdk/esp32s3/include/esp_common/include/esp_idf_version.h:22-26` |
| `ESP_NOW_MAX_DATA_LEN` | **250** | `tools/sdk/esp32s3/include/esp_wifi/include/esp_now.h:51` |
| callback de réception | `(const uint8_t *mac_addr, const uint8_t *data, int data_len)` | `esp_now.h:89` |
| callback d'envoi | `(const uint8_t *mac_addr, esp_now_send_status_t status)` | `esp_now.h:96` |
| USB de la carte | `ARDUINO_USB_CDC_ON_BOOT=1`, `usb_mode=0` (TinyUSB, USB-OTG) | `boards/adafruit_feather_esp32s3_nopsram.json`, `boards.txt:10320-10321` |

**Mettre la plateforme à jour ne change rien.** La dernière version publiée de la plateforme
officielle, **v7.1.3** (2026-09-11), annonce toujours dans ses notes de version, comme v7.1.0,
v7.0.0, v6.13.0 et v6.12.0 : « Arduino - v2.0.17 (based on IDF v4.4.7) »
([releases](https://github.com/platformio/platform-espressif32/releases)). PlatformIO a ouvert en
2023 un appel à soutenir arduino-esp32 3.x
([platform-espressif32#1225](https://github.com/platformio/platform-espressif32/issues/1225),
`ivankravets`, 2023-11-04 : « The ESP32 Core for Arduino 2.x is the most recent major version
currently recommended for use with PlatformIO »). Le relais est la plateforme communautaire
**pioarduino** ([pioarduino/platform-espressif32](https://github.com/pioarduino/platform-espressif32),
dernière version `55.03.312-1`, 2026-09-22, construite sur arduino-esp32 3.3.12).

**La correspondance arduino-esp32 → IDF**, lue dans les notes de version d'Espressif :

| arduino-esp32 | IDF | Source |
|---|---|---|
| 2.0.17 | v4.4.7 | installé |
| 3.0.0 (2024-05-27) | v5.1.4 | « based on the ESP-IDF v5.1.4 » ([release 3.0.0](https://github.com/espressif/arduino-esp32/releases/tag/3.0.0)) |
| 3.2.0 (2025-03-28) | v5.4 | « Arduino 3.2.0 based on ESP-IDF v5.4 » ([release 3.2.0](https://github.com/espressif/arduino-esp32/releases/tag/3.2.0)) |
| 3.3.0 (2025-07-23) | v5.5 | « Arduino ESP32 v3.3 based on ESP-IDF v5.5 » ([release 3.3.0](https://github.com/espressif/arduino-esp32/releases/tag/3.3.0)) ; 3.3.12 intègre « IDF v5.5.5 » |

**Et ce que chaque IDF fait d'ESP-NOW**, lu dans `components/esp_wifi/include/esp_now.h` à chaque
tag :

| IDF | Charge max | Callback de réception | Callback d'envoi |
|---|---|---|---|
| [v4.4.7](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_now.h) (installé) · [v5.0](https://github.com/espressif/esp-idf/blob/v5.0/components/esp_wifi/include/esp_now.h) | 250 | adresse MAC seule | adresse MAC + booléen |
| [v5.1](https://github.com/espressif/esp-idf/blob/v5.1/components/esp_wifi/include/esp_now.h) · [v5.3](https://github.com/espressif/esp-idf/blob/v5.3/components/esp_wifi/include/esp_now.h) | 250 | **`esp_now_recv_info_t`** : `src_addr`, `des_addr`, **`rx_ctrl`** | inchangé |
| [v5.4](https://github.com/espressif/esp-idf/blob/v5.4/components/esp_wifi/include/esp_now.h) | v1 : 250 ; **v2 : 1490** (`ESP_NOW_MAX_DATA_LEN_V2`) | idem | inchangé |
| [v5.5](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_wifi/include/esp_now.h) | v1 : 250 ; **v2 : 1470** | idem | **`esp_now_send_info_t`** = `wifi_tx_info_t` : adresses, `data`, `data_len`, **`rate`**, `tx_status` |

*(Le tag v5.4 déclare 1490, le tag v5.5 et la
[doc v5.5](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/api-reference/network/esp_now.html)
1470 — la différence ne nous concerne pas, nos trames font 140 octets au plus.)*

La documentation citée est celle de la version concernée : [v4.4.7 /
esp32s3](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/) pour la roue telle qu'elle
est, [v5.5 / esp32s3](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/) pour un
récepteur construit en arduino-esp32 3.3.

---

## 1. Les exigences, et ce qui les fonde

Chacune est reliée à la mesure ou au constat qui la fonde. Elles valent pour toute architecture ;
le § 6 dit laquelle les satisfait.

| # | Exigence | Fondée sur |
|---|---|---|
| E1 | **Aucun équipement de la salle dans la chaîne.** Ni sa box, ni son canal, ni son mode auto. | #80 : grille de 10,07 s et 0,46–0,70 % de perte avec la box SFR en canal auto ; rien de tel avec un autre équipement sur un autre canal. |
| E2 | **Le canal se choisit, et ne bouge plus.** Fixe, 20 MHz, sans choix automatique ni balayage de fond — y compris côté hôte. | #80 point 5 : le mode auto de la box a choisi le canal 6, où trois réseaux voisins sont à −60, −62 et −72 dBm, quand le canal 1 était presque vide. |
| E3 | **La radio de l'hôte hors du 2,4 GHz**, ou hors de la chaîne. | Passe du ticket #86 : Mac en 2,4 GHz sur le canal de la box ⇒ rafales de ~29 paquets toutes les ~145 ms, retard médian +72 ms. Hypothèse : mise en tampon par l'AP pour une station en économie d'énergie — le même mécanisme que la note #52 § 3.5 décrit pour l'ESP. |
| E4 | **Le modem-sleep de la roue est une décision, pas un défaut.** Ou l'architecture le supprime, ou l'intervalle de balise et le DTIM se règlent. | #51 § 2.2 : `WIFI_PS_MIN_MODEM` est actif par défaut, la radio s'éteint entre deux DTIM ; #63 doit le mesurer. #52 § 3.5 : il crée une asymétrie de 51 à 154 ms sur la synchro. |
| E5 | **Les retransmissions doivent devenir lisibles quelque part.** | #60 § 3.3 : l'ESP ne peut pas lire son propre nombre de retransmissions ; `txStatus` reste `true` jusqu'à 32 essais. #80 point 2 : la perte naît dans l'ESP, à l'envoi. |
| E6 | **Un relevé RF par salle**, rejouable. | #79 (environnement RF de la salle), #71 (harnais rejouable). |
| E7 | **Fonctionnement sans Internet**, sans compte ni application constructeur pour démarrer. | Contrainte du ticket. |
| E8 | **Charge : ~200 trames/s de 24 à 40 octets**, latence et queue avant débit. | #80 : charge révisée à 50 Hz, GYRO + LINEAR_ACCEL + GAME_RV + super 0 = `[0, 6]`. |
| E9 | **Alimentation compatible avec une scène inconnue** : USB, batterie, ou rien. | Ouvert dans le ticket. |

---

## 2. D'où vient la queue, mécanisme par mécanisme

Les deux architectures se jugent sur les mécanismes qu'elles retirent. Cinq candidats ressortent
de #49, #51, #52, #80 et du ticket :

| Mécanisme | Ce qu'il produit | AP à soi | ESP-NOW |
|---|---|---|---|
| **M1. L'AP quitte son canal** (balayage de fond, choix automatique, « optimisation ») | l'ESP n'obtient plus d'ACK, réessaie, renonce ; file pleine, débordement — la signature de #80 point 2 | retiré **si** l'AP se règle (E2) | retiré : il n'y a pas d'AP |
| **M2. Le voisinage occupe le canal** | attente de canal libre, collisions, retransmissions | inchangé, mais le canal se choisit | inchangé, mais le canal se choisit |
| **M3. L'AP tamponne pour une station endormie** (DTIM) | rafales périodiques | touche la radio de l'hôte s'il est en 2,4 GHz (E3) ; touche les **commandes** vers l'ESP (#52 § 3.5) | retiré : pas d'AP, pas de DTIM |
| **M4. Le modem-sleep de la roue** | RF/PHY/BB éteints entre deux DTIM (#51 § 2.2) | présent tant que `WiFi.setSleep(false)` n'est pas posé (#63) | retiré : une station **non associée** ne dort pas (§ 4.3) |
| **M5. Le relais** | la trame traverse deux fois l'air si l'hôte est en 2,4 GHz sur le même canal | retiré si l'hôte est en Ethernet | remplacé par l'USB (§ 4.6) |

**La grille de 10 s n'est attribuée à aucun d'eux.** Elle disparaît quand on change *à la fois*
d'équipement et de canal (#80 point 5). Si elle vient de M1, n'importe quel AP réglable la fait
disparaître, sur n'importe quel canal. Si elle vient de M2, elle reviendra sur le canal 6 avec
n'importe quelle architecture — ESP-NOW compris — et seule la *possibilité de choisir le canal*
compte. C'est la question que la passe 1 (§ 8) tranche, et elle coûte un AP, pas un firmware.

---

## 3. Architecture 1 — un point d'accès à soi, protocole actuel

### 3.1 Ce qui ne change pas

**Rien du protocole.** `transport/protocol.py`, `protocol.h`, `UDPReceiver`, `EspConfigurator`,
`EspHealth`, le simulateur et `bench/capture.py` restent tels quels, le mDNS aussi
(`config.ESP_HOST = imu-cyrwheel.local`, à condition que l'AP n'isole pas les clients — `isolate`
vaut 0 par défaut sous OpenWrt). Le seul changement est l'identifiant réseau dans `wifi_config.h`
(non ouvert ici). La date noyau `SO_TIMESTAMP_MONOTONIC` de `bench/capture.py` reste disponible.

### 3.2 Ce qu'un AP réglable apporte

**Contrôle radio — établi pour OpenWrt** ([Wi-Fi configuration](https://openwrt.org/docs/guide-user/network/wifi/basic)) :

| Critère du ticket | Option OpenWrt | Ce que dit la doc |
|---|---|---|
| canal fixe | `channel` | « 'auto' defaults to the lowest available channel, or utilizes the ACS algorithm » — un numéro le fixe |
| 20 MHz | `htmode HT20` | « control 802.11n (HT), 802.11ac (VHT) and 802.11ax (HE) » |
| pas de balayage de chevauchement | `noscan` | « Do not scan for overlapping BSSs in HT40+/- or HE40 modes » — sans objet en HT20 |
| intervalle de balise | `beacon_int` | « measured in units of 1.024 ms », défaut 100 |
| DTIM | `dtim_period` | « one DTIM per this many beacon frames », défaut **2** |
| couper le 802.11b | `legacy_rates` | « 0 = Disallow legacy 802.11b data rates », **défaut 0 depuis 21.02** |
| débits de base | `basic_rate`, `supported_rates` | en kb/s |
| puissance | `txpower` | dBm, bornée par le pays |
| WMM | `wmm` | défaut 1 |
| pas d'isolation | `isolate` | défaut 0 |

Le DTIM par défaut d'OpenWrt est **2**, pas 3 : avec le modem-sleep actif, cela met déjà la roue à
~4,9 réveils/s au lieu de 3,3 (#51 § 2.1). Et `dtim_period 1` le porte au plafond de la balise.

**Observabilité — établi, avec une correction au ticket.** `iw station dump` imprime
`tx retries`, `tx failed`, `last ack signal` et `avg ack signal`
([iw `station.c`](https://git.kernel.org/pub/scm/linux/kernel/git/jberg/iw.git/tree/station.c),
lignes 354-415). Sur les puces MediaTek MT7981 — celles des deux appareils OpenWrt de la courte
liste —, le pilote est `mt7915` (le fichier `mt7915/soc.c` traite `is_mt7981`), et sa fonction
`mt7915_sta_statistics` remplit ces quatre champs sans condition
([`mt76/mt7915/main.c:1244-1254`](https://github.com/openwrt/mt76/blob/master/mt7915/main.c)).

**Mais ces compteurs sont ceux de l'AP qui émet vers l'ESP**, c'est-à-dire le sens descendant —
les commandes de config, quelques datagrammes par minute. Le sens qui porte nos 200 trames/s est
**montant**, et un AP ne compte pas les retransmissions d'un autre. Ce qu'il en voit :

- le **compteur de doublons** de mac80211 (`rx_duplicates`, debugfs par station,
  [`debugfs_sta.c:1319`](https://github.com/torvalds/linux/blob/master/net/mac80211/debugfs_sta.c)),
  incrémenté quand une trame porte le bit *Retry* **et** le même numéro de séquence que la
  précédente ([`rx.c:1566-1570`](https://github.com/torvalds/linux/blob/master/net/mac80211/rx.c)).
  Il ne voit donc **que les retransmissions dont l'original a été reçu** — celles où c'est l'ACK
  qui s'est perdu. Une trame perdue puis retransmise n'y laisse aucune trace. Borne inférieure,
  et encore : que le matériel ne filtre pas les doublons avant mac80211 est **à vérifier** ;
- le signal par trame reçue, en **mode moniteur** sur une interface virtuelle — y compris le bit
  *Retry* de chaque essai décodé, donc une autre borne inférieure ;
- l'**occupation du canal** (`iw dev … survey dump` : temps actif, occupé, reçu, émis), qui est
  exactement le relevé de #79.

Tout cela s'interroge depuis l'hôte par SSH, sans rien installer sur l'AP au-delà de `iw` (présent).

### 3.3 Ce qu'il laisse

- **M2, le voisinage.** Inchangé ; on choisit seulement le canal.
- **M4, le modem-sleep de la roue.** Présent tant que #63 n'a pas conclu. L'AP à soi le rend
  *réglable* (balise, DTIM), pas absent.
- **Une radio de plus à maîtriser** : l'AP lui-même — son firmware, ses mises à jour, son
  démarrage (un AP OpenWrt démarre en dizaines de secondes, **à mesurer** par appareil).
- **Un second appareil sur scène**, à alimenter (E9) : c'est le cas pour les deux architectures,
  mais ici il demande du courant, alors qu'un récepteur ESP-NOW est alimenté par l'hôte.
- **L'hôte doit être en Ethernet** (E3, M5) pour que la trame ne traverse l'air qu'une fois. Un
  Mac sans port demande un adaptateur (ouvert dans le ticket). À défaut, l'hôte en 5 GHz sur le
  même AP double bande retire aussi M3/M5 de la bande 2,4 GHz — hypothèse plausible, **non
  mesurée**.

### 3.4 Coût de migration

Un appareil, sa configuration (une fois, sauvegardée en fichier), et un changement d'identifiant
réseau dans le firmware. **Aucun code.**

---

## 4. Architecture 2 — ESP-NOW vers un second ESP32 branché en USB

La roue émet ses trames en ESP-NOW unicast vers un récepteur ESP32-S3, qui les relaie par USB
CDC à l'hôte. Plus d'AP, plus d'IP, plus de mDNS.

### 4.1 La taille de trame, contre nos paquets

**Établi.** Format ([doc v4.4.7, Frame Format](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-reference/network/esp_now.html)) :
trame d'action spécifique au fabricant, `MAC Header | Category Code | OUI | Random Values |
Vendor Specific Content | FCS` = 24 + 1 + 3 + 4 + (7 + corps) + 4, soit **43 octets de surcoût**
par trame et un corps de 0 à 250 octets.

Nos paquets, lus dans le firmware (`protocol.h`, `report_manager.h:26-28`, `config_server.cpp:90-141`) :

| Paquet | Octets (en-tête 12 compris) |
|---|---|
| GYRO, LINEAR_ACCEL (Vec3) | 24 |
| GAME_RV (Quat) | 28 |
| super 0 = `[0, 6]` | 12 + 12 + 16 = **40** |
| heartbeat | 12 + 24 = 36 |
| super le plus gros possible (8 quaternions) | 12 + 128 = **140** |
| ACK de config (`_sendAck`) | 12 + 1 + 8 × 12 + 1 + 8 × 14 + 4 = **226** |

**Tout tient en v1.** Le relais peut donc porter **les octets exacts du datagramme UDP actuel**, et
`parse_packet` les décoder sans changement.

**Compatibilité v1/v2 — établi** (en-tête v5.5, `esp_now_get_version`) : « v2.0 devices are capable
of receiving packets from both v2.0 and v1.0 devices », et « v1.0 devices can receive v2.0 packets
if the packet length is less than or equal to `ESP_NOW_MAX_IE_DATA_LEN` » (250). Une roue en IDF
4.4.7 et un récepteur en IDF 5.5 se parlent dans les deux sens, tant que les commandes restent
sous 250 octets — elles en font moins de 20.

### 4.2 Le débit PHY, et ce qu'il coûte au canal

**Établi** ([doc v5.5](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/api-reference/network/esp_now.html)) :
« The default ESP-NOW bit rate is 1 Mbps. » Et dans l'IDF installé, la constante de rang 0 est
`WIFI_PHY_RATE_1M_L` — « 1 Mbps with long preamble »
(`esp_wifi_types.h:593`), soit un préambule PLCP de 192 µs.

**Calcul (pas une mesure).** Temps d'air par trame = DIFS + attente aléatoire moyenne + trame +
SIFS + ACK, avec les paramètres 802.11b (préambule long 192 µs, ACK de 14 octets à 1 Mbit/s,
DIFS 50 µs, fenêtre min. 31 × 20 µs) et 802.11n HT20 (préambule mixte 36 µs, symboles de 4 µs,
ACK OFDM, créneau court). Pour l'UDP, en-tête MAC QoS + CCMP + LLC + IP + UDP + FCS = 82 octets.
Charge : quatre flux à 50 Hz (24, 24, 28, 40 octets).

| Transport | Air par trame | Occupation à 200 trames/s |
|---|---|---|
| **ESP-NOW à 1 Mbit/s (défaut)** | 1,40 – 1,53 ms | **~29 %** |
| ESP-NOW en HT20 MCS0, si on le règle | 0,26 – 0,28 ms | ~5 % |
| ESP-NOW en HT20 MCS7, si on le règle | 0,19 ms | ~4 % |
| UDP actuel en HT20 MCS7 | 0,19 ms | ~4 % |
| UDP actuel en HT20 MCS0 | 0,31 – 0,33 ms | ~6 % |
| ESP-NOW LR 500 kbit/s (borne basse) | 1,9 – 2,2 ms | ~40 % |
| ESP-NOW LR 250 kbit/s (borne basse) | 3,0 – 3,5 ms | ~63 % |

Trois lectures :

1. **Au débit par défaut, ESP-NOW occupe le canal sept fois plus que l'UDP d'aujourd'hui.** Sur
   un canal vide, 29 % passe. Sur le canal 6 de #80, avec trois box voisines, c'est autant de
   temps exposé aux collisions, et une file qui se creuse plus vite pendant un épisode. Ce serait
   une régression structurelle déguisée en simplification.
2. **Le débit se règle** : `esp_wifi_config_espnow_rate(wifi_interface_t ifx, wifi_phy_rate_t rate)`
   est dans l'IDF installé (`esp_now.h:244`, « should be called after `esp_wifi_start()` »). Qu'il
   soit honoré pour des trames d'action, et ce qu'un débit élevé retire en portée, est **à vérifier
   au banc** (passe 2). En IDF 5.x l'API a un successeur par pair ; la roue n'y est pas.
3. **Le mode LR est exclu à cette charge.** La doc v4.4.7 ([Wi-Fi Driver, Long Range](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-guides/wifi.html))
   le décrit à « 1/2 Mbps and 1/4 Mbps », avec « about 4 dB gain than the traditional 802.11B
   mode » et une distance « about 2 to 2.5 times the distance of 11B », pour des besoins de débit
   « very small, such as remote device control ». 40 à 63 % d'occupation **avant** toute
   retransmission, à 5–10 m d'un hôte : le gain de portée ne sert à rien, la queue empire. Le
   préambule LR propre n'est pas documenté ; les chiffres ci-dessus sont des bornes basses.

### 4.3 Accusé, retransmissions, et pourquoi la roue ne dormira plus

**Établi.** Le callback d'envoi « will return `ESP_NOW_SEND_SUCCESS` […] if the data is received
successfully on the MAC layer. Otherwise, it will return `ESP_NOW_SEND_FAIL` », et « It is not
guaranteed that application layer can receive the data » (doc v4.4.7). L'unicast est donc acquitté
et retransmis par le MAC, comme une trame de données en infrastructure.

**Une contrainte de cadence — établi** (même page) : « too short interval between sending two
ESP-NOW data may lead to disorder of sending callback function. So, it is recommended that sending
the next ESP-NOW data after the sending callback function of the previous sending has returned. »
Le firmware actuel envoie quand le BNO livre, sans attendre. À 200 trames/s on a 5 ms entre deux
envois et ~1,5 ms d'air par trame au débit par défaut : la recommandation est tenable en régime,
**pas pendant un épisode** — c'est là que les callbacks se désordonneraient. `esp_now_send` renvoie
`ESP_ERR_ESPNOW_NO_MEM` quand la file est pleine (« you can delay a while before sending the next
data », `esp_now.h:185`) : c'est l'exact pendant de `udp_errors`.

**Combien d'essais avant `FAIL` — non établi.** #60 § 2.4 a lu dans `lmacInit` les deux limites de
retransmission à 32 ; qu'une trame d'action ESP-NOW soit soumise aux mêmes limites que les données
n'est écrit nulle part. **À vérifier au banc** (passe 2, en comptant les *Retry* côté récepteur).

**La roue ne dort plus — établi par la configuration.** Le modem-sleep est un mode de **station
associée** : il suit les DTIM d'un AP. Une roue ESP-NOW n'est associée à rien. Pour le cas « non
associée », l'IDF installé n'a d'économie d'énergie que si `CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE`
est posé — il ne l'est pas (`sdkconfig:1253`) — et l'en-tête précise : « If never configured
wake_window, the chip would keep waked at disconnected once it uses esp_now » (`esp_now.h:320`).
M4 disparaît, et avec lui l'asymétrie de #52 § 3.5.

**Ce que ça ne change pas.** M2 reste entier : même canal 2,4 GHz, même CSMA/CA, mêmes voisins.
Pour la grille de 10 s : si elle vient de l'AP de la box (M1), ESP-NOW y échappe ; si elle vient du
canal 6 lui-même (M2), ESP-NOW sur le canal 6 la subira — et ESP-NOW sur le canal 1, non.

### 4.4 Ce que l'on gagne en observabilité

**Côté récepteur — établi pour IDF ≥ 5.1.** Le callback reçoit `esp_now_recv_info_t`, dont
`rx_ctrl` pointe un `wifi_pkt_rx_ctrl_t`. Ses champs, lus dans l'IDF installé
(`esp_wifi_types.h:387-411`, identiques en substance en 5.x) : `rssi` (« RSSI of packet. unit:
dBm »), `rate`, `noise_floor`, et `timestamp` — « The local time when this packet is received. It
is precise only if modem sleep or light sleep is not enabled. unit: microsecond ». Un récepteur ne
dort pas : l'horodatage est valide.

C'est **le RSSI par trame que #51 a jugé inaccessible** : là-bas, le seul écrivain de l'octet
renvoyé par `WiFi.RSSI()` était la balise (#51 § 1.3), donc 3 à 10 valeurs par seconde. Ici, une
valeur **par trame reçue**, à la cadence d'émission — 4 × 50 Hz, ~100 échantillons par tour de
roue. Il mesure l'atténuation roue → récepteur au lieu de AP → roue ; pour lire l'effet de
l'orientation de l'antenne, c'est le même canal radio pris dans l'autre sens. Que les deux sens
soient réciproques à ce grain est une **hypothèse** raisonnable (même antenne, même fréquence),
non vérifiée.

**Côté récepteur, en IDF v4.4.7** (si on le laissait en arduino-esp32 2.0.17) : le callback ne
porte que l'adresse. Il faudrait le mode promiscuous du récepteur, que #51 § 5 déconseille sous
trafic soutenu — sur une station qui émet ; sur un récepteur qui n'émet rien, l'objection tombe
peut-être, **non vérifié**. Le chemin propre est de construire le récepteur en arduino-esp32 3.3
(pioarduino) : il n'a pas d'historique, et la roue n'a pas à suivre (§ 4.1).

**Les retransmissions, vues du récepteur — hypothèse.** En promiscuous, le récepteur voit l'en-tête
MAC de chaque essai qu'il décode, bit *Retry* compris. Ce serait un comptage des retransmissions
**par trame**, borne inférieure (un essai non décodé ne se voit pas), que #60 § 3.3 a déclaré
introuvable côté émetteur. Le callback ESP-NOW, lui, ne livre que le corps.

**Côté roue.** Le callback d'envoi est une **API publique** et porte, en IDF v4.4.7, le même
booléen que `txStatus` (#60 § 1, dernière ligne du tableau) — aveugle à une trame acquittée au
32ᵉ essai. En IDF 5.5, `wifi_tx_info_t` ajoute le **débit PHY** effectivement employé, mais pas le
nombre d'essais ; et la roue n'y est pas. La date de fin d'émission par trame, que #60 a trouvée
dans `esp_wifi_set_tx_done_cb`, reste obtenable de la même façon (l'instant où le callback
d'envoi s'exécute), avec la même réserve sur le rattachement à *sa* trame.

### 4.5 Le plan de config

**Établi : le lien est bidirectionnel.** ESP-NOW n'a ni client ni serveur ; chaque côté ajoute
l'autre comme pair (`esp_now_add_peer`) et émet. Les commandes `CFG_*` et l'ACK peuvent donc
passer par le même lien, aux mêmes octets — l'ACK fait 226 octets, sous les 250 (§ 4.1).

Ce qui change dans `ConfigServer` : aujourd'hui `poll()` lit un datagramme par tour de `loop()`
(`config_server.cpp:13-19`). En ESP-NOW, la réception arrive dans le callback, qui « runs from the
Wi-Fi task. So, do not do lengthy operations […] post the necessary data to a queue » (doc v4.4.7)
— donc une file FreeRTOS remplie par le callback et vidée par `poll()`. `CFG_SET_HOST` n'a plus
d'objet (il n'y a plus d'IP) ; l'adresse du pair devient le « host ».

### 4.6 La liaison USB vers l'hôte

**Établi.** Le port USB natif de l'ESP32-S3 est « full-speed USB OTG interface, compliant with the
USB 1.1 specification » ([ESP32-S3-DevKitC-1, guide v1.1](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.1.html)),
soit 12 Mbit/s par trames de 1 ms. Notre débit — 200 trames × ~50 octets, ~10 ko/s, plus quelques
octets de métadonnées par trame — en est à moins de 1 %.

Deux pièges établis, et un à éviter :

- **Ne pas passer par un pont USB-UART.** Le guide de la DevKitC-1 annonce son pont à « up to
  3 Mbps », ce qui suffirait en débit, mais un pont a sa propre politique d'envoi : chez FTDI, le
  « latency timer » vaut **16 ms par défaut** — le pont garde les octets jusqu'à ce qu'il expire
  ([FTDI AN232B-04](https://www.ftdichip.com/old2020/Support/Documents/AppNotes/AN232-04.pdf)).
  C'est exactement une queue de latence fabriquée par un réglage invisible. Le port natif de l'S3
  n'a pas d'intermédiaire.
- **Dans arduino-esp32 2.0.17, chaque `Serial.write` vide aussitôt le tampon TinyUSB**
  (`USBCDC.cpp:400-406` : `tud_cdc_n_write` puis `tud_cdc_n_write_flush`), donc pas de mise en
  tampon côté récepteur — et **il renvoie 0 sans rien envoyer tant que l'hôte n'a pas ouvert le
  port** (`!tud_cdc_n_connected(itf)`, ligne 377). Le récepteur doit compter ce qu'il jette.
- La doc du contrôleur USB Serial/JTAG (l'autre port USB de l'S3) prévient que « in rare cases, it
  is possible that data sent from ESP32-S3 to the host gets 'stuck' in host memory. Sending more
  data will get it 'unstuck' » ([USB Serial/JTAG Console, v5.5](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/api-guides/usb-serial-jtag-console.html)).
  À 200 trames/s on envoie sans cesse, mais c'est une raison de préférer le CDC TinyUSB (mode
  OTG, celui de la Feather) au Serial/JTAG — **à vérifier**.

**La latence et la gigue de l'USB sur macOS ne sont documentées nulle part que j'aie trouvé.** Le
plafond structurel est la trame de 1 ms ; ce que le pilote CDC de macOS ajoute est **à mesurer**
(passe 3).

**Ce que ça fait à `ts_rx_us`.** L'hôte n'a plus de date noyau par datagramme
(`SO_TIMESTAMP_MONOTONIC` de `bench/capture.py` est une option de socket) : il lit un flux d'octets.
Mais le récepteur peut estampiller chaque trame **à sa réception radio** (`rx_ctrl.timestamp`, ou
`esp_timer_get_time()` dans le callback) et joindre cet horodatage au relais. C'est une date plus
proche de l'air que la date noyau d'aujourd'hui, qui inclut la radio du Mac et son pilote.

### 4.7 Ce que ça fait à la synchro d'horloge (#52)

**Plus simple en principe, avec une horloge de plus.** La note #52 bute sur une asymétrie
irréductible : le modem-sleep retarde le sens hôte → ESP jusqu'à un DTIM, pas l'autre (#52 § 3.5).
En ESP-NOW, la roue ne dort pas (§ 4.3), et il n'y a pas d'AP pour tamponner : l'échange à quatre
estampilles de #52 entre roue et récepteur redevient symétrique **par construction** — à M2 près,
qui frappe les deux sens.

En contrepartie, la chaîne a trois horloges au lieu de deux : roue, récepteur, hôte. Le lien
récepteur ↔ hôte est l'USB, symétrique et borné par la trame de 1 ms ; le lien roue ↔ récepteur est
la radio, où chaque trame reçue porte déjà sa date de réception. Une hypothèse à poser devant #65 :
synchroniser **roue ↔ récepteur** par ESP-NOW (le récepteur répond immédiatement depuis une tâche,
sans le `DRAIN_BUDGET_MS` de la roue), et **récepteur ↔ hôte** par USB. Que cela batte la
synchro UDP est **à vérifier** ; ce n'est pas plus dur.

### 4.8 Coût côté conductor

Lu dans le code de ce dépôt :

| Pièce | Aujourd'hui | En ESP-NOW |
|---|---|---|
| `transport/protocol.py` (`parse_packet`, `parse_ack`, `build_*`) | décode les octets du datagramme | **inchangé** si le relais porte ces octets ; une enveloppe de relais (longueur, RSSI, horodatage récepteur) se décode *autour* |
| `transport/udp_receiver.py` (157 lignes) | `asyncio.DatagramProtocol`, `last_esp_ip`, `SO_RCVBUF` | remplacé par un lecteur série : encadrement **COBS ou SLIP** (un flux d'octets n'a pas de frontière), resynchronisation, même `put_nowait`, même compteur `dropped`. `pyserial` (déjà prévu par #81) ou `termios` + `loop.add_reader` en stdlib |
| `transport/esp_configurator.py` (285 lignes) | socket UDP 4211, `resolve()` mDNS, `set_host`, attente d'ACK | même `build_*`/`parse_ack`, sur le port série ; `resolve`, `set_host`, `esp_net` et l'auto-détection par IP de `log_stats` **disparaissent** ; l'identification devient celle du port USB |
| `transport/esp_health.py` | heartbeat + conformité des flux | **inchangé** pour la présence ; `rssi_dbm` du heartbeat vaut 0 hors association et doit venir du récepteur |
| `core.accept_live` | filtre à la socket pendant un replay | inchangé, appliqué au lecteur série |
| `simulator/wire.py`, `simulator/esp32.py` | parlent UDP sur deux ports | le simulateur doit écrire dans un **pseudo-terminal** (`os.openpty`, stdlib) ; `wire.py` compose déjà les octets, il gagne l'encadrement |
| `bench/capture.py` (473 lignes) | `recvmsg` + `SO_TIMESTAMP_MONOTONIC` | une variante série ; la date noyau est remplacée par la date de réception radio du récepteur |
| `config.py` | IP, ports, `ESP_HOST`, mode SIM | un chemin de port série en plus |

Ordre de grandeur : un module neuf (~150 lignes), deux modules amputés, le simulateur et le banc
adaptés, et leurs tests. Rien dans `model/`, `storage/`, `api/`, ni dans le schéma CSV.

### 4.9 Coût côté firmware

- **Roue** (dépôt firmware, lecture seule ici) : `_sendPacket` (`report_manager.cpp:209-222`)
  remplace `beginPacket/write/endPacket` par un `esp_now_send` sur un tampon assemblé (l'API copie :
  « The buffer pointed to by data argument does not need to be valid after `esp_now_send`
  returns ») ; `_sendData` compte `ESP_ERR_ESPNOW_NO_MEM` là où il compte `udp_errors` ;
  `ConfigServer` reçoit par file (§ 4.5) ; `setup()` perd `WiFi.begin`, le mDNS et
  `handleWiFiBlocking()` — qui, aujourd'hui, **bloque toute la boucle** pendant une reconnexion
  (`main.cpp:55-67`), et n'aurait plus d'objet. La roue peut rester en arduino-esp32 2.0.17.
- **Récepteur** : un firmware neuf — `WiFi.mode(WIFI_STA)`, canal fixe, `esp_now_init`, un pair,
  un callback qui pousse dans une file, une tâche qui encadre et écrit sur l'USB, et le sens
  inverse pour les commandes. Une centaine de lignes, en arduino-esp32 3.3 pour avoir `rx_ctrl`.
  **Un second firmware à maintenir**, avec son propre dépôt ou un second `env` PlatformIO.

### 4.10 Portée, antennes, plusieurs roues

- **Antennes.** La roue garde l'antenne intégrée de la Feather. Le récepteur peut avoir une
  antenne externe : l'ESP32-S3-WROOM-1**U** « comes with an external antenna connector »
  (guide DevKitC-1 v1.1), et la Seeed XIAO ESP32S3 a un connecteur d'antenne avec une antenne
  fournie dans le paquet ([wiki Seeed](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/)).
  Le récepteur se place au bout d'une rallonge USB, au bord de scène — **5 m en USB passif** au plus
  (limite courante de câblage USB 2.0, non citée ici d'une norme), contre 100 m d'Ethernet pour un AP.
- **Plusieurs roues.** `ESP_NOW_MAX_TOTAL_PEER_NUM = 20` (`esp_now.h:48`) : un récepteur accepte
  plusieurs émetteurs. Mais l'occupation du canal s'additionne : deux roues au débit par défaut,
  ~58 %. Le débit doit être réglé (§ 4.2) avant toute deuxième roue. En infrastructure, un AP
  accepte plusieurs stations sans que la question se pose dans ces termes.
- **Chiffrement.** Optionnel ; « the maximum number of different LMKs is six » (doc v4.4.7). Sans
  objet pour une roue, à noter si un public peut injecter.

### 4.11 Ce qu'on perd

- **Les outils IP** : `ping`, Wireshark sur IP, `tcpdump` sur l'interface de l'hôte. Les trames
  ESP-NOW restent visibles par une carte en mode moniteur, mais c'est un outillage de plus.
- **Plusieurs consommateurs du flux brut sur le réseau** : sans objet aujourd'hui, l'hôte est le
  seul, et la diffusion aval (WebSocket 8081, OSC) ne change pas.
- **L'indépendance vis-à-vis d'un second matériel** : perdue, mais elle l'est aussi avec un AP à
  soi. Le récepteur est un appareil de ~10 €, qu'on double dans la valise ; il n'a besoin ni de
  courant de scène, ni de configuration réseau.
- **Le réseau comme bus de debug** pendant le développement : SIM, extern, tout passe par des
  sockets aujourd'hui.

---

## 5. Les variantes

**L'ESP de la roue en SoftAP — écartée, sauf preuve du contraire.** L'hôte se connecte au réseau de
la roue. Le canal se choisit (bien), il n'y a pas de box (bien), le mDNS et le protocole restent.
Mais **la radio du Mac devient une station 2,4 GHz** — le cas de la passe à 145 ms (E3) — et l'ESP
tamponnerait pour elle si macOS la met en économie d'énergie (même mécanisme M3, retourné). L'hôte
perd en outre son autre réseau. Seul gain propre, et il est réel : en SoftAP, le RSSI par station
est mis à jour à chaque trame reçue (#51 § 1.3 : `hostap_input` appelle `cnx_rc_update_rssi`) —
mais c'est le RSSI du Mac vu par la roue, pas l'inverse. **Hypothèse** : moins bon que les deux
architectures principales sur le seul critère qui compte (la queue).

**Le mode LR — écarté à cette charge** (§ 4.2). 40 à 63 % d'occupation du canal avant
retransmissions, pour une portée dont nous n'avons pas besoin. Et un AP ESP32-S3 en LR est
« incompatible with traditional 802.11 mode because the beacon is sent in LR mode » (doc v4.4.7) :
pas d'hôte ordinaire dessus.

**ESP-NOW et station WiFi sur le même canal — une transition, pas une cible.** La roue reste
associée à un AP (pour la config, ou un futur OTA) et émet ses données en ESP-NOW. Le canal du pair
« must be set as the channel that the local device is on » (doc) : c'est l'AP qui le choisit — on
retrouve E2 — et la roue associée retrouve son modem-sleep (M4). On cumule les contraintes des
deux. Utile seulement pour comparer ESP-NOW et UDP **dans la même passe, sur le même canal** — ce
qui est précisément un usage de banc (passe 2).

---

## 6. La comparaison, résumée

| | AP à soi + UDP | ESP-NOW + récepteur USB |
|---|---|---|
| **E1 box de la salle** | retirée | retirée |
| **E2 canal choisi** | oui, si l'AP se règle | oui, codé des deux côtés |
| **E3 radio de l'hôte** | hors chaîne si Ethernet (adaptateur ?) ; 5 GHz plausible | **hors chaîne par construction** (USB) |
| **E4 modem-sleep** | réglable (balise, DTIM), reste à décider (#63) | **disparu** (station non associée) |
| **E5 retransmissions** | AP : les siennes (descendant) ; montant en creux (`rx_duplicates`, moniteur) | récepteur : *Retry* visibles en promiscuous (hyp.) ; RSSI **par trame** établi en IDF ≥ 5.1 |
| **E6 relevé RF** | `iw survey dump`, moniteur, `spectral-scan` (RouterOS) | à faire à part (le Mac, ou l'ESP en promiscuous) |
| **E7 sans Internet** | oui (OpenWrt, RouterOS) | oui |
| **E8 occupation du canal** | ~4–6 % | **~29 % au défaut** ; ~4–5 % si le débit est réglé (à vérifier) |
| **E9 alimentation** | 5 V USB-C (hAP ax lite, Beryl AX) ou 12 V PD (OpenWrt One) | **par l'hôte** |
| **Synchro #52** | asymétrie DTIM tant que le modem-sleep reste | symétrique par construction ; trois horloges |
| **Coût conductor** | nul | un lecteur série, configurateur amputé, simulateur et banc adaptés |
| **Coût firmware** | identifiant réseau | `_sendPacket`, `ConfigServer`, `setup()` + un second firmware |
| **Matériel** | 60 à 100 $ | ~10 € par récepteur |
| **Ce qu'on perd** | rien | outils IP, mDNS, debug réseau |

---

## 7. La courte liste matérielle

### 7.1 Pour l'architecture 1 — trois familles, trois budgets

Critère éliminatoire commun : **aucun compte ni cloud pour démarrer** (E7). Les trois candidats le
remplissent par leur système (OpenWrt, RouterOS).

| | **MikroTik hAP ax lite** | **GL.iNet GL-MT3000 « Beryl AX »** | **OpenWrt One** |
|---|---|---|---|
| Famille | AP pro réglable, propriétaire | routeur de voyage, OpenWrt | carte « maison » officielle OpenWrt |
| Prix public | **59 $** ([mikrotik.com](https://mikrotik.com/product/hap_ax_lite)) | **98,99 $** ([gl-inet.com](https://www.gl-inet.com/en-us/products/gl-mt3000)) | **89 $** avec boîtier ([SFC, 2024-11-29](https://sfconservancy.org/news/2024/nov/29/openwrt-one-wireless-router-now-ships-black-friday/)) |
| Radio | IPQ-5010, **2,4 GHz seul**, Wi-Fi 6, 2 chaînes, antennes intégrées 4,3 dBi | MT7981B, 2,4 + 5 GHz, Wi-Fi 6 ; 1 antenne interne + **2 externes rétractables** (non amovibles) | MT7981B + MT7976C, 2,4 + 5 GHz, Wi-Fi 6 ; **3 antennes externes amovibles** ([ToH](https://openwrt.org/toh/openwrt/one)) |
| Alimentation | **USB-C 5 V**, 8 W max | **USB-C 5 V / 3 A**, < 8 W (fiche technique ver.20250320) | **USB-C PD 12 V**, ou PoE 802.3af/at sur le port 2,5 G |
| Ventilation | passive | **ventilateur** piloté en température (constaté sur OpenWrt vanilla 23.05.2 : [forum OpenWrt](https://forum.openwrt.org/t/beryl-ax-and-openwrt-and-fan/192016), `bluewavenet`, 2024-03-26 — absent de la fiche) | passive |
| Taille, poids | compact | 120 × 83 × 34 mm, 196 g | 148 × 100,5 mm |
| Ethernet (hôte) | 4 × 1 GbE | 2,5 G WAN + 1 G LAN | 2,5 G WAN + 1 G LAN |
| Système | RouterOS v7, licence niveau 4, paquet `wifi-qcom` | firmware GL (fork d'OpenWrt 21.02), **OpenWrt officiel installable** ([ToH](https://openwrt.org/toh/gl.inet/gl-mt3000)) | OpenWrt officiel, livré avec |
| Canal fixe, 20 MHz | oui (`channel.frequency`, `channel.width`) | oui | oui |
| Balise, DTIM | **oui** (`beacon-interval`, `dtim-period`, [doc WiFi](https://help.mikrotik.com/docs/spaces/ROS/pages/224559120/WiFi)) | oui (`beacon_int`, `dtim_period`) | oui |
| Couper le 802.11b | **non documenté** dans le paquet `wifi` | oui (`legacy_rates 0`, défaut) | oui |
| Compteurs par client | `signal`, `tx-rate`, `rx-rate`, `packets`, `bytes` — **pas de retransmissions** dans la table d'enregistrement | `tx retries`, `tx failed`, `ack signal` (pilote `mt7915`), `rx_duplicates` (debugfs) | idem Beryl AX |
| Relevé RF | `frequency-scan`, `scan`, **`sniffer`** (moniteur), **`spectral-scan`** | `iw survey dump`, moniteur | idem |
| Depuis l'hôte | SSH, API RouterOS | SSH, ubus | SSH, ubus |

**Lecture.**

- **hAP ax lite — le moins cher et le plus simple sur scène** : 5 V USB-C (une batterie USB
  suffit), passif, 2,4 GHz seul (ce qui force l'hôte en Ethernet — c'est E3, pas un défaut). Il
  règle balise et DTIM et sait renifler, mais il **ne rend pas de compteur de retransmissions**
  par client, et la coupure du 802.11b n'est pas documentée. Système fermé, mais local et sans
  compte.
- **Beryl AX — le plus transportable des OpenWrt** : 5 V USB-C, poche, OpenWrt complet, compteurs
  `mt7915`. Deux réserves : un **ventilateur** (bruit sur scène calme, à écouter), et des antennes
  rétractables mais non amovibles. Livré sous le firmware GL ; passer à OpenWrt officiel est
  documenté (« do not keep the configuration »).
- **OpenWrt One — le plus ouvert et le plus évolutif** : OpenWrt officiel d'usine, quasi
  impossible à briquer, **antennes amovibles** (donc remplaçables par des antennes directionnelles
  ou déportées), passif. Deux réserves : **12 V USB-PD** (un chargeur PD 12 V ou une batterie PD,
  pas une batterie 5 V), et un format moins « poche ».

Tous les comportements radio ci-dessus sont des **capacités documentées**, pas des mesures :
qu'un AP réglé ne produise aucune grille, et que ses compteurs disent vrai, est la passe 1.

**Écartés, et pourquoi.** La box de la salle et le partage de téléphone (ni canal, ni DTIM, ni
compteurs — le téléphone n'a valu que par un canal vide, #80) ; un AP « grand public » non
OpenWrt (mêmes défauts que la box) ; les AP professionnels gérés par contrôleur (UniFi, Omada…)
ne sont **pas instruits ici** — leur dépendance à un contrôleur est à vérifier avant de les
considérer.

### 7.2 Pour l'architecture 2 — le récepteur

| | Seeed XIAO ESP32S3 | ESP32-S3-DevKitC-1U | Adafruit Feather ESP32-S3 (comme la roue) |
|---|---|---|---|
| Antenne | connecteur + antenne fournie | connecteur U.FL (module WROOM-1U) | intégrée |
| USB | Type-C | **deux ports** : natif USB-OTG **et** pont USB-UART — utiliser le **natif** (§ 4.6) | natif |
| Intérêt | minuscule, antenne déportable | référence Espressif | même carte que la roue, pièce de rechange commune |

Prix de l'ordre de 10 à 20 €, **non relevés ici** — à vérifier au moment d'acheter. Le
prototype de la passe 2 demande **un** récepteur ; la Feather de rechange, si elle existe, suffit
pour commencer.

---

## 8. Le protocole de vérification

Toutes les passes sont des passes de `bench/capture.py`, à la charge de #80 (50 Hz, quatre flux),
de 5 min, avec les mêmes déplacements et occultations ponctuelles, et le relevé RF de la salle au
début (`rf_snapshot`). Les sorties attendues sont des **grandeurs** — retard p50/p95/p99/p99,9,
perte, `udp_errors`, présence de la grille (R de Rayleigh, p corrigé du balayage, comme #80) —
jamais des verbes de verdict (#59).

### Passe 1 — l'AP à soi, et la séparation « box » / « canal » (aucun code)

Un AP de la courte liste, configuré : canal fixe, HT20, `legacy_rates 0`, DTIM 1, aucun
balayage ; l'hôte **en Ethernet**. La box SFR reste allumée, à sa place, en canal auto.

| Passe | Canal de l'AP | Ce qu'elle montre |
|---|---|---|
| 1a | **6** (celui de la box) | grille présente ⇒ M2 (le canal et ses voisins) ; absente ⇒ M1 (l'équipement) |
| 1b | 1 ou 11 (le plus vide au relevé) | la ligne de base de l'AP à soi |
| 1c | comme 1b, DTIM 3 | ce que le DTIM coûte avec le modem-sleep par défaut — croise #63 |

Côté AP, pendant chaque passe, relever toutes les secondes par SSH : `iw station dump` (signal,
`tx retries`, `tx failed`) et `survey dump` du canal, et lire `rx_duplicates` en début et fin.
**Ce qu'elle doit montrer** : si 1a reproduit la grille, ESP-NOW sur le canal 6 ne la retirera
pas non plus, et **la seule chose qui compte est de choisir le canal** — ce que les deux
architectures permettent. Si 1a ne la reproduit pas, c'était la box, et l'AP à soi suffit à E1.

### Passe 2 — le prototype ESP-NOW, contre l'UDP, sur le même canal

**Le prototype** : dans la copie de banc du firmware (#81 — pas de `#if` en production), la
variante ESP-NOW de `_sendPacket` derrière un drapeau de compilation, plus un firmware de
récepteur (arduino-esp32 3.3 via pioarduino) qui relaie en COBS sur l'USB les octets reçus, avec
`rx_ctrl.rssi`, `rx_ctrl.timestamp` et `rx_ctrl.rate` ; côté hôte, une variante série de
`bench/capture.py`. Pas de plan de config dans le prototype : la composition des flux est figée à
la compilation. **Prix** : un récepteur (~10–20 €, ou une Feather de rechange), et de l'ordre de
**deux jours** de développement — estimation, pas mesure.

| Passe | Configuration | Ce qu'elle montre |
|---|---|---|
| 2a | ESP-NOW au débit par défaut (1 Mbit/s), canal de 1b | l'occupation réelle (survey de l'AP éteint, ou relevé du Mac) et la queue, contre 1b |
| 2b | ESP-NOW à débit réglé (`esp_wifi_config_espnow_rate`, HT MCS0 puis MCS7) | que le réglage est honoré (`rx_ctrl.rate`), et ce qu'il coûte en perte à 5–10 m |
| 2c | ESP-NOW sur le canal 6, si 1a a montré la grille | que la grille revient — ou non |

En plus de la queue : le **RSSI par trame** replié sur `spin_deg` (le travail de #69, gratuitement
enrichi) ; le nombre de `FAIL` et de `NO_MEM` côté roue ; et, en promiscuous sur le récepteur, la
part des trames reçues avec le bit *Retry* (hypothèse du § 4.4).

### Passe 3 — l'USB seul

Le récepteur émet des trames synthétiques horodatées (`esp_timer_get_time()`) à 200/s sur l'USB,
sans radio ; l'hôte les date en `time.monotonic_ns()`. Après retrait de la dérive linéaire, la
distribution de l'écart **est** la gigue de l'USB et du pilote CDC de macOS. **Ce qu'elle doit
montrer** : une queue bornée par quelques millisecondes ; sinon, l'USB ajoute ce que la radio
retire.

### Passe 4 — la synchro, si ESP-NOW passe

L'échange de #52 entre roue et récepteur, sur ESP-NOW, contre le même échange en UDP avec
modem-sleep : la dispersion de θ sur dix épisodes. **Ce qu'elle doit montrer** : que l'asymétrie
de #52 § 3.5 disparaît. À brancher sur #65.

---

## 9. Ce qu'il faut essayer d'abord

**La passe 1, avec l'appareil le moins cher de la liste — le hAP ax lite, ou le Beryl AX si l'on
veut OpenWrt et ses compteurs de retransmissions.** Trois raisons, dans l'ordre :

1. **Elle ne demande aucune ligne de code**, ni ici ni dans le firmware : un appareil, une
   configuration, un identifiant réseau.
2. **Elle tranche la question que #80 a laissée ouverte** — l'équipement ou le canal — et la
   réponse décide de la valeur d'ESP-NOW sur l'axe « latence » : si la grille vient du canal,
   ESP-NOW ne l'aurait pas retirée ; si elle vient de la box, un AP à soi suffit.
3. **Elle produit la ligne de base** (1b) contre laquelle le prototype ESP-NOW devra être comparé,
   au même endroit, sur le même canal.

**Le prototype ESP-NOW ne se justifie pas par la latence avant la passe 1. Il se justifie déjà par
l'observabilité** — le RSSI par trame (§ 4.4), la roue sans modem-sleep (§ 4.3), la synchro
symétrique (§ 4.7) — **et par l'alimentation** : rien à brancher sur scène. Commander un récepteur
en même temps que l'AP coûte une dizaine d'euros et ne décide rien ; écrire le prototype attend
la passe 1. La tournée se décide après les passes.

---

## Sources

**Code installé sur cette machine** (autorité pour « ce qui tourne sur la roue ») :
`~/.platformio/platforms/espressif32/platform.json:21` (6.10.0) ·
`~/.platformio/platforms/espressif32/boards/adafruit_feather_esp32s3_nopsram.json` ·
`~/.platformio/packages/framework-arduinoespressif32/` — `package.json` · `boards.txt:10320-10321` ·
`cores/esp32/USBCDC.cpp:375-417` · `tools/sdk/esp32s3/include/esp_wifi/include/esp_now.h:48, 51, 89, 96, 171-189, 244, 311-326` ·
`tools/sdk/esp32s3/include/esp_wifi/include/esp_wifi_types.h:237, 387-423, 593, 624-625` ·
`tools/sdk/esp32s3/sdkconfig:280-288, 1184, 1225-1226, 1253, 1258`.

**Firmware de la roue** (lecture seule, `src/wifi_config.h` non ouvert) :
`platformio.ini` · `src/protocol.h` · `src/main.cpp:55-67, 70-91` · `src/config_server.cpp:13-19, 90-141` ·
`src/report_manager.h:26-28` · `src/report_manager.cpp:182-233`.

**Ce dépôt** : `transport/udp_receiver.py` · `transport/esp_configurator.py` · `transport/protocol.py:192` ·
`bench/capture.py` · `simulator/wire.py` · notes `docs/research/cadence-rssi-esp32.md` (#51),
`grandeurs-lien-wifi-esp32.md` (#60), `synchro-horloge-esp32.md` (#52).

**ESP-IDF, en-têtes aux tags consultés :**
[v4.4.7](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_now.h) ·
[v5.0](https://github.com/espressif/esp-idf/blob/v5.0/components/esp_wifi/include/esp_now.h) ·
[v5.1](https://github.com/espressif/esp-idf/blob/v5.1/components/esp_wifi/include/esp_now.h) ·
[v5.3](https://github.com/espressif/esp-idf/blob/v5.3/components/esp_wifi/include/esp_now.h) ·
[v5.4](https://github.com/espressif/esp-idf/blob/v5.4/components/esp_wifi/include/esp_now.h) ·
[v5.5](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_wifi/include/esp_now.h) ·
[v5.5 `esp_wifi_types_generic.h` (`wifi_tx_info_t`)](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_wifi/include/esp_wifi_types_generic.h).

**Documentation Espressif :**
[ESP-NOW, v4.4.7 / esp32s3](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-reference/network/esp_now.html) ·
[ESP-NOW, v5.5 / esp32s3](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/api-reference/network/esp_now.html) ·
[Wi-Fi Driver (Long Range, Power-saving), v4.4.7 / esp32s3](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-guides/wifi.html) ·
[USB Serial/JTAG Console, v5.5 / esp32s3](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/api-guides/usb-serial-jtag-console.html) ·
[ESP32-S3-DevKitC-1, guide v1.1](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.1.html).

**arduino-esp32 et PlatformIO :**
[arduino-esp32 3.0.0](https://github.com/espressif/arduino-esp32/releases/tag/3.0.0) ·
[3.2.0](https://github.com/espressif/arduino-esp32/releases/tag/3.2.0) ·
[3.3.0](https://github.com/espressif/arduino-esp32/releases/tag/3.3.0) ·
[3.3.12](https://github.com/espressif/arduino-esp32/releases/tag/3.3.12) ·
[platform-espressif32, notes de version](https://github.com/platformio/platform-espressif32/releases) ·
[platform-espressif32#1225](https://github.com/platformio/platform-espressif32/issues/1225) ·
[pioarduino/platform-espressif32](https://github.com/pioarduino/platform-espressif32).

**Linux / OpenWrt :**
[OpenWrt, configuration Wi-Fi](https://openwrt.org/docs/guide-user/network/wifi/basic) ·
[iw `station.c`](https://git.kernel.org/pub/scm/linux/kernel/git/jberg/iw.git/tree/station.c) ·
[mt76 `mt7915/main.c`](https://github.com/openwrt/mt76/blob/master/mt7915/main.c) ·
[mt76 `mt7915/soc.c`](https://github.com/openwrt/mt76/blob/master/mt7915/soc.c) ·
[mac80211 `debugfs_sta.c`](https://github.com/torvalds/linux/blob/master/net/mac80211/debugfs_sta.c) ·
[mac80211 `rx.c`](https://github.com/torvalds/linux/blob/master/net/mac80211/rx.c) ·
[ToH OpenWrt One](https://openwrt.org/toh/openwrt/one) ·
[ToH GL-MT3000](https://openwrt.org/toh/gl.inet/gl-mt3000).

**Fabricants :**
[MikroTik hAP ax lite](https://mikrotik.com/product/hap_ax_lite) ·
[RouterOS — WiFi](https://help.mikrotik.com/docs/spaces/ROS/pages/224559120/WiFi) ·
[RouterOS — Wireless Interface (paquet historique)](https://help.mikrotik.com/docs/spaces/ROS/pages/8978446/Wireless+Interface) ·
[GL.iNet GL-MT3000](https://www.gl-inet.com/en-us/products/gl-mt3000) ·
[fiche technique GL-MT3000 ver.20250320](https://static.gl-inet.com/www/images/products/datasheet/mt3000_datasheet_20250320.pdf) ·
[SFC — OpenWrt One en vente](https://sfconservancy.org/news/2024/nov/29/openwrt-one-wireless-router-now-ships-black-friday/) ·
[Seeed XIAO ESP32S3](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/) ·
[FTDI AN232B-04, Data Throughput, Latency and Handshaking](https://www.ftdichip.com/old2020/Support/Documents/AppNotes/AN232-04.pdf).

**Tiers, cités comme tels :**
[forum OpenWrt — Beryl AX and OpenWRT and fan](https://forum.openwrt.org/t/beryl-ax-and-openwrt-and-fan/192016) (`bluewavenet`, 2024-03-26).

**Calculs propres à cette note** (§ 4.2) : temps d'air par trame d'après les paramètres
802.11b (préambule long 192 µs, DIFS 50 µs, CWmin 31, créneau 20 µs) et 802.11n HT20 (préambule
mixte 36 µs, symboles 4 µs, créneau court 9 µs, CWmin 15), ACK compris. Ce sont des ordres de
grandeur pour comparer, pas des mesures ; la passe 2 les remplace.
