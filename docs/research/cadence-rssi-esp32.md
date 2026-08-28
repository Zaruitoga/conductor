# À quelle cadence `WiFi.RSSI()` se rafraîchit-il réellement sur un ESP32-S3 ?

Recherche pour le ticket [#51](https://github.com/Zaruitoga/conductor/issues/51), sous la carte
[#49](https://github.com/Zaruitoga/conductor/issues/49) « performances du lien WiFi en conditions
d'installation ».
Recherche seulement : **aucun correctif n'est appliqué ici**, ni dans ce dépôt ni dans le firmware.

**Enjeu.** La roue tourne à ~2 tr/s et l'antenne est dans le tube : elle balaie un tour complet
d'orientation en 500 ms. Le firmware lit le RSSI une seule fois par heartbeat, soit toutes les 2 s
(`report_manager.cpp:222`) — **un échantillon toutes les quatre révolutions**. Pour lire une
modulation à 2 Hz (un lobe par tour) ou 4 Hz (les deux nuls d'un diagramme d'antenne), il faut des
valeurs *indépendantes* à bien plus de 8 Hz. La question n'est donc pas « peut-on appeler
`WiFi.RSSI()` à 100 Hz » — on le peut — mais « la radio écrit-elle quelque chose de neuf entre deux
appels ».

---

## Verdict

**Non. La valeur n'est réécrite qu'à la réception d'une balise du point d'accès — au mieux ~9,8 Hz,
et ~3,3 Hz dans la configuration actuelle, où le modem-sleep est actif par défaut sans que personne
l'ait choisi. L'échantillonner à 100 Hz rendrait dix à trente fois la même valeur. Le RSSI haute
cadence ne mérite pas sa place dans le protocole permanent.**

Les six points décisifs, et ce qu'il faut accepter avec :

| | Établi | Contrepartie |
|---|---|---|
| **1. Ce qui est renvoyé** | Le RSSI **brut de la dernière balise**, pas une moyenne. Le header d'Espressif le dit (`esp_wifi.h:1394`) et le désassemblage du blob le confirme : la moyenne glissante existe, elle est dans un **champ voisin** que rien n'expose. | Bonne nouvelle inattendue : si la valeur bougeait, elle bougerait franchement. C'est la cadence qui tue, pas le lissage. |
| **2. Qui l'écrit** | **Un seul écrivain côté STA** : `scan_parse_beacon()`, appelé par `sta_recv_mgmt()`. Ni le chemin de données, ni le chemin ACK, ni `libpp` ne touchent cet octet. | Établi par désassemblage du blob livré, pas par du code source ouvert (§ 1.3). Corroboré par la doc du fabricant. |
| **3. La cadence** | Balise standard = 102,4 ms ⇒ **plafond 9,8 Hz**, soit 4,9 échantillons par tour de roue. Avec le modem-sleep par défaut (`WIFI_PS_MIN_MODEM`) : un réveil par DTIM, **3,3 Hz** à DTIM 3, soit 1,6 échantillon par tour. | L'intervalle de balise appartient à l'AP. On ne le règle pas, et la carte a mis « comparer les AP » hors périmètre. |
| **4. Le coût d'un appel** | ⚠️ **`WiFi.RSSI()` n'est pas un accès mémoire.** C'est un `zalloc(24)`, un mutex partagé avec la tâche WiFi, un **post vers la tâche WiFi sur l'autre cœur**, un **blocage sans timeout** sur sémaphore, puis un `free`. À 0,5 Hz c'est gratuit ; à 100 Hz depuis `loop()`, c'est un suspect de premier rang pour #49. | `esp_wifi_sta_get_rssi()` lit **le même octet** en trois instructions sous un verrou. Si on garde une lecture de RSSI, c'est celle-là qu'il faut appeler — y compris pour le heartbeat actuel. |
| **5. Le RSSI par paquet** | Atteignable *en principe* : le mode promiscuous est documenté comme compatible avec un STA connecté, et le filtre de sous-type **ACK** existe. | Espressif écrit noir sur blanc qu'il ne faut **pas** l'activer sous trafic soutenu. Et que l'ACK qui nous est adressé remonte réellement au callback **n'est nulle part documenté** — § 5 donne la mesure qui trancherait. |
| **6. Ce qui caractérise la rotation à la place** | Le **retard par paquet**, replié sur `spin_deg` : cadence 100–200 Hz, **zéro firmware**, déjà mesurable sur les takes existantes. C'est la piste que la carte a déjà notée. | Ne dit rien du RSSI. Mesure l'effet, pas la cause — ce qui est exactement ce que la carte demande. |

**Découverte incidente, qui n'est pas de ce ticket mais le déborde** : le firmware n'appelle jamais
`WiFi.setSleep()`, donc l'ESP32-S3 tourne en **modem-sleep minimum**, où « RF, PHY et BB sont
éteints » entre deux DTIM. Personne ne l'a choisi : c'est le défaut d'arduino-esp32 (§ 2.2). C'est
un candidat sérieux pour les épisodes périodiques de #49, et c'est **une ligne** à essayer.

**Ce qui n'est pas vérifiable ici** : § 6.

---

## 0. La version qui fait foi

Le firmware est `~/Desktop/IMU_project/PIO projects/260524_BNO_super_reports`,
`platformio.ini` : `platform = espressif32`, `board = adafruit_feather_esp32s3_nopsram`,
`framework = arduino`, sans épinglage de version. Ce qui est **effectivement installé** sur cette
machine, et donc ce qui fait foi :

```
~/.platformio/packages/framework-arduinoespressif32/package.json
  "version": "3.20017.241212+sha.dcc1105b"
```

| | Version | Source lue |
|---|---|---|
| arduino-esp32 | **2.0.17** | `cores/esp32/esp_arduino_version.h:22-26` (2 / 0 / 17) |
| ESP-IDF embarqué | **v4.4.7** | `tools/sdk/esp32s3/include/esp_common/include/esp_idf_version.h:22-26` (4 / 4 / 7) |
| cible | `esp32s3` | `tools/sdk/esp32s3/sdkconfig:7` — `CONFIG_IDF_TARGET="esp32s3"` |

Tout ce qui suit est lu dans **ces fichiers-là**, pas sur `master`. Les liens GitHub pointent sur les
tags [`arduino-esp32@2.0.17`](https://github.com/espressif/arduino-esp32/tree/2.0.17) et
[`esp-idf@v4.4.7`](https://github.com/espressif/esp-idf/tree/v4.4.7), vérifiés identiques aux
fichiers locaux (numéros de ligne compris). La documentation citée est la version
[v4.4.7 / esp32s3](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/), pas `latest`.

Une part du WiFi est un **blob fermé** : `libnet80211.a` et `libpp.a` dans
`tools/sdk/esp32s3/lib/`. Là où la réponse s'y trouve, elle est obtenue par **désassemblage du
binaire livré** (`xtensa-esp32s3-elf-objdump`, du toolchain PlatformIO). C'est une source primaire —
c'est littéralement le code qui tourne sur la roue — mais ce n'est pas du source, et le § 6 dit où
la lecture reste une inférence.

---

## 1. Ce que `WiFi.RSSI()` renvoie, jusqu'à l'octet

### 1.1 La couche Arduino ne fait rien

[`libraries/WiFi/src/WiFiSTA.cpp:726-736`](https://github.com/espressif/arduino-esp32/blob/2.0.17/libraries/WiFi/src/WiFiSTA.cpp#L726-L736) :

```cpp
int8_t WiFiSTAClass::RSSI(void)
{
    if(WiFiGenericClass::getMode() == WIFI_MODE_NULL){
        return 0;
    }
    wifi_ap_record_t info;
    if(!esp_wifi_sta_get_ap_info(&info)) {
        return info.rssi;
    }
    return 0;
}
```

Aucun cache, aucun lissage, aucune mémoire : tout se joue dans `esp_wifi_sta_get_ap_info()`. Noter
au passage que la valeur renvoyée à un appel raté est **`0`**, pas une sentinelle — un `0 dBm` est
une valeur physiquement possible (le header prévient que « in some rare cases where signal strength
is very strong, rssi values can be slightly positive »), donc le firmware ne peut pas distinguer
« pas connecté » de « très fort ». Le heartbeat porte ce `0` tel quel aujourd'hui.

*(Vérifié : arduino-esp32 **3.x** ne change rien — `STA.cpp::RSSI()` passe toujours par
`esp_wifi_sta_get_ap_info`. Une montée de version ne corrigerait ni la cadence ni le coût.)*

### 1.2 Deux API, un seul octet

`esp_wifi_sta_get_ap_info` et `esp_wifi_sta_get_rssi` sont toutes deux définies dans
`libnet80211.a`, membre `ieee80211_api.o` — donc invérifiables en source. Le désassemblage donne
les deux chemins.

`esp_wifi_sta_get_rssi` (dix-neuf instructions) :

```
entry
  a8 = wifi_api_lock()        ; verrou
  a8 = g_ic                   ; état global de l'interface
  a8 = [a8 + 16]              ; -> l'interface STA
  a8 = [a8 + 228]             ; -> l'enregistrement du BSS associé
  a8 = sext(byte [a8 + 166])  ; <<<< l'octet
  [a2] = a8
  wifi_api_unlock()
retw
```

`esp_wifi_sta_get_ap_info` passe par un ioctl (§ 3), dont le corps est
`wifi_get_ap_info_process` (`ieee80211_ioctl.o`). Ce corps commence par
`memset(ap_info, 0, 80)` — `sizeof(wifi_ap_record_t)` — puis remplit champ par champ depuis
**le même enregistrement** `g_ic[16][228]` :

| écrit dans `wifi_ap_record_t` | lu à l'offset | champ |
|---|---|---|
| `+39` | `+171` | `primary` |
| `+40` | `+172` | `second` |
| **`+44`** | **`+166`** | **`rssi`** |
| `+48` | `+0x200+139` | `authmode` |
| `+68` (12 o) | `+0x2ac` | `country` |

L'offset 44 est bien `rssi` dans
[`wifi_ap_record_t`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_wifi_types.h#L190-L210)
(`bssid[6]` 0–5, `ssid[33]` 6–38, `primary` 39, `second` 40–43, `rssi` **44**, `authmode` 48…), et
la table entière concorde.

> **Les deux API renvoient exactement la même valeur.** Elles ne diffèrent que par le chemin
> emprunté pour aller la chercher — et ce chemin diffère énormément (§ 3).

### 1.3 Qui écrit cet octet — et qui ne l'écrit pas

Recherche exhaustive des écritures à l'offset 166 dans les deux blobs (`s8i …, 166` sur les 39
objets de `libnet80211.a` et les 20 de `libpp.a`) : **deux écrivains, pas un de plus.**

```
libnet80211/wl_cnx.o  <cnx_rc_update_rssi>   s8i a10, a2, 166
libnet80211/wl_cnx.o  <cnx_update_bss_more>  s8i a7,  a9, 166
```

Et en remontant les relocations, `cnx_rc_update_rssi` n'a que **trois** appelants dans tout le blob :

```
ieee80211_scan.o    scan_parse_beacon        <-- le chemin STA
ieee80211_hostap.o  hostap_input             <-- chemin SoftAP (per-station rssi)
ieee80211_hostap.o  hostap_recv_mgmt         <-- idem
```

`scan_parse_beacon` lui-même n'a qu'un appelant : `sta_recv_mgmt` (`ieee80211_sta.o`), la réception
de **trames de gestion** côté station — balises et réponses de sonde.

La chaîne complète, côté STA :

```
trame de gestion reçue de l'AP
   └─ sta_recv_mgmt()          ieee80211_sta.o
        └─ scan_parse_beacon() ieee80211_scan.o
             └─ cnx_rc_update_rssi(bss, rssi, flag)   wl_cnx.o
                  ├─ bss[166] = rssi          <- brut, ce que les deux API renvoient
                  ├─ bss[164] = (13*bss[164] + 3*rssi)/16   <- la moyenne
                  └─ bss[165] = terme de pente
```

**Ce qui n'y est pas** est aussi tranchant que ce qui y est. `ieee80211_input.o` (le chemin de
données), `ieee80211_output.o`, et **l'intégralité de `libpp.a`** — donc le MAC bas niveau, la
gestion des ACK, `lmac.o`, `hal_mac_rx.o` — ne référencent ni `cnx_rc_update_rssi` ni
`cnx_update_bss_more`. **Ni un paquet de données, ni un ACK ne met à jour la valeur que
`WiFi.RSSI()` renvoie.** C'est la réponse au point qui « décide de tout » dans le ticket.

### 1.4 Trois confirmations indépendantes

1. **Le header d'Espressif**, dans le SDK installé
   ([`esp_wifi.h:1388-1400`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_wifi.h#L1388-L1400)) :

   > `@param rssi store the rssi info received from **last beacon**.`

   C'est la seule phrase de toute la documentation Espressif qui nomme l'événement de mise à jour, et
   elle dit exactement ce que le désassemblage montre. Elle porte sur `esp_wifi_sta_get_rssi` ; le
   § 1.2 établit que `esp_wifi_sta_get_ap_info` lit le même octet, donc elle vaut pour les deux.
   *(La page de doc en ligne, elle, ne reprend pas la ligne `@param` : la
   [référence v4.4.7](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-reference/network/esp_wifi.html)
   se limite à « Get the rssi info after station connected to AP ». Le header installé est la source
   la plus complète.)*

2. **La FAQ d'Espressif** ([`esp-faq`, `software-framework/wifi.rst`](https://github.com/espressif/esp-faq/blob/master/docs/en/software-framework/wifi.rst)),
   sur le lissage :

   > « The previous RSSI has a weight of 13, and the new RSSI has a weight of 3. […]
   > `rssi_avg = rssi_avg*13/16 + new_rssi * 3/16` »

   C'est **littéralement** l'arithmétique lue dans `cnx_rc_update_rssi` (13·précédent + 3·nouveau,
   décalé de 4). Le fabricant décrit cette moyenne pour `esp_wifi_ap_get_sta_list` (mode AP) — et de
   fait, `hostap_input` appelle la même fonction. **Mais la moyenne est écrite en `+164`, pas en
   `+166`.** Ce que nos deux API renvoient est le terme brut. À noter comme correctif d'une
   croyance répandue : `esp_wifi_set_rssi_threshold` dit « if **average** rssi gets lower than
   threshold » ([`esp_wifi.h:1183-1197`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_wifi.h#L1183-L1197)) —
   c'est ce champ-là, celui que rien n'expose en lecture.

3. **Une corroboration externe, à ranger comme telle.** Sur
   [espressif/esp-idf#17664](https://github.com/espressif/esp-idf/issues/17664)
   (« static value of rssi from esp_wifi_sta_get_rssi in esp-idf 5.5 », ouvert par `H1steria` le
   2025-09-29), un commentaire de `balla94` (2026-02-20, `author_association: NONE` — **pas un
   mainteneur Espressif**, et de son propre aveu assisté par un agent) décrit la même chaîne :
   « byte 166 of the BSS node at `g_ic→offset16→offset228` […] updated by `cnx_rc_update_rssi()`
   called from `scan_parse_beacon()` in `ieee80211_scan.o` ». C'est un désassemblage indépendant du
   nôtre, sur une version d'IDF plus récente, qui tombe sur les mêmes offsets. Cela **corrobore**,
   cela ne prouve pas : la preuve reste le § 1.3, mené sur le binaire de cette machine.

   *Aucun mainteneur Espressif n'a répondu sur ce fil* (un commentaire au total, à ce jour). Sur
   [#12685](https://github.com/espressif/esp-idf/issues/12685) (« Need clarification on
   `esp_wifi_sta_get_rssi()` »), fermé avec la résolution « NA », **la seule réponse vient également
   d'un tiers** (`filzek`, 2023-11-29, `NONE`) : « the function call simple grab the last RSSI frame
   info and store it […] it is the same as `ap_wifidata.rssi` ». Correct sur le fond, mais sans
   valeur d'autorité. Le ticket #51 avait raison : **sur ce point, il n'existe pas de réponse
   officielle en prose.** Il en existe une dans le header, et une dans le binaire.

---

## 2. La cadence effective, en Hz

### 2.1 Le plafond : l'intervalle de balise

Une balise 802.11 est émise à l'intervalle annoncé par l'AP, par défaut **100 TU = 100 × 1024 µs =
102,4 ms**, soit **9,77 balises/s**. C'est le plafond absolu du rafraîchissement, et il ne dépend
d'aucun réglage de notre côté : il appartient à l'AP.

Traduit en unités de roue, à 2 tr/s :

| | intervalle | échantillons par tour | angle entre deux |
|---|---|---|---|
| plafond théorique (aucune veille, DTIM 1) | 102,4 ms | **4,9** | 74° |
| modem-sleep, DTIM 3 (courant sur box) | 307 ms | **1,6** | 221° |
| aujourd'hui (heartbeat) | 2 000 ms | **0,25** | 4 tours |

4,9 échantillons par tour place le fondamental de rotation (2 Hz) juste sous Nyquist et son
harmonique 2 (4 Hz, les deux nuls d'un diagramme d'antenne — l'hypothèse la plus probable pour une
antenne dans un tube) **au-dessus**. Et les balises ne sont pas verrouillées en phase sur la roue,
donc la modulation reviendrait repliée à une fréquence de battement arbitraire. À 1,6 échantillon
par tour, il n'y a plus de question.

**Échantillonner à 100 Hz rendrait donc dix fois (au mieux) à trente et une fois (cas courant) la
même valeur.** C'est exactement le scénario que le ticket redoutait.

### 2.2 Le plancher : personne n'a choisi le modem-sleep

[`WiFiGeneric.cpp:766-770`](https://github.com/espressif/arduino-esp32/blob/2.0.17/libraries/WiFi/src/WiFiGeneric.cpp#L766-L770) :

```cpp
#if CONFIG_IDF_TARGET_ESP32S2
wifi_ps_type_t WiFiGenericClass::_sleepEnabled = WIFI_PS_NONE;
#else
wifi_ps_type_t WiFiGenericClass::_sleepEnabled = WIFI_PS_MIN_MODEM;
#endif
```

L'ESP32-**S3** n'est pas l'S2 : le défaut est `WIFI_PS_MIN_MODEM`. Il est appliqué sans qu'on
demande rien, à l'événement `ARDUINO_EVENT_WIFI_STA_START`
([`WiFiGeneric.cpp:1046`](https://github.com/espressif/arduino-esp32/blob/2.0.17/libraries/WiFi/src/WiFiGeneric.cpp#L1046)) :

```cpp
if(esp_wifi_set_ps(_sleepEnabled) != ESP_OK){
```

Et le firmware n'appelle jamais `WiFi.setSleep()` (vérifié : aucune occurrence dans `src/`). Donc
la roue vole **en modem-sleep minimum**. Ce que la doc v4.4.7 en dit
([Wi-Fi Driver → ESP32-S3 Wi-Fi Power-saving Mode → Station Sleep](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-guides/wifi.html#esp32-s3-wi-fi-power-saving-mode)) :

> « If the Modem-sleep mode is enabled, station will switch between active and sleep state
> periodically. **In sleep state, RF, PHY and BB are turned off** in order to reduce power
> consumption. […] In minimum power save mode, **station wakes up every DTIM to receive beacon.** »

Deux conséquences, et la seconde déborde ce ticket :

1. **Le RSSI ne peut pas se rafraîchir plus vite qu'un DTIM.** La station dort à travers les
   balises intermédiaires. DTIM est choisi par l'AP (typiquement 1 à 3) : 9,8 Hz à 3,3 Hz.
2. **La radio est éteinte par intermittence pendant que le spectacle tourne.** C'est un candidat de
   premier rang pour les épisodes de retard groupé décrits dans #49 — et ça n'a jamais été une
   décision, seulement un défaut de framework. `WiFi.setSleep(false)` est une ligne.
   **Ce n'est pas le sujet de ce ticket** ; c'est une piste pour #50/#54, à mesurer avant/après.

> Note : `WIFI_PS_NONE` remonterait le plafond à 9,8 Hz. Ça ne sauve pas le RSSI haute cadence
> (4,9 échantillons/tour restent 4,9), mais ça change *tout le reste*. Les deux effets ne doivent
> pas être confondus dans la même mesure.

---

## 3. Le coût d'un appel — le vrai piège

C'est le point où l'écart entre les deux API cesse d'être académique.

### 3.1 `esp_wifi_sta_get_rssi` : un verrou et trois lectures

Voir § 1.2. Le seul coût est `wifi_api_lock()`, dont le désassemblage
(`ieee80211_api.o`, section `.text.wifi_api_lock`) donne :

```
if (current_task_is_wifi_task()) return;                  ; déjà dans la tâche WiFi -> rien
if (!s_wifi_api_lock) s_wifi_api_lock = recursive_mutex_create();
mutex_lock(s_wifi_api_lock);
```

Un **mutex récursif** (`g_osi_funcs_p->_recursive_mutex_create`, offset 76 dans
[`wifi_os_adapter.h`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_private/wifi_os_adapter.h),
puis `_mutex_lock`, offset 84). Recherche exhaustive des références : outre l'objet qui le définit
(`ieee80211_api.o`, où chaque point d'entrée d'API le prend), `wifi_api_lock` n'est appelé que
depuis `ieee80211_ioctl.o` et `ieee80211_timer.o` — et **depuis aucun objet de `libpp.a`**. C'est
un verrou d'*API*, pas un verrou tenu par le chemin RX/TX du MAC. La contention se limite donc aux
autres appels d'API concurrents, c'est-à-dire, chez nous, à rien. *(Et il ne verrouille même pas
quand l'appelant est déjà la tâche WiFi : premier test de la fonction.)*

### 3.2 `esp_wifi_sta_get_ap_info` : un aller-retour inter-cœurs, bloquant

Le désassemblage (`.text.esp_wifi_sta_get_ap_info` puis `.text.ieee80211_ioctl`) donne la séquence
suivante — les noms des fonctions OSI viennent des offsets de `wifi_osi_funcs_t`, comptés sur
[`wifi_os_adapter.h`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_private/wifi_os_adapter.h) :

```
esp_wifi_sta_get_ap_info(ap_info)
  wifi_init_completed()
  param = g_osi_funcs_p->_wifi_zalloc(24)          ; offset 372  <-- allocation tas
  param->cmd = 8 ; param->handler = wifi_get_ap_info_process ; param->arg = ap_info
  ieee80211_ioctl(param)
      si l'appelant n'est PAS la tâche WiFi :
        param->flags |= 1                          ; « il faudra poster »
        param->flags |= 2                          ; « bloquant »
        param->sem = g_osi_funcs_p->_wifi_thread_semphr_get()   ; offset 68
        wifi_api_lock()                            ; mutex
        wifi_get_init_state() ; wifi_is_stop_in_progress()
        pp_post(6, param)                          ; <-- file de la tâche WiFi
        wifi_api_unlock()
        g_osi_funcs_p->_semphr_take(param->sem, 0xFFFFFFFF)     ; offset 60
                                                   ; <-- BLOCAGE SANS TIMEOUT
      sinon : ieee80211_ioctl_process(param) en direct
  g_osi_funcs_p->_free(param)                      ; offset 176
```

Trois choses à retenir :

- **C'est un aller-retour vers une autre tâche, sur l'autre cœur.** `sdkconfig:1242` porte
  `CONFIG_ESP32_WIFI_TASK_PINNED_TO_CORE_0=y`, tandis que `loopTask` est créée à la priorité **1**
  sur le cœur **1** (`cores/esp32/main.cpp:71`, `CONFIG_ARDUINO_RUNNING_CORE=1`). L'appelant est la
  tâche **la moins prioritaire du système** : une fois le sémaphore rendu, il attend encore d'être
  réordonnancé.
- **Le blocage n'a pas de timeout** (`0xFFFFFFFF` = `OSI_FUNCS_TIME_BLOCKING`). La durée dépend
  entièrement de l'état de la file de la tâche WiFi — c'est-à-dire, chez nous, de ce que fait la
  radio pendant qu'on émet 100 à 200 paquets par seconde.
- **Il alloue.** `_wifi_zalloc(24)` / `_free` à chaque appel. À 100 Hz, cent allocations-libérations
  par seconde dans le tas WiFi, depuis la boucle qui doit aussi vider la file du BNO en moins de
  `DRAIN_BUDGET_MS` (50 ms, `main.cpp:27`).

À 0,5 Hz (le heartbeat actuel) tout cela est invisible et personne n'aurait dû s'en soucier. À
100 Hz depuis `loop()`, **`WiFi.RSSI()` serait exactement le genre de chose que #49 cherche.**

> **Conséquence pratique, indépendamment de la suite** : même pour le heartbeat, il n'y a aucune
> raison de payer l'ioctl. `esp_wifi_sta_get_rssi(&r)` donne la même valeur sous un simple verrou —
> une substitution d'une ligne, plus un `#include <esp_wifi.h>` (que `WiFi.h` n'apporte pas de
> lui-même : `WiFiSTA.cpp` l'inclut dans son propre `.cpp`). Aucun changement de protocole, aucun
> changement de comportement observable. *(Constat, pas un correctif : ce ticket n'écrit rien.)*

---

## 4. Les alternatives, évaluées

### 4.1 RSSI par paquet en mode promiscuous — techniquement ouvert, pratiquement déconseillé

La doc v4.4.7 ([Wi-Fi Driver → Wi-Fi Sniffer Mode](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-guides/wifi.html#wi-fi-sniffer-mode))
est explicite sur les trois points qui comptent :

> « The Wi-Fi sniffer mode can be enabled in the Wi-Fi mode of `WIFI_MODE_NULL`, or `WIFI_MODE_STA`,
> or `WIFI_MODE_AP`, or `WIFI_MODE_APSTA`. **In other words, the sniffer mode is active when the
> station is connected to the AP** […] »

> « Please note that **the sniffer has a great impact on the throughput** of the station or AP Wi-Fi
> connection. Generally, **we should NOT enable the sniffer, when the station/AP Wi-Fi connection
> experiences heavy traffic** unless we have special reasons. »

> « The callback will be **called directly in the Wi-Fi driver task**, so if the application has a
> lot of work to do for each filtered packet, the recommendation is to post an event to the
> application task […] »

Chaque trame remontée porte un `wifi_pkt_rx_ctrl_t`
([`esp_wifi_types.h`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_wifi_types.h#L386-L432))
avec `rssi` (dBm), `noise_floor` (présent sur S3), `rate`, `mcs`, `sig_len`, `channel`, `timestamp`,
`rx_state`. Les trames de **contrôle** sont filtrables par sous-type, et
`WIFI_PROMIS_CTRL_FILTER_MASK_ACK` existe (`esp_wifi_types.h:472`) — donc *l'API prévoit* de
remonter les ACK avec leur RSSI.

**Bilan :**

| | |
|---|---|
| Cadence atteignable | égale au trafic *reçu sur le canal* — potentiellement des centaines de trames/s, tout le trafic de la cellule inclus, pas seulement le nôtre. |
| Coût | un callback dans la tâche WiFi à chaque trame, plus l'avertissement d'Espressif sur le débit. Sur un lien qui porte 100–200 paquets/s en émission, c'est précisément le cas qu'il dit d'éviter. |
| Ce que ça empêche | **le modem-sleep** (radio éteinte = rien à renifler), et la sérénité du chemin critique du spectacle. C'est un changement permanent du comportement radio, pas un champ en plus. |
| Non vérifiable | **qu'un ACK qui nous est adressé remonte réellement au callback.** L'ACK est acquitté par le matériel en SIFS ; que `libpp` le repasse au chemin promiscuous n'est écrit nulle part. Voir § 6, mesure B. |

**Verdict** : ce n'est pas « une extension du protocole permanent », c'est un mode de fonctionnement
radio différent, dont le fabricant déconseille explicitement l'usage sous notre charge. Hors
périmètre de la carte, qui n'admet qu'un seul comportement de spectacle.

### 4.2 CSI (`esp_wifi_set_csi_rx_cb`) — même mur

`wifi_csi_info_t` commence par un `wifi_pkt_rx_ctrl_t` complet
([`esp_wifi_types.h:502-509`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_wifi_types.h#L502-L509)),
donc un RSSI **par paquet reçu**, sans mode promiscuous. Mais le mur est le même et il est
structurel : **nous ne recevons presque rien.** Hors balises et ACK, un STA qui émet 100–200
paquets/s en UDP unidirectionnel ne reçoit aucune trame de données. Une API qui se déclenche à la
réception ne peut pas battre plus vite que la réception. Sans compter que le CSI est une mesure par
sous-porteuse, bien plus lourde que ce que la question demande.

### 4.3 Les compteurs de pile — impasse

- **`esp_wifi_statis_dump(modules)`** : le désassemblage montre qu'il n'appelle que
  `dbg_lmac_statis_dump()` et `dbg_hmac_statis_dump()` et **ne renvoie rien de structuré** — c'est
  un dump vers le log série. Inexploitable comme source de données pour un champ de protocole.
- **`esp_wifi_sta_get_negotiated_phymode()`** : renvoie le mode PHY (11b / 11g / HT20 / HT40), pas
  le MCS ni le débit instantané. Trop grossier pour une modulation par tour, et il passe par
  `esp_wifi_ipc_internal` — donc un aller-retour inter-cœurs, comme `get_ap_info`.
- **`ic_get_rssi()`** (`libpp/if_hwctrl.o`, symbole global mais sans header) : lit
  `trc[3] - wDevCtrl[46]`, où `trc[3]` est écrit par `rcUpdateRxDone` — **par trame reçue**. Même
  mur que 4.2, et en prime c'est un offset dans une structure non documentée d'un blob fermé, qui
  peut changer à chaque version d'IDF. **À ne pas construire dessus.**

### 4.4 Le taux d'échec en émission — la seule grandeur radio à notre cadence

`esp_wifi_set_tx_done_cb()` existe et est linké (`libnet80211.a:ieee80211_api.o`), déclaré dans
[`esp_wifi/include/esp_private/wifi.h:543-562`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_private/wifi.h#L543-L562) :

```c
/** @param txStatus True:if the data was transmitted sucessfully False: if data transmission failed */
typedef void (* wifi_tx_done_cb_t)(uint8_t ifidx, uint8_t *data, uint16_t *data_len, bool txStatus);
esp_err_t esp_wifi_set_tx_done_cb(wifi_tx_done_cb_t cb);
```

C'est **la seule grandeur radio qui bat à notre propre cadence d'émission**, puisque c'est nous qui
émettons. L'enregistrement est gratuit (le désassemblage montre un simple
`ic_register_pp_tx_done_cb`, aucun ioctl, aucun verrou).

| | |
|---|---|
| Cadence | notre débit d'émission, 100–200 Hz. Exactement l'échelle de la roue. |
| Coût | le callback tourne dans la tâche WiFi : il ne doit rien faire d'autre qu'incrémenter un compteur `volatile`. Un octet par paquet dans l'en-tête suffirait à le transporter. |
| Ce que ça empêche | rien du comportement radio — c'est de l'observation pure. |
| Le prix réel | **c'est une API privée** (`esp_private/`). Elle n'est pas dans `esp_wifi.h`, elle n'est pas dans la doc en ligne, et rien ne garantit sa présence ni sa signature à la version d'IDF suivante. Un champ de protocole permanent adossé à une API privée est une dette qu'il faut nommer. |
| Ce que ça ne dit **pas** | le **nombre** de retransmissions. `txStatus` est un booléen « acquitté ou abandonné ». Une trame acquittée à la septième tentative est un succès. Or c'est précisément le retard, pas l'échec, que la reconnaissance de #49 a observé — perte nulle, retard groupé. Ce booléen ne verrait donc **rien** du phénomène déjà mesuré. |

### 4.4 bis — récapitulatif

| Alternative | Cadence | Coût | Empêche | Verdict |
|---|---|---|---|---|
| `esp_wifi_sta_get_rssi` (au lieu de `WiFi.RSSI`) | inchangée (~3–10 Hz) | verrou seul | rien | **à faire** — pour le heartbeat existant |
| promiscuous + filtre ACK | 100s/s (théorique) | callback tâche WiFi + débit dégradé | modem-sleep, sérénité du chemin critique | **non** — mode radio, pas extension |
| CSI | = trafic **reçu** ≈ 0 | lourd | — | **non** — mur structurel |
| `esp_wifi_statis_dump` | — | — | — | **non** — log, pas donnée |
| `ic_get_rssi`, `rc_get_trc` | = trafic reçu | offsets non documentés | — | **non** — blob non contractuel |
| `esp_wifi_set_tx_done_cb` | **100–200 Hz** | callback trivial | rien | **candidat**, mais API privée, et aveugle au retard |
| retard par paquet (`ts_esp_us`) replié sur `spin_deg` | **100–200 Hz** | **zéro firmware** | rien | **la réponse** (§ 7) |

---

## 5. Ce qui ne marche pas et ne doit pas être tenté

- **Appeler `WiFi.RSSI()` à 100 Hz dans `loop()`.** Cent aller-retours bloquants inter-cœurs et cent
  allocations par seconde, dans la boucle qui doit vider le BNO en moins de 50 ms — pour dix
  répétitions de la même valeur. C'est fabriquer le défaut que la carte voisine cherche.
- **Moyenner ou filtrer le RSSI côté hôte pour « récupérer » de la résolution.** Il n'y a rien à
  récupérer : entre deux balises, le champ est *littéralement inchangé*. Lisser dix copies d'un
  même octet produit une courbe lisse et fausse — pire que pas de courbe, parce qu'elle a l'air
  d'une mesure.
- **Prendre `esp_wifi_sta_get_rssi` pour une valeur « instantanée » au sens temporel.** Elle est
  instantanée au sens *non moyennée* (§ 1.4), ce qui est une bonne nouvelle, et périmée de 0 à
  300 ms au sens temporel, ce qui est la mauvaise. Les deux sont vraies en même temps.
- **Lire `trc[0]` / `trc[3]` via `rc_get_trc()` pour attraper le SNR des ACK.** `rcUpdateAckSnr()`
  existe bel et bien dans `libpp/trc.o` et est appelé depuis `rcUpdateTxDone` — le matériel *fait*
  remonter quelque chose sur les ACK. Mais aucun header ne décrit cette structure, aucune API ne
  l'expose, et un offset d'octet dans un blob fermé n'est pas un contrat. Le spectacle ne se pose
  pas là-dessus.
- **Attendre qu'une montée de version règle le problème.** arduino-esp32 3.x appelle toujours
  `esp_wifi_sta_get_ap_info` ; le champ est toujours écrit par le seul chemin balise dans les IDF
  récentes (c'est le sujet de [#17664](https://github.com/espressif/esp-idf/issues/17664), sur 5.5).
- **Croire les forums sur ce point.** Le ticket le disait ; le § 1.4 le confirme depuis l'autre
  côté : les deux fils GitHub qui posent exactement la question n'ont **aucune** réponse de
  mainteneur. Ce qui fait autorité ici, c'est la ligne `@param` du header et le binaire.

---

## 6. Ce qui reste non vérifiable, et la mesure qui trancherait

Trois points où l'honnêteté impose de distinguer l'établi du plausible.

**(a) L'exhaustivité de la recherche d'écrivains (§ 1.3).** Elle porte sur le motif littéral
`s8i …, 166`. Une écriture passant par une base pré-calculée (`addi a3, a2, 160` puis
`s8i a10, a3, 6`) y échapperait. Rien dans le code lu ne suggère ce style, et la conclusion
concorde avec la ligne `@param` du fabricant — mais c'est une recherche par motif, pas une preuve.

**(b) Le sort des ACK dans le chemin promiscuous (§ 4.1).** `WIFI_PROMIS_CTRL_FILTER_MASK_ACK`
existe dans l'API. Que le matériel repasse au logiciel un ACK *qui nous est adressé* — acquitté en
SIFS, dans une fenêtre où la radio vient de basculer de TX à RX — n'est écrit nulle part, et
`libpp` est fermé. C'est le point sur lequel les forums se contredisent, et il ne se règle que par
la mesure.

**(c) Le DTIM réel de la box.** Le § 2.1 raisonne sur 1 à 3. La valeur est annoncée dans la balise
et se lit depuis un ordinateur du réseau ; sur macOS, `wdutil info` ou une capture Wireshark en mode
moniteur suffisent. Ça change le chiffre, pas la conclusion.

### Mesure A — la cadence réelle, en dix lignes

À poser temporairement dans `loop()` du firmware, laisser tourner 30 s, lire le port série, retirer.
Elle ne mesure pas le débit de lecture (on sait qu'on peut lire vite) : elle compte **les valeurs
qui changent**.

```cpp
#include <esp_wifi.h>   // esp_wifi_sta_get_rssi : le même octet, sans l'ioctl

static int      last = 127;   // hors plage : le premier tour compte toujours
static uint32_t reads = 0, changes = 0, t0 = 0;

int r;
if (esp_wifi_sta_get_rssi(&r) == ESP_OK) {
  reads++;
  if (r != last) { changes++; last = r; }
}
if (millis() - t0 >= 1000) {
  Serial.printf("%lu lectures/s, %lu changements/s, dernier %d dBm\n", reads, changes, r);
  t0 = millis(); reads = 0; changes = 0;
}
```

**Lecture attendue** : `changes/s` proche de **3,3** (modem-sleep, DTIM 3) ou **9,8**
(`WiFi.setSleep(false)` en amont, ou DTIM 1), *indépendamment* de `lectures/s`, qui sera de l'ordre
du millier. Si `changes/s` dépassait 20, tout le § 1.3 serait faux et il faudrait rouvrir.
Faire la passe **deux fois** — avec et sans `WiFi.setSleep(false)` — sépare le plafond (§ 2.1) du
plancher (§ 2.2) dans le même geste.

### Mesure B — un ACK remonte-t-il au sniffer ?

Même forme, à ne tenter que si quelqu'un veut vraiment le RSSI par paquet. Compter, ne rien faire
d'autre : le callback tourne dans la tâche WiFi.

```cpp
static volatile uint32_t n_ack = 0;
void cb(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type == WIFI_PKT_CTRL) n_ack++;      // rssi dans ((wifi_promiscuous_pkt_t*)buf)->rx_ctrl.rssi
}
// dans setup(), APRÈS la connexion :
WiFi.setSleep(false);                       // sinon la radio dort et ne renifle rien
wifi_promiscuous_filter_t f  = { .filter_mask = WIFI_PROMIS_FILTER_MASK_CTRL };
wifi_promiscuous_filter_t cf = { .filter_mask = WIFI_PROMIS_CTRL_FILTER_MASK_ACK };
esp_wifi_set_promiscuous_filter(&f);
esp_wifi_set_promiscuous_ctrl_filter(&cf);
esp_wifi_set_promiscuous_rx_cb(cb);
esp_wifi_set_promiscuous(true);
```

**Lecture attendue** : si `n_ack/s` s'approche du nombre de paquets émis par seconde, les ACK
remontent et le RSSI par paquet est physiquement atteignable (au prix du § 4.1). S'il reste à zéro
ou ne compte que le trafic des voisins, la question est close. Surveiller en même temps `seq` côté
hôte : l'avertissement d'Espressif sur le débit se vérifiera là.

### Mesure C — le coût, chiffré

Si quelqu'un doute du § 3, mille appels de chaque API encadrés par `esp_timer_get_time()`, une fois
au repos et une fois pendant que les 100 Hz tournent. C'est le second cas qui est intéressant : la
file de la tâche WiFi est alors pleine, et c'est là que le blocage sans timeout se paie.

---

## 7. Ce que ça décide pour #49

**Le RSSI haute cadence n'entre pas dans le protocole permanent.** Non pas parce que le champ
coûterait cher — un `int8` par paquet, c'est 100 o/s, l'argument du ticket tient — mais parce qu'il
ne porterait aucune information : entre deux balises, l'octet est le même, et les balises arrivent
trois à dix fois par seconde contre quatre tours de roue par seconde. Un champ qui répète dix fois
la même valeur est pire qu'absent : il *ressemble* à une mesure.

**Ce que ce ticket rapporte quand même de concret :**

1. **Le heartbeat garde son RSSI, tel quel, et il est au bon endroit.** À 2 s, ce champ n'a jamais
   prétendu suivre la rotation : c'est un indicateur de *lien*, et pour ça une balise toutes les
   300 ms moyennée par l'œil suffit. Rien à changer dans `transport/protocol.py` (`<IIIiff`,
   `rssi_dbm`) ni dans le firmware, **sauf** l'appel lui-même : `esp_wifi_sta_get_rssi()` au lieu de
   `WiFi.RSSI()`, même valeur, sans l'aller-retour inter-cœurs (§ 3.2). Une ligne, aucun effet
   observable, et ça retire un appel bloquant de la boucle qui pace le BNO.
2. **Le modem-sleep est un ticket à lui seul.** `WIFI_PS_MIN_MODEM` est actif par défaut sur
   l'ESP32-S3 et personne ne l'a décidé ; la radio s'éteint périodiquement en plein spectacle. La
   mesure A le révèle gratuitement au passage (deux passes, avec et sans). C'est un candidat de
   premier rang pour les épisodes ~3,25 s, et **c'est une ligne à essayer** — mais c'est un
   changement de comportement, donc son propre ticket, avec un avant/après.

**Par quoi la condition « rotation » se caractérise à la place**, par ordre de coût croissant :

| Option | Ce qu'elle mesure | Cadence | Coût | Ce qu'elle empêche |
|---|---|---|---|---|
| **A. Retard par paquet replié sur `spin_deg`** | l'effet de l'orientation sur le lien, tel qu'il arrive vraiment à l'hôte | 100–200 Hz | **zéro** : `ts_esp_us` et `seq` sont déjà dans chaque paquet, `spin_deg` est déjà calculé par le modèle, et les quatre takes de `2026-08-05_21-15_test-1` sont déjà sur le disque | rien |
| **B. Échec d'émission (`esp_wifi_set_tx_done_cb`)** | les trames abandonnées après retransmissions | 100–200 Hz | un octet par paquet + un callback trivial | rien du comportement radio ; mais **API privée**, et **aveugle au retard** — or la reconnaissance n'a vu que du retard |
| **C. RSSI par paquet (promiscuous)** | le champ lui-même | ~centaines/s | débit dégradé, modem-sleep interdit, callback dans la tâche WiFi | le comportement de spectacle — c'est un mode, pas une extension |

**A est la réponse**, et elle n'est pas un pis-aller. La carte #49 a déjà établi que le phénomène
est un *retard groupé sans perte* — exactement la signature d'une retransmission de niveau liaison.
Or c'est précisément ce que A mesure, et c'est ce que B ne verrait pas (une trame retransmise six
fois puis acquittée est un `txStatus == true`) et ce que C ne verrait qu'indirectement. Le RSSI
aurait été la *cause* ; le retard est l'*effet*, et c'est l'effet qui est déjà dans les fichiers.

Si A montre que les épisodes se groupent à une phase de roue, l'hypothèse « masquage par le corps
ou par le tube » est établie sans une ligne de firmware. Si elle montre qu'ils ne se groupent pas,
c'est le modem-sleep, l'AP ou l'air ambiant — et B et C n'auraient rien ajouté.

---

## Sources

**Code effectivement installé sur cette machine** (autorité pour « ce qui tourne sur la roue ») :
`~/.platformio/packages/framework-arduinoespressif32/` — `package.json` (3.20017.241212+sha.dcc1105b) ·
`cores/esp32/esp_arduino_version.h:22-26` · `cores/esp32/main.cpp:71` ·
`libraries/WiFi/src/WiFiSTA.cpp:726-736` · `libraries/WiFi/src/WiFiGeneric.cpp:766-770, 1046` ·
`tools/sdk/esp32s3/include/esp_common/include/esp_idf_version.h:22-26` ·
`tools/sdk/esp32s3/include/esp_wifi/include/esp_wifi.h` ·
`tools/sdk/esp32s3/include/esp_wifi/include/esp_wifi_types.h` ·
`tools/sdk/esp32s3/include/esp_wifi/include/esp_private/wifi.h:543-562` ·
`tools/sdk/esp32s3/include/esp_wifi/include/esp_private/wifi_os_adapter.h` ·
`tools/sdk/esp32s3/sdkconfig:7, 233-238, 1242`.

**Binaire livré, désassemblé** (`xtensa-esp32s3-elf-objdump` / `-nm`, toolchain PlatformIO) :
`tools/sdk/esp32s3/lib/libnet80211.a` — `ieee80211_api.o` (`esp_wifi_sta_get_rssi`,
`esp_wifi_sta_get_ap_info`, `wifi_api_lock`, `esp_wifi_statis_dump`, `esp_wifi_set_tx_done_cb`,
`esp_wifi_sta_get_negotiated_phymode`), `ieee80211_ioctl.o` (`ieee80211_ioctl`,
`wifi_get_ap_info_process`), `wl_cnx.o` (`cnx_rc_update_rssi`), `ieee80211_scan.o`
(`scan_parse_beacon`), `ieee80211_sta.o` (`sta_recv_mgmt`, `rssi_saved`) ·
`tools/sdk/esp32s3/lib/libpp.a` — `trc.o` (`rcUpdateAckSnr`, `rcUpdateRxDone`, `rc_get_trc`),
`if_hwctrl.o` (`ic_get_rssi`).

**Amont, aux versions consultées :**
[arduino-esp32 2.0.17 — `WiFiSTA.cpp`](https://github.com/espressif/arduino-esp32/blob/2.0.17/libraries/WiFi/src/WiFiSTA.cpp#L726-L736) ·
[arduino-esp32 2.0.17 — `WiFiGeneric.cpp`](https://github.com/espressif/arduino-esp32/blob/2.0.17/libraries/WiFi/src/WiFiGeneric.cpp#L766-L770) ·
[arduino-esp32 master — `STA.cpp`](https://github.com/espressif/arduino-esp32/blob/master/libraries/WiFi/src/STA.cpp) ·
[esp-idf v4.4.7 — `esp_wifi.h`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_wifi.h#L1388-L1400) ·
[esp-idf v4.4.7 — `esp_wifi_types.h`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_wifi_types.h#L190-L210) ·
[esp-idf v4.4.7 — `esp_private/wifi.h`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_private/wifi.h#L543-L562) ·
[esp-idf v4.4.7 — `esp_private/wifi_os_adapter.h`](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_private/wifi_os_adapter.h)

**Documentation Espressif, version v4.4.7 / esp32s3 :**
[Wi-Fi Driver (Sniffer Mode, Power-saving Mode)](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-guides/wifi.html) ·
[Wi-Fi API Reference](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-reference/network/esp_wifi.html) ·
[esp-faq — Wi-Fi (moyenne 13/3)](https://github.com/espressif/esp-faq/blob/master/docs/en/software-framework/wifi.rst)

**Issues GitHub — aucune réponse de mainteneur Espressif, citées comme telles :**
[esp-idf#12685](https://github.com/espressif/esp-idf/issues/12685) (`filzek`, 2023-11-29, `NONE`) ·
[esp-idf#17664](https://github.com/espressif/esp-idf/issues/17664) (`H1steria` 2025-09-29 ;
commentaire `balla94` 2026-02-20, `NONE`)

**Firmware** (`~/Desktop/IMU_project/PIO projects/260524_BNO_super_reports`, lu, non modifié) :
`platformio.ini` · `src/report_manager.cpp:217-226` · `src/main.cpp:27, 93-124` ·
`src/protocol.h:42`.

**Dépôt courant** (lu, non modifié) : `transport/protocol.py:27-31, 65-80`.
