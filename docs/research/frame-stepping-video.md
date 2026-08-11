# Pas-à-pas frame par frame dans un `<video>` : ce que le navigateur permet vraiment

Recherche pour le ticket [#4](https://github.com/Zaruitoga/conductor/issues/4), sous la carte
[#2](https://github.com/Zaruitoga/conductor/issues/2) « vidéo synchronisée à la visualisation 3D ».

**Enjeu.** Toute la méthode d'alignement de la carte repose sur le fait de pouvoir désigner
*exactement* la frame du début de mouvement. À 2 tours/s, une frame de 30 fps vaut 33 ms, soit ~24°
de roue : se tromper d'une frame est visible. La question n'est donc pas « le navigateur sait-il
lire une vidéo », c'est « sait-il **nommer** la frame qu'il affiche ».

---

## Verdict

**Oui, c'est faisable, en JavaScript natif pur — sans build, sans bundler, sans CDN, sans npm.**

Mais pas pour la raison qu'on attendrait. Le point décisif n'est pas que le seek soit exact : c'est
que `requestVideoFrameCallback()` **rapporte le PTS réel de la frame affichée**. On ne demande donc
jamais au navigateur d'atterrir au bon endroit du premier coup — on lui demande de **dire** où il a
atterri, et on corrige. Le problème passe d'« exactitude ouverte » à « boucle fermée avec accusé de
réception », ce qui est un problème résoluble.

Ce qu'il faut accepter en contrepartie :

| | |
|---|---|
| **Exactitude en pause** | Adossée à une **garantie d'implémenteur Chromium**, pas à du texte normatif. Aucun énoncé public équivalent côté WebKit ou Gecko. → Chrome comme navigateur de référence pour la page d'alignement. |
| **Firefox** | Deux bugs ouverts (non confirmés) touchent exactement notre geste : seeks coalescés depuis rVFC en H.264, et rVFC plafonnée à ~25 Hz. À traiter comme non supporté jusqu'à vérification. |
| **Latence** | Un pas **arrière** oblige à re-décoder depuis le keyframe précédent : sur un GOP de 2 s (courant en H.264 caméra), un pas arrière coûte un GOP entier. L'UI doit être une machine à états asynchrone, jamais un `for` synchrone. |
| **Cadence** | Aucune API ne la donne. Ce n'est pas bloquant : la méthode fermée n'en a pas besoin pour *désigner* une frame, seulement pour *viser* la suivante — et il existe une parade sans cadence. |

**Ce qui ne marche pas et ne doit pas être tenté** : `presentedFrames` comme numéro de frame ;
`currentTime` relu comme identité de frame ; `currentTime += 1/fps` ; extraire toutes les frames via
rVFC pendant la lecture.

---

## 1. `requestVideoFrameCallback()`

### 1.1 Support

| Navigateur | Version | Source |
|---|---|---|
| Chrome / Edge | **83** (mai 2020) | [chromestatus 6335927192387584](https://chromestatus.com/feature/6335927192387584), [BCD `api/HTMLVideoElement.json`](https://github.com/mdn/browser-compat-data/blob/main/api/HTMLVideoElement.json) |
| Safari / iOS | **15.4** (mars 2022) | [WebKit blog, « New WebKit Features in Safari 15.4 »](https://webkit.org/blog/12445/new-webkit-features-in-safari-15-4/) |
| Firefox | **132** (oct. 2024) | [notes de version Firefox 132](https://www.firefox.com/en-US/firefox/132.0/releasenotes/), [bug 1800882](https://bugzil.la/1800882) |

MDN classe la fonctionnalité **Baseline « newly available » depuis octobre 2024**
([MDN](https://developer.mozilla.org/en-US/docs/Web/API/HTMLVideoElement/requestVideoFrameCallback)).

Deux réserves sur le statut :

- La spec est un **WICG Draft Community Group Report** (2 août 2024) — elle le dit elle-même :
  « It is not a W3C Standard nor is it on the W3C Standards Track »
  ([spec, Status of this document](https://wicg.github.io/video-rvfc/)). Pas d'intégration dans le
  HTML Standard, pas de suite de tests normative.
- Firefox **n'implémente pas `processingDuration`** ([bug 1908246](https://bugzil.la/1908246),
  statut NEW). Sans conséquence pour nous : on n'en a pas besoin.

### 1.2 Ce que `mediaTime` rapporte vraiment

Définition normative ([spec § 2.2](https://wicg.github.io/video-rvfc/)) :

> `mediaTime`, of type double — The media presentation timestamp (PTS) in seconds of the frame
> presented (e.g. its timestamp on the `video.currentTime` timeline). MAY have a zero value for
> live-streams or WebRTC applications.

Confirmé côté implémentation par Dale Curtis (propriétaire du composant media de Chromium) :
« generally the media time should be exactly what the demuxer emits », avec la réserve « There are
some rare cases where we do rewrite timestamps »
([WICG/video-rvfc#59](https://github.com/WICG/video-rvfc/issues/59)).

**C'est la seule valeur de la plateforme qui nomme la frame affichée.** Voir § 3.3 pour la
démonstration que `currentTime` ne le fait pas.

Réserve importante, et l'éditeur de la spec s'est explicitement rétracté sur ce point : la
correspondance métadonnées ↔ pixels n'est **pas** garantie à 100 % pendant la lecture. Thomas
Guilbert (éditeur, Google) :

> Actually, what I said about the guarantee of the metadata and the frame matching was wrong […]
> The latest frame can internally be updated on the compositor thread after we read its metadata, so
> there might be a mismatch.
> — [WICG/video-rvfc#66](https://github.com/WICG/video-rvfc/issues/66)

Cette réserve vaut **pendant la lecture**. En pause, voir § 1.4.

### 1.3 Ce que `presentedFrames` ne rapporte pas

Définition normative ([spec § 2.2](https://wicg.github.io/video-rvfc/)) :

> `presentedFrames` — A count of the number of frames submitted for composition. Allows clients to
> determine if frames were missed between VideoFrameRequestCallbacks. MUST be monotonically
> increasing.

**Ce n'est pas un index de frame dans le fichier**, et ce n'est pas un oubli de la spec — c'est
délibéré. Thomas Guilbert :

> The fact that `presentedFrames` does not update after seeks is by design. `presentedFrames`
> corresponds to the number of frames sent to the compositor, regardless of where those frames came
> from in the video/stream.
> — [WICG/video-rvfc#82](https://github.com/WICG/video-rvfc/issues/82)

Conséquences :

- Un seek en arrière **n'incrémente pas moins** le compteur ; il l'incrémente comme tout le reste.
- Le compteur n'a pas d'origine liée au fichier : il compte des présentations depuis l'attachement.
- Son seul usage légitime : détecter des **trous pendant une lecture continue** (delta > 1).
- Et même là, il est incomplet : une vidéo à 120 fps sur un écran 60 Hz voit la moitié de ses frames
  décodées mais jamais présentées — **sans créer de discontinuité dans `presentedFrames`** (mêmes
  fils WICG, [#66](https://github.com/WICG/video-rvfc/issues/66)). Le compteur compte les
  présentations, pas les frames du média.

**Pour nous : inutilisable.** L'identité d'une frame, c'est `mediaTime`, point.

### 1.4 Le seul régime où la garantie est forte : en pause

C'est notre cas d'usage exact, et c'est une chance. Dale Curtis, Chromium :

> When paused you're always going to get the right frame callback after a seek. **The best effort
> only applies to during playback.** Due to the threading involved, it's not possible to provide a
> guarantee that you are getting the information for the frame on screen [pendant la lecture].
> — [WICG/video-rvfc#69](https://github.com/WICG/video-rvfc/issues/69)

Et, à la question de savoir si le risque de désynchronisation métadonnées/pixels de §1.2 s'applique
aussi en pause :

> Correct, pause is unaffected since only 1 frame is ever rendered in the pause case (until `play()`
> is called anyways).
> — même fil

**Statut de cette affirmation : déclaration d'implémenteur Chromium, pas texte normatif.** La spec ne
distingue pas le régime pause. Il n'existe aucun énoncé public équivalent côté WebKit ni Gecko. C'est
la raison principale du choix « Chrome comme navigateur de référence » dans le verdict.

### 1.5 Pièges opérationnels documentés

Tous issus des fils WICG, tous applicables à une boucle de pas-à-pas :

1. **Enregistrer la callback AVANT de seeker.** « You should […] make sure to call
   `requestVideoFrameCallback()` before seeking, because if the seek completes before the callbacks
   are attached, the callbacks will never fire »
   ([#53](https://github.com/WICG/video-rvfc/issues/53)).
2. **Ne jamais enchaîner deux seeks sans attendre.** « If you have multiple seeks that complete
   between two rendering steps, only the last seek will produce an rVFC callback. The seeked frames
   in between might never be presented » ([#66](https://github.com/WICG/video-rvfc/issues/66)).
3. **Attendre `seeked` ET la rVFC.** Les deux ne sont pas ordonnés : `seeked` est une *task*, rVFC
   tourne dans les *rendering steps*. « if you are playing and seek to a frame, the rVFC for that
   specific frame might be before or after the `onseeked` » (même fil).
4. **La callback peut arriver 1 v-sync en retard** — spec, note § 4.2 : « There are no strict timing
   guarantees when it comes to how soon callbacks are run after a new video frame has been
   presented ». On peut le détecter en comparant `metadata.expectedDisplayTime` à `now` : un écart de
   quelques ms = à l'heure, un écart quasi nul = en retard d'un v-sync
   ([#59](https://github.com/WICG/video-rvfc/issues/59)). Sans importance en pause : la frame reste à
   l'écran.
5. **Il n'existe aucun moyen fiable d'extraire toutes les frames par rVFC en lecture.** « There is no
   guaranteed way to extract all frames from a video using requestVideoFrameCallback » — l'éditeur,
   [#66](https://github.com/WICG/video-rvfc/issues/66). Ce qu'il recommande à la place, c'est
   WebCodecs (voir § 6).

---

## 2. Obtenir la cadence du fichier

Aucune API HTML5 ne l'expose. Pire : la seule API de pas-à-pas natif qui ait jamais existé a été
**retirée**.

### 2.1 Ce qui n'existe pas / plus

- **`HTMLMediaElement.seekToNextFrame()`** — non standard, Firefox uniquement, ajouté en 56,
  **retiré en Firefox 128**
  ([BCD `api/HTMLMediaElement.json`](https://github.com/mdn/browser-compat-data/blob/main/api/HTMLMediaElement.json),
  `version_removed: "128"`). Jamais implémenté par Chrome ni Safari. Il n'existe donc plus **aucune**
  primitive « frame suivante » dans la plateforme.
- **`getVideoPlaybackQuality().totalVideoFrames`** — « the total number of frames that would have
  been displayed if no frames are dropped »
  ([W3C Media Playback Quality](https://w3c.github.io/media-playback-quality/)). C'est un compteur
  cumulatif *de lecture*, pas une propriété du fichier : il faudrait lire la vidéo en entier pour
  l'obtenir, et le rapport `totalVideoFrames / duration` est faux dès qu'on est en cadence variable.
  Sans intérêt ici.

### 2.2 Les trois voies réelles

**a. Déduire des deltas de `mediaTime` — 100 % natif, zéro dépendance.**
En cadence constante, quelques dizaines de frames présentées suffisent. Précaution : des callbacks
peuvent manquer (§ 1.3), donc les deltas contiennent des multiples entiers de la vraie période.
Prendre le **plus petit delta observé** ou le **mode**, jamais la moyenne. Faux par construction en
VFR (§ 5).

**b. Côté serveur, `ffprobe`.**
`-show_streams` donne `r_frame_rate` et `avg_frame_rate`. Attention à ce que `r_frame_rate` est
vraiment — la doc FFmpeg est explicite
([`libavformat/avformat.h`](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/avformat.h)) :

> Real base framerate of the stream. This is the lowest framerate with which all timestamps can be
> represented accurately (it is the least common multiple of all framerates in the stream).
> **Note, this value is just a guess!**

Le vrai remède n'est pas la cadence mais `-show_frames`, qui donne la **liste exacte des PTS** — ce
qui rend la question de la cadence sans objet, y compris en VFR.

*Contrainte dépôt* : `ffprobe` **n'est pas installé sur cette machine** (`which ffprobe` → not
found). Ce serait une nouvelle dépendance **binaire externe**, d'une autre nature que les
`pip install` ad hoc actuels. Ce n'est pas rédhibitoire (c'est du backend, ça ne viole pas la règle
« pas de build/bundler/CDN » qui concerne le frontend), mais c'est une décision à prendre
explicitement, pas un détail d'implémentation.

**c. Saisie manuelle.** Dernier recours, et faux en VFR.

### 2.3 Le point qui change la donne

**Pour *désigner* une frame, on n'a pas besoin de la cadence.** `mediaTime` donne l'identité de la
frame affichée sans qu'on sache combien il y en a par seconde. La cadence ne sert qu'à *viser* la
frame suivante avant de l'avoir vue — et le § 4 montre comment s'en passer.

---

## 3. Exactitude d'un seek par `currentTime`

### 3.1 Normativement : exact, et le contraire est réservé à `fastSeek()`

Le HTML Standard réserve la snap au keyframe à un drapeau, `approximate-for-speed`
([HTML, § seeking](https://html.spec.whatwg.org/multipage/media.html#seeking)) :

> If the **approximate-for-speed** flag is set, adjust the new playback position to a value that will
> allow for playback to resume promptly. […] *For example, the user agent could snap to a nearby key
> frame, so that it doesn't have to spend time decoding then discarding intermediate frames before
> resuming playback.*

Et ce drapeau n'est posé que par `fastSeek()` :

> The `fastSeek(time)` method must **seek** to the time given by *time*, with the
> **approximate-for-speed** flag set.

Alors qu'écrire `currentTime` seeke **sans** le drapeau
([HTML, § offsets into the media resource](https://html.spec.whatwg.org/multipage/media.html#offsets-into-the-media-resource)) :

> On setting, […] it must set the **official playback position** to the new value and then **seek**
> to the new value.

La spec dit par ailleurs de façon nette ce qui doit s'afficher
([HTML, § the video element](https://html.spec.whatwg.org/multipage/media.html#the-video-element)) :

> When the `video` element is paused — The `video` element represents the frame of video corresponding
> to the **current playback position**. […] **Which frame in a video stream corresponds to a
> particular playback position is defined by the video stream's format.**

Donc : normativement, un `currentTime = t` sur un élément en pause doit afficher la frame que le
format associe à `t`. Pas le keyframe le plus proche.

### 3.2 Dans les implémentations : conforme

**Chromium.** Le démuxeur cherche le keyframe **antérieur** (`AVSEEK_FLAG_BACKWARD`), puis le
renderer décode en avant et **jette** ce qui précède la cible
([`media/filters/ffmpeg_demuxer.cc`](https://github.com/chromium/chromium/blob/main/media/filters/ffmpeg_demuxer.cc),
[`media/renderers/video_renderer_impl.cc`](https://github.com/chromium/chromium/blob/main/media/renderers/video_renderer_impl.cc)) :

```cpp
// ffmpeg_demuxer.cc
int AVSeekFrame(AVFormatContext* s, int stream_index, int64_t timestamp) {
  // Seek to a timestamp <= to the desired timestamp.
  int result = av_seek_frame(s, stream_index, timestamp, AVSEEK_FLAG_BACKWARD);
  ...
}

// video_renderer_impl.cc
bool VideoRendererImpl::HasBestFirstFrame(const VideoFrame& frame) {
  // We have the best first frame in the queue if our current frame has a
  // timestamp after `start_timestamp_` or straddles `start_timestamp_`.
  return frame.timestamp() >= start_timestamp_ ||
         frame.timestamp() + frame.metadata().frame_duration.value_or(
                                 last_decoder_stream_avg_duration_) >
             start_timestamp_;
}
```

La frame retenue est celle dont l'intervalle `[pts, pts + durée)` **contient** `t`. C'est exactement
la définition attendue. (Noter le `value_or(last_decoder_stream_avg_duration_)` : quand la durée par
frame n'est pas propagée, la décision retombe sur une **durée moyenne** — c'est la fissure VFR, voir
§ 5.3.)

**WebKit.** Le choix est explicite dans le code
([`Source/WebCore/html/HTMLMediaElement.cpp`](https://github.com/WebKit/WebKit/blob/main/Source/WebCore/html/HTMLMediaElement.cpp)) :

```cpp
void HTMLMediaElement::seek(const MediaTime& time) {
    seekWithTolerance({ time, MediaTime::zeroTime(), MediaTime::zeroTime() }, true);
}
// … et dans seekTask() :
SeekType thisSeekType = (negativeTolerance == MediaTime::zeroTime()
                      && positiveTolerance == MediaTime::zeroTime()) ? Precise : Fast;
```

`fastSeek()` passe au contraire une tolérance non nulle. Un `currentTime` est donc un seek **précis**
dans WebKit aussi.

**Quantification à la timescale.** Les deux moteurs font passer la valeur demandée par
`MediaTimeForTimeValue()` avant de chercher — la cible est arrondie à la base de temps du conteneur
(Blink : `html_media_element.cc`, WebKit : `seekTask()`, avec dans les deux cas le même commentaire
d'origine). Chromium travaille ensuite en `base::TimeDelta`, résolution µs. **Conséquence pratique :
une cible posée pile sur une frontière de frame est à la merci de cet arrondi.** C'est l'argument
central du § 4.2.

### 3.3 Le vrai piège : `currentTime` relu ne dit pas quelle frame est affichée

C'est le point le plus important de ce document, et il est **normatif**, pas anecdotique.

`currentTime` en lecture renvoie l'**official playback position**, qui est décrite comme « an
approximation of the current playback position that is kept stable while scripts are running »
([HTML](https://html.spec.whatwg.org/multipage/media.html#offsets-into-the-media-resource)). Et sur
écriture, la spec fait poser cette position **avant** que le seek n'ait lieu — la valeur relue est
donc littéralement **ce qu'on a demandé**, pas ce qui a été atteint. La spec le souligne elle-même
dans l'algorithme de seek :

> The `currentTime` attribute returns the **official playback position**, not the current playback
> position, and therefore gets updated before script execution, separate from this algorithm.

Chromium le confirme jusque dans le code
([`web_media_player_impl.cc`](https://github.com/chromium/chromium/blob/main/third_party/blink/renderer/platform/media/web_media_player_impl.cc)) :

```cpp
base::TimeDelta WebMediaPlayerImpl::GetCurrentTimeInternal() const {
  base::TimeDelta current_time;
  if (Seeking())      current_time = seek_time_;      // la cible demandée
  else if (paused_)   current_time = paused_time_;    // l'horloge du pipeline
  else                current_time = pipeline_controller_->GetMediaTime();
  ...
}
```

et côté Blink, `HTMLMediaElement::currentTime()` renvoie `last_seek_time_` tant que `seeking_` est
vrai ([`html_media_element.cc`](https://github.com/chromium/chromium/blob/main/third_party/blink/renderer/core/html/media/html_media_element.cc)).

**Donc `Math.floor(video.currentTime * fps)` est un calcul faux.** Il donne le numéro de frame
*demandé*, pas le numéro de frame *affiché*.

Un test tiers documenté dans [WICG/video-rvfc#69](https://github.com/WICG/video-rvfc/issues/69)
(1 000 itérations, vidéo 25 fps CFR horodatée à l'image) est éloquent : 996 accords entre
`Math.floor(currentTime * 25)` et `Math.round(mediaTime * 25)`, et **les 4 désaccords étaient des
erreurs de `currentTime`, pas de `mediaTime`** — le lecteur affichait bien la frame indiquée par
`mediaTime`. *Statut : observation tierce sur Chrome, pas une spec.* Elle est citée ici parce qu'elle
va dans le sens du raisonnement normatif ci-dessus, pas comme preuve.

### 3.4 Codec, conteneur, index, frames B

- **Frames B** : le vieux bug Chromium 66631 (« video frame displayed does not match currentTime when
  there's B frames »), imputé à un défaut de seek d'ffmpeg, cité dans
  [WICG/video-rvfc#64](https://github.com/WICG/video-rvfc/issues/64). *Son statut actuel n'a pas pu
  être vérifié : l'ancien `bugs.chromium.org` est fermé et `issues.chromium.org` demande une
  authentification.* Ce qui compte ici est la réponse de Dale Curtis en 2020 : « video.rVFC is
  unaffected by that bug. […] This API tells you what frame was sent to the compositor, so seeking
  can't impact it » (même fil).
  **C'est exactement la propriété qui sauve la méthode** : même si un seek atterrit une frame à côté,
  `mediaTime` le *dit*, et la boucle corrige.
- **Sans index** (par ex. un MP4 sans `moov` en tête, ou un flux non indexé) : le seek reste possible
  mais coûteux, et `seekable` peut être restreint — l'algorithme de seek recale alors la cible dans
  la plage `seekable` la plus proche (HTML, § seeking, étape 8). À vérifier sur les rushes réels ; un
  MP4 « faststart » est le cas sain.
- **Firefox, deux signaux ouverts** — à traiter comme des signaux, pas comme des faits établis, les
  deux étant **UNCONFIRMED** :
  - [bug 1941279](https://bugzil.la/1941279) — « Can't seek video on requestAnimationFrame (or
    requestVideoFrameCallback) » : sur du H.264, « It seems that Firefox is "collecting" all the seek
    requests, debounce them to finally display a frame » ; « It seems to work fine on av1 videos ».
    C'est **précisément** notre geste (seeker depuis la callback).
  - [bug 1935256](https://bugzil.la/1935256) — rVFC cadencée à ~40 ms (25 Hz) quel que soit l'OS,
    donc frames sautées au-delà de 25 fps. Concerne la lecture, pas la pause, mais entame la
    confiance.

---

## 4. Avancer / reculer d'exactement une frame

### 4.1 Le geste correct : une boucle fermée

Il n'existe aucune primitive native (§ 2.1). Le geste correct est :

```
1. pause, puis laisser l'état se stabiliser        (attendre `seeked` du seek précédent)
2. video.requestVideoFrameCallback(cb)             ← AVANT le seek, obligatoire (§1.5-1)
3. video.currentTime = cible                       ← viser le MILIEU d'une frame (§4.2)
4. attendre `seeked` ET la rVFC                    ← les deux, non ordonnés (§1.5-3)
5. lire metadata.mediaTime                         ← PTS RÉEL de la frame affichée
6. si ce n'est pas la frame voulue → corriger la cible, retour en 2
```

L'étape 5 est ce qui transforme un pari en mesure. En pratique, en CFR, l'étape 6 ne se déclenche
quasiment jamais ; elle existe pour les cas de § 3.4 et § 5.

**Conséquence de conception pour la carte** : ce qu'il faut stocker dans l'ancre vidéo
(`onset_video_s`), c'est le `mediaTime` de la frame retenue — le PTS lu dans le média — et **jamais**
un `n / fps` recalculé.
Le PTS est ce que le fichier dit ; `n / fps` est ce qu'on croit que le fichier dit.

### 4.2 Viser le milieu, jamais la frontière

Une cible posée exactement sur `n / fps` tombe sur la frontière entre la frame `n-1` et la frame `n`.
Deux arrondis s'appliquent ensuite (quantification à la timescale du conteneur, puis µs dans
Chromium — § 3.2), et le test de Chromium est `frame.timestamp() + durée > start_timestamp_`, une
inégalité **stricte** dont l'issue dépend du dernier bit. Viser :

```js
t = (n + 0.5) / fps                       // CFR, cadence connue
t = pts_courant + 1.5 * duree_frame       // avancer d'une, durée mesurée sur les deltas de mediaTime
```

La demi-frame de marge rend la cible insensible aux deux arrondis. C'est gratuit et ça supprime toute
une classe de bugs « une frame de décalage, parfois ».

### 4.3 Pourquoi `currentTime += 1/fps` échoue

Par ordre de gravité :

1. **La lecture de `currentTime` n'est pas le PTS affiché** (§ 3.3). L'incrément part donc d'une
   valeur qui a *déjà* pu diverger de la frame réellement à l'écran. C'est le défaut principal, et il
   se manifeste avant tout problème de flottant : on cumule un écart qu'on ne mesure jamais.
2. **Accumulation.** `1/fps` n'est pas représentable exactement — en NTSC, 30000/1001 donne
   `1/fps = 0,0333667…` — et chaque écriture est de plus requantifiée à la timescale du conteneur.
   Sur quelques centaines de pas, l'erreur cumulée franchit la demi-frame et on saute ou on double une
   frame, **silencieusement**. Le remède est de toujours recalculer une cible **absolue** depuis un
   index entier (`t = (n + 0.5) * duree_frame`), jamais un `+=`.
3. **Cadence variable** : il n'existe pas de « 1/fps » (§ 5).
4. **Frames B** : elles n'invalident pas la boucle fermée — `mediaTime` est en ordre de présentation
   — mais elles sont ce qui rend un seek nu potentiellement imprécis dans les vieux chemins ffmpeg
   (§ 3.4). Raison de plus de vérifier plutôt que de calculer.

### 4.4 Reculer coûte plus cher qu'avancer

Symétrique en exactitude, asymétrique en coût. Un pas arrière oblige le décodeur à repartir du
keyframe précédent et à redécoder tout le GOP jusqu'à la frame visée — c'est littéralement ce que
fait `AVSEEK_FLAG_BACKWARD` + le rejet des frames antérieures dans Chromium (§ 3.2). Sur un H.264 de
caméra avec un keyframe toutes les 2 s à 30 fps, un pas arrière = jusqu'à 60 frames décodées puis
jetées.

Ce n'est pas un problème d'exactitude, c'est un problème d'**interface** : le pas-à-pas doit être une
machine à états asynchrone qui affiche un état « en cours », pas une boucle qui suppose que le pas
précédent est terminé. Si le pas arrière rapide s'avère nécessaire (maintien de touche), la parade
usuelle est de transcoder une copie de travail en all-intra — mais c'est déjà le sujet « transcodage »
listé dans le brouillard de la carte #2, pas celui de ce ticket.

---

## 5. Cadence variable (VFR)

### 5.1 Ce n'est pas une anomalie, c'est le format

Un MP4/MOV stocke les durées **par échantillon** (atome `stts`, « time-to-sample », une liste de
deltas), pas une cadence globale. La cadence constante n'est qu'un cas particulier où tous les deltas
sont égaux. FFmpeg le dit à sa manière en documentant `r_frame_rate` (§ 2.2) : c'est « the least
common multiple of all framerates in the stream », et « **Note, this value is just a guess!** » — une
formulation qui n'a de sens que parce qu'un flux peut contenir plusieurs cadences.

### 5.2 Ce que ça casse, point par point

| Section | Effet du VFR |
|---|---|
| § 2 — cadence | **Détruit.** Il n'y a pas de cadence à obtenir : ni la saisie manuelle, ni les deltas de `mediaTime`, ni `r_frame_rate` ne décrivent le fichier. |
| § 4.2/4.3 — arithmétique | **Détruite.** `1/fps`, `(n + 0.5)/fps`, `n * duree_frame` : tout calcul de la position de la frame `n` est faux. |
| § 1.2 — `mediaTime` | **Intact.** C'est un PTS lu dans le média, pas un calcul. Il reste exact. |
| § 3 — seek | **Intact.** « la frame dont l'intervalle contient `t` » reste bien définie : les intervalles sont juste inégaux. Désigner une frame par un instant reste correct. |

**Autrement dit, le VFR casse la capacité à *calculer* la frame suivante, pas la capacité à
*désigner* la frame courante.** C'est une dégradation sérieuse mais pas fatale, parce que la méthode
de la carte a besoin de désigner une frame — une seule, le début de mouvement — pas d'énumérer.

### 5.3 Une fissure supplémentaire côté Chromium

Le test de sélection de frame rappelé au § 3.2 utilise
`frame.metadata().frame_duration.value_or(last_decoder_stream_avg_duration_)`. Quand la durée réelle
de la frame n'est pas propagée jusque-là, la décision « quelle frame contient `t` » se fait donc avec
une **durée moyenne**. En VFR, une frame courte suivie d'une longue peut alors être mal bornée près
d'une frontière. Argument supplémentaire, et indépendant, pour viser le milieu de l'intervalle
(§ 4.2) et pour vérifier via `mediaTime` plutôt que de faire confiance à la cible.

### 5.4 Les deux parades

**a. Sonde — 100 % natif.** Pour avancer d'une frame sans connaître la durée : seeker à
`mediaTime + ε`, avec `ε` initialisé à la plus petite durée observée jusque-là, et le doubler tant que
`mediaTime` ne change pas. Converge en 1 à 3 seeks en pratique. Coûteux mais borné, et parfaitement
acceptable pour un geste ponctuel : l'opérateur cherche *une* frame, il ne parcourt pas le take.

**b. Table de PTS précalculée côté serveur.** `ffprobe -show_frames` donne la liste exacte des PTS ;
stockée dans le dossier du take, elle rend le pas-à-pas exact et O(1), en VFR comme en CFR, et
supprime toute la § 2. C'est la voie robuste. Elle est cohérente avec l'acquis n°9 de la carte (« le
précalcul déterministe est légitime pour ce qu'on inspecte »), mais elle introduit la dépendance
`ffprobe` discutée au § 2.2 — décision à prendre, pas détail d'implémentation.

Recommandation : **(a) suffit pour ce que la carte demande**, et (b) est la porte de sortie si les
rushes réels s'avèrent VFR et que le geste devient pénible. Ne pas ouvrir (b) par précaution.

---

## 6. La question npm : la réponse tient-elle en JS natif ?

**Oui, entièrement.** `requestVideoFrameCallback`, `currentTime`, l'événement `seeked` : tout est de
la plateforme, disponible depuis un module ES brut servi par le dépôt, hors ligne. Aucune dépendance,
aucun build, aucun CDN. La contrainte du dépôt est respectée sans compromis.

**Et l'alternative WebCodecs ?** Elle donnerait un contrôle frame-exact absolu (c'est d'ailleurs ce
que l'éditeur de rVFC recommande pour « extraire toutes les frames », § 1.5-5). Mais WebCodecs
**exclut explicitement les conteneurs** : « Direct APIs for media containers (muxers/demuxers) » figure
dans les non-objectifs, et l'explainer précise que c'est à l'application de démuxer — « App demuxes
(decontainerizes) input and makes repeated calls to the provided callbacks to feed the decoders »
([explainer WebCodecs](https://github.com/w3c/webcodecs/blob/main/explainer.md)).

Il faudrait donc **vendorer un démuxeur MP4 en JavaScript**. Le dépôt a un précédent (three.js est
vendoré en modules ES bruts dans `api/viz/vendor/`), donc ce n'est pas interdit par principe. Mais
c'est un composant nettement plus lourd et plus fragile que three.js, pour un geste — désigner une
frame en pause — que rVFC couvre déjà. **Non retenu**, et à ne rouvrir que si le pas-à-pas natif
s'avère insuffisant à l'usage.

---

## Récapitulatif : réponses aux cinq questions du ticket

1. **rVFC** — Chrome 83, Safari 15.4, Firefox 132 ; spec WICG non standards-track. `mediaTime` est le
   PTS de la frame présentée et **c'est la seule identité de frame que la plateforme expose**.
   `presentedFrames` est un compteur de présentations au compositeur, pas un index de frame, et ne se
   recale pas sur les seeks — **by design**, dixit l'éditeur : inutilisable pour compter des frames du
   fichier. La garantie forte (« la bonne frame après un seek ») n'existe **qu'en pause**, et c'est
   une garantie d'implémenteur Chromium, pas normative.
2. **Cadence** — aucune API ne la donne, et `seekToNextFrame()` a été retiré de Firefox 128 : il
   n'existe plus aucune primitive native de pas-à-pas. Trois voies : deltas de `mediaTime` (natif),
   `ffprobe` côté serveur (dépendance binaire absente aujourd'hui), saisie manuelle. **Mais la
   méthode fermée n'a pas besoin de la cadence pour désigner une frame.**
3. **Seek** — normativement **exact** : le snap au keyframe est réservé au drapeau
   `approximate-for-speed`, que seul `fastSeek()` pose. Chromium et WebKit sont conformes (keyframe
   antérieur puis rejet jusqu'à la cible ; tolérance zéro). Le vrai piège n'est pas le seek, c'est que
   **`currentTime` relu renvoie la cible demandée, pas le PTS affiché** — normatif, et visible dans le
   code des deux moteurs.
4. **Un pas exact** — pas de primitive ; boucle fermée `rVFC d'abord → seek → attendre seeked + rVFC
   → lire mediaTime → corriger`. Viser **le milieu** de l'intervalle, jamais la frontière.
   `currentTime += 1/fps` échoue d'abord parce que `currentTime` n'est pas le PTS affiché, ensuite par
   accumulation flottante et requantification, et complètement en VFR. Reculer coûte un GOP.
5. **VFR** — détruit la notion de cadence et toute arithmétique de frame ; laisse **intacts**
   `mediaTime` et la sémantique du seek. On perd la capacité de *calculer* la frame suivante, pas
   celle de *désigner* la frame courante. Parade native : sonde `mediaTime + ε`. Parade robuste :
   table de PTS `ffprobe`.

---

## Sources

Toutes consultées le 2026-08-05.

**Normatif**
- WICG, [`HTMLVideoElement.requestVideoFrameCallback()`](https://wicg.github.io/video-rvfc/) — Draft
  Community Group Report, 2 août 2024
- WHATWG HTML Standard, [§ 4.8.11.9 Seeking](https://html.spec.whatwg.org/multipage/media.html#seeking),
  [§ 4.8.11.6 Offsets into the media resource](https://html.spec.whatwg.org/multipage/media.html#offsets-into-the-media-resource),
  [§ 4.8.9 The video element](https://html.spec.whatwg.org/multipage/media.html#the-video-element)
- W3C, [Media Playback Quality](https://w3c.github.io/media-playback-quality/)
- W3C, [WebCodecs](https://w3c.github.io/webcodecs/) et son
  [explainer](https://github.com/w3c/webcodecs/blob/main/explainer.md)

**Support**
- [MDN — `requestVideoFrameCallback`](https://developer.mozilla.org/en-US/docs/Web/API/HTMLVideoElement/requestVideoFrameCallback)
- [mdn/browser-compat-data — `api/HTMLVideoElement.json`](https://github.com/mdn/browser-compat-data/blob/main/api/HTMLVideoElement.json),
  [`api/HTMLMediaElement.json`](https://github.com/mdn/browser-compat-data/blob/main/api/HTMLMediaElement.json)
- [chromestatus — HTMLVideoElement.requestVideoFrameCallback()](https://chromestatus.com/feature/6335927192387584)
- [WebKit — New WebKit Features in Safari 15.4](https://webkit.org/blog/12445/new-webkit-features-in-safari-15-4/)
- [Firefox 132 release notes](https://www.firefox.com/en-US/firefox/132.0/releasenotes/)

**Implémentations (code source)**
- Chromium — [`media/filters/ffmpeg_demuxer.cc`](https://github.com/chromium/chromium/blob/main/media/filters/ffmpeg_demuxer.cc),
  [`media/renderers/video_renderer_impl.cc`](https://github.com/chromium/chromium/blob/main/media/renderers/video_renderer_impl.cc),
  [`third_party/blink/renderer/core/html/media/html_media_element.cc`](https://github.com/chromium/chromium/blob/main/third_party/blink/renderer/core/html/media/html_media_element.cc),
  [`third_party/blink/renderer/platform/media/web_media_player_impl.cc`](https://github.com/chromium/chromium/blob/main/third_party/blink/renderer/platform/media/web_media_player_impl.cc)
- WebKit — [`Source/WebCore/html/HTMLMediaElement.cpp`](https://github.com/WebKit/WebKit/blob/main/Source/WebCore/html/HTMLMediaElement.cpp)
- FFmpeg — [`libavformat/avformat.h`](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/avformat.h),
  [doc ffprobe](https://ffmpeg.org/ffprobe.html)

**Discussions d'implémenteurs** (WICG/video-rvfc — éditeur de la spec et propriétaire media Chromium)
- [#53](https://github.com/WICG/video-rvfc/issues/53) — callbacks en pause / hors écran
- [#59](https://github.com/WICG/video-rvfc/issues/59) — ce que `mediaTime` garantit, et la rétractation
- [#64](https://github.com/WICG/video-rvfc/issues/64) — frames B, et pourquoi rVFC y échappe
- [#66](https://github.com/WICG/video-rvfc/issues/66) — extraire toutes les frames : impossible de façon fiable
- [#69](https://github.com/WICG/video-rvfc/issues/69) — garantie en pause ; test tiers 1 000 itérations
- [#82](https://github.com/WICG/video-rvfc/issues/82) — `presentedFrames` et les seeks

**Bugs ouverts, non confirmés** (à revérifier avant de choisir un navigateur cible)
- [Mozilla 1941279](https://bugzil.la/1941279) — seeks coalescés depuis rAF/rVFC en H.264
- [Mozilla 1935256](https://bugzil.la/1935256) — rVFC plafonnée à ~40 ms
- [Mozilla 1908246](https://bugzil.la/1908246) — `processingDuration` non implémenté
- Chromium 66631 — frames B et seek ; statut actuel non vérifiable (tracker fermé au public), connu
  seulement par sa citation dans [WICG/video-rvfc#64](https://github.com/WICG/video-rvfc/issues/64)
