# Servir la vidéo d'un take au navigateur

Recherche pour [#5](https://github.com/Zaruitoga/conductor/issues/5) (carte [#2](https://github.com/Zaruitoga/conductor/issues/2)).
Recherche seulement : **aucun correctif n'est appliqué ici**.

Le dépôt n'avait pas encore de dossier de notes de recherche ; celui-ci inaugure `docs/research/`,
à côté du `docs/agents/` existant.

---

## Résumé exécutif

1. **`Range` marche déjà, sans rien écrire.** Starlette **1.2.1** est installé ; le support est arrivé en
   **0.39.0**. `FileResponse` *et* `StaticFiles` répondent `206` + `Content-Range`. Vérifié bout en bout ici.
2. **Endpoint dédié**, pas de mount statique : un mount publie `raw.csv` et `take.json` avec la vidéo, et
   se paie une contrainte d'ordre supplémentaire face au catch-all `/`.
3. **⚠️ La traversée de chemin est un trou *déjà présent*, pas un risque futur.** `POST /api/playback/start`
   accepte un chemin absolu depuis son corps JSON et lit `<n'importe où>/raw.csv`. Indépendant de la vidéo.
4. **Servir un `Content-Type` faux est pire que n'en servir aucun** : `application/octet-stream` est
   explicitement rattrapé par la spec HTML, un type reconnu-mais-faux fait échouer le `<video>`.

---

## 1. Le Starlette installé supporte-t-il `Range` ?

**Oui — sans une ligne de code à écrire.**

### Versions constatées dans cet environnement

```
$ python3 -m pip show starlette fastapi uvicorn
```

| Paquet | Version installée |
|---|---|
| starlette | **1.2.1** |
| fastapi | **0.136.3** |
| uvicorn | **0.48.0** |
| python | 3.12.4 (`/Library/Frameworks/Python.framework/Versions/3.12`) |

Commande équivalente plus courte :

```
$ python3 -c "import starlette, fastapi; print(starlette.__version__, fastapi.__version__)"
1.2.1 0.136.3
```

### À quelle version le support est-il apparu

Changelog Starlette, [`docs/release-notes.md`](https://github.com/encode/starlette/blob/master/docs/release-notes.md) —
**0.39.0 (23 septembre 2024)**, section *Added* :

> Add support for [HTTP Range](https://developer.mozilla.org/en-US/docs/Web/HTTP/Range_requests) to `FileResponse` [#2697](https://github.com/Kludex/starlette/pull/2697).

Affinages ultérieurs : **0.39.1** (25 sept. 2024) « Consider `FileResponse.chunk_size` when handling multiple
ranges » ([#2703](https://github.com/Kludex/starlette/pull/2703)) ; **1.0.0rc1** (fév. 2026) corrige les
réponses multi-plages (`multipart/byteranges`, CRLF dans les frontières — [#3143](https://github.com/Kludex/starlette/pull/3143), [#3142](https://github.com/Kludex/starlette/pull/3142)).

**1.2.1 ≫ 0.39.0 : on est très largement au-dessus du seuil.** La marge est telle qu'aucune contrainte de
version minimale n'a besoin d'être documentée pour ce ticket.

### Le code réellement installé

`site-packages/starlette/responses.py`, classe `FileResponse` :

- `l. 319` — `self.headers.setdefault("accept-ranges", "bytes")`, posé sur **toute** réponse fichier.
- `l. 362-380` — lit `Range` et `If-Range`, parse, puis `_handle_single_range` (`206`) ou
  `_handle_multiple_ranges` (`multipart/byteranges`).
- `l. 403-405` — `content-range: bytes {start}-{end-1}/{file_size}`, statut `206`.
- `l. 372-374` — plage hors bornes ⇒ `416` avec `Content-Range: bytes */{taille}`.
- `l. 454-455` — `If-Range` comparé au `last-modified` **ou** à l'`etag`.

`StaticFiles` en hérite sans rien faire de plus : `staticfiles.py:184` construit un `FileResponse`.
Documentation officielle ([`docs/responses.md`](https://github.com/encode/starlette/blob/master/docs/responses.md)) :

> File responses also supports HTTP range requests. The `Accept-Ranges: bytes` header will be included in the
> response if the file exists. For now, only the `bytes` range unit is supported.

### Vérification bout en bout dans cet environnement

Un fichier `.mp4` de 10 240 octets de contenu connu, servi par les deux chemins, interrogé via `TestClient` —
octets renvoyés comparés octet à octet à la tranche attendue :

| Requête | Statut | En-têtes | Corps |
|---|---|---|---|
| `GET` sans `Range` | `200` | `accept-ranges: bytes`, `content-length: 10240` | 10240 o |
| `Range: bytes=0-99` | **`206`** | `content-range: bytes 0-99/10240` | 100 o — **contenu exact** |
| `Range: bytes=5000-5009` | **`206`** | `content-range: bytes 5000-5009/10240` | 10 o — **contenu exact** |
| `Range: bytes=-50` (suffixe) | **`206`** | `content-range: bytes 10190-10239/10240` | 50 o |
| `Range: bytes=999999-` | **`416`** | `content-range: bytes */10240` | 0 o |
| via mount `StaticFiles`, `bytes=0-99` | **`206`** | `content-range: bytes 0-99/10240` | 100 o |

Le seek du navigateur est donc acquis. MDN, [Range requests](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Range_requests) :

> Range requests are useful for various clients, including **media players that support random access** […]

**Conséquence pour le ticket :** l'essentiel du travail se réduit à choisir *où* brancher un `FileResponse`.
Aucun traitement manuel de `Range`, et **aucune dépendance nouvelle** — contrainte du dépôt respectée.

---

## 2. Mount statique ou endpoint dédié ?

### Ce qu'un mount coûterait

`app.mount("/media", StaticFiles(directory="sessions"))` :

- **Publie tout l'arbre.** Vérifié : `take.json` se récupère par le même mount (`200`,
  `content-type: application/json`). `raw.csv` aussi. Or `take.json` contient `performer`, `notes`,
  `imu_config` — des données de séance, pas des assets web. `sessions/.active` également.
- **Ne peut pas répondre 404 « pas de vidéo pour ce take »** : il ne connaît que des fichiers, pas des takes.
  Le client devrait d'abord lire `video_file` dans le snapshot ou via `GET /api/sessions`, puis fabriquer
  l'URL — le couplage nom-de-fichier↔URL fuit dans le frontend, ce que l'acquis n°1 de la carte
  (« `video_file` est un nom de fichier, pas un chemin ») cherche justement à éviter.
- **Contrainte d'ordre supplémentaire.** `api/app.py:43-46` porte déjà le commentaire :

  ```python
  # The root mount is a catch-all, so /viz must be registered before it.
  app.mount("/viz", StaticFiles(directory=_VIZ_DIR, html=True), name="viz")
  app.mount("/",    StaticFiles(directory=_STATIC_DIR), name="static")
  ```

  Un troisième mount devient un troisième invariant d'ordre non testé. L'acquis n°8 de la carte prévoit
  **déjà** un mount de plus pour la page d'alignement ; en ajouter un pour les médias en fait deux.

- **`sessions/` n'existe pas au démarrage** tant qu'aucune session n'a été créée. `StaticFiles` vérifie le
  répertoire (`check_dir=True` par défaut) et **lève au montage** si absent. Il faudrait un `makedirs`
  préalable ou `check_dir=False` — une subtilité de plus à retenir. (`SessionManager.__init__` fait bien un
  `makedirs`, mais il s'exécute à l'import de `core`, pas nécessairement avant le montage.)

### Ce qu'un endpoint dédié apporte

`GET /api/sessions/{session}/takes/{take}/video` :

- Sert **la vidéo et rien d'autre** — `raw.csv` et `take.json` restent hors du web.
- **Résout `video_file` côté serveur** depuis `take.json` : l'URL est dérivable de `(session, take)` seuls,
  que le frontend possède déjà. Renommer le fichier sur le disque ne change aucune URL.
- **Valide ses arguments** (point 3) et distingue proprement `404 take inconnu` / `404 pas de vidéo`.
- Vit sous le préfixe `/api` **déjà routé avant les mounts** (`include_router` en `app.py:35`) : zéro
  contrainte d'ordre nouvelle.
- `return FileResponse(chemin)` suffit — `Range` inclus (point 1).

### Recommandation

**Endpoint dédié.** Le mount n'économise qu'une poignée de lignes et les rachète en surface exposée, en
couplage frontend et en invariant d'ordre. Le seul argument réel du mount — « `Range` gratuit » — n'en est
pas un : `FileResponse` le donne aussi.

> À noter tout de même : `StaticFiles` est, lui, **correctement défendu** contre la traversée (point 3).
> Un endpoint dédié doit reproduire cette défense explicitement, sans quoi il serait *moins* sûr que le mount.

---

## 3. Traversée de chemin — ⚠️ trou déjà ouvert

### Le code en cause

`storage/session_manager.py:300-307` :

```python
def session_path(self, session: str) -> str:
    return os.path.join(self.sessions_dir, session)

def take_path(self, session: str, take: str) -> str:
    return os.path.join(self.sessions_dir, session, "takes", take)

def csv_path(self, take_dir: str) -> str:
    return os.path.join(take_dir, "raw.csv")
```

Aucune validation. **Deux vecteurs distincts**, dont le second est le plus souvent oublié :

```
take_path('a', '../../../../config.py')  ->  sessions/a/takes/../../../../config.py
                                         ->  <repo>/../config.py
take_path('a', '/etc/passwd')            ->  /etc/passwd          ← le préfixe disparaît
take_path('/etc', 'foo')                 ->  /etc/takes/foo
```

Le second est propre à `os.path.join` : **un composant absolu écrase tout ce qui précède**. Pas besoin
d'un seul `..`. C'est aussi exactement le cas que `StaticFiles` traite en premier (`staticfiles.py:155-157`,
commentaire : *« Reject absolute paths so they cannot escape the served directory »*).

### Les routes existantes sont-elles déjà vulnérables ?

Testé en pilotant le routeur et l'application ASGI directement.

#### `POST /api/playback/start` — **oui, vulnérable, sans réserve**

`api/routes.py:337-350`, corps validé par `PlaybackRequest` (`api/models.py:61-66`) :

```python
class PlaybackRequest(BaseModel):
    session: str
    take: str
```

`str` nu, aucune contrainte. Les valeurs arrivent **verbatim** jusqu'à `take_path` :

| Corps JSON | Reçu par le handler |
|---|---|
| `{"session": "../../..", "take": "x"}` | `('../../..', 'x')` |
| `{"session": "a", "take": "/etc/passwd"}` | `('a', '/etc/passwd')` |
| `{"session": "a", "take": "../../../../config.py"}` | `('a', '../../../../config.py')` |

Ce sont des valeurs de **corps JSON** : contrairement à un segment d'URL, elles ne subissent **aucune**
normalisation, ni par le client, ni par le routeur. Un `POST` ordinaire suffit — pas de client exotique.

Puis `api/routes.py:344` :

```python
if not os.path.exists(sm.csv_path(sm.take_path(req.session, req.take))):
```

et `storage/playback_engine.py:108-111, 166` reprend le même chemin et l'ouvre en lecture.

**Impact réel :**
- **Oracle d'existence** sur `<n'importe quel répertoire>/raw.csv` (404 vs 200), n'importe où sur le disque.
- **Lecture de fichier arbitraire limitée aux fichiers nommés `raw.csv`** : le contenu est parsé en CSV et
  rejoué sur le bus — donc diffusé aux abonnés WebSocket (8081) et à l'OSC.

Le suffixe fixe `raw.csv` **contient** les dégâts. C'est une limitation heureuse, pas une défense :
elle tient au fait qu'aucun nom de fichier n'est contrôlé par l'appelant. **Le ticket #5 propose précisément
d'introduire un endpoint où un nom de fichier le serait** — voir plus bas.

#### `PATCH /api/sessions/{session}/takes/{take}` — vulnérable, mais bien plus étroitement

`api/routes.py:321-332`. Les valeurs viennent de l'URL, donc deux filtres s'interposent :

1. **uvicorn décode le chemin avant le routage** — `uvicorn/protocols/http/h11_impl.py:201` :
   `path = unquote(raw_path.decode("ascii"))`. Un `%2F` devient un vrai `/` **avant** que Starlette ne
   compare la route.
2. Le convertisseur `{session}` de Starlette ne matche qu'un segment (`[^/]+`).

Conséquence : les `%2F` **échouent** (segments surnuméraires ⇒ la route ne matche plus).

| Chemin | Résultat |
|---|---|
| `/api/sessions/..%2F..%2Fetc/takes/x` | `404` |
| `/api/sessions/%2Fetc/takes/x` | `404` |
| `/api/sessions/a/takes/%2Fetc%2Fpasswd` | `404` |
| `/api/sessions/a/takes/..%2F..%2F..%2F..%2Fconfig.py` | `404` |

**Mais un `..` non encodé passe.** Starlette **ne normalise pas** `scope["path"]` pour les routes ordinaires
(seul `StaticFiles` le fait, `staticfiles.py:107`). En appelant l'application ASGI directement — ce que fait
un client brut, `curl --path-as-is`, ou un proxy non normalisant :

| Chemin brut | Statut | Reçu par le handler |
|---|---|---|
| `/api/sessions/../takes/x` | `200` | `('..', 'x')` |
| `/api/sessions/../takes/..` | `200` | `('..', '..')` |
| `/api/sessions/../../takes/x` | `404` | — |

`take_path('..', '..')` → `sessions/../takes/..` → **racine du dépôt**, où `update_take` lit puis **réécrit**
`take.json`. La profondeur d'évasion est plafonnée à un seul segment `..` (un second exigerait un `/`, filtré),
et le nom de fichier reste `take.json` : écriture JSON semi-arbitraire, un ou deux niveaux au-dessus de
`sessions/`. **Sérieux mais nettement moins que `playback/start`**, et il faut un client qui ne normalise pas.

#### Le vecteur chaîné que #5 ouvrirait

`video_file` est **éditable par l'utilisateur** — `TAKE_EDITABLE` (`session_manager.py:39-40`) et
`TakeUpdate.video_file: str | None` (`api/models.py:57`), `str` libre, aucune validation.

Un endpoint vidéo qui ferait naïvement `os.path.join(take_dir, meta.video_file)` deviendrait une
**lecture de fichier vraiment arbitraire** : plus de suffixe fixe pour contenir les dégâts, puisque le nom
complet vient de l'appelant. Enchaînement : `PATCH … {"video_file": "../../../../../../etc/passwd"}`
puis `GET …/video`.

> **Donc `video_file` doit être validé aux *deux* bouts** : à l'écriture (`PATCH`, refuser tout ce qui n'est
> pas un nom de fichier simple) et à la lecture (endpoint vidéo, confinement). Valider seulement à l'écriture
> laisserait passer les `take.json` déjà sur le disque ou édités à la main.

### La façon idiomatique de fermer ça

Il n'y a pas de garde-fou magique côté FastAPI : `{param}` est un `str`, et aucun type Pydantic intégré ne
signifie « segment de chemin sûr ». La pratique idiomatique est celle que **Starlette applique déjà à
`StaticFiles`** — donc du code présent dans une dépendance installée, copiable sans rien ajouter :

`staticfiles.py:154-173` :

```python
def lookup_path(self, path):
    # Reject absolute paths so they cannot escape the served directory.
    if path.startswith(("/", "\\")):
        return "", None
    for directory in self.all_directories:
        joined_path = os.path.join(directory, path)
        full_path = os.path.realpath(joined_path)
        directory = os.path.realpath(directory)
        if os.path.commonpath([full_path, directory]) != str(directory):
            # Don't allow misbehaving clients to break out of the static files directory.
            continue
```

Trois couches, dans cet ordre :

1. **Rejeter l'absolu** — neutralise l'écrasement par `os.path.join`.
2. **`os.path.realpath`** — résout `..` **et** les liens symboliques (un `normpath` seul laisse passer un
   symlink pointant hors de l'arbre).
3. **`os.path.commonpath([complet, racine]) != racine` ⇒ refus** — le confinement proprement dit.

Vérifié : le mount `StaticFiles` résiste bien (`/media/../../../../etc/hosts` → `404`,
`/media/..%2F..%2Fetc%2Fhosts` → `404`).

Deux options pour ce dépôt, non exclusives :

- **Valider par forme** — un nom de session/take est engendré par `_slug()` (`session_manager.py:60-64`),
  donc `[A-Za-z0-9._-]+` suffit largement. En FastAPI cela s'exprime déclarativement, sans code de garde :
  `Path(..., pattern=r"^[A-Za-z0-9._-]+$")` pour les paramètres d'URL, `Field(pattern=...)` dans
  `PlaybackRequest`/`TakeUpdate` — un rejet devient un `422` automatique. Attention : ce motif autorise
  encore le segment `..` littéral, à exclure explicitement.
- **Confiner par résolution** — appliquer les trois couches ci-dessus dans `take_path()`/`session_path()`
  eux-mêmes. **C'est l'endroit qui ferme tous les appelants d'un coup**, présents et futurs, plutôt que
  route par route — cinq appelants aujourd'hui (`update_take`, `playback_start`, `PlaybackEngine.start`,
  `new_take`, `list_takes`).

La ceinture *et* les bretelles se justifient ici : la validation par forme donne un message d'erreur clair et
précoce, le confinement garantit l'invariant même si une route future oublie la validation.

**Rien de tout cela n'est implémenté dans le cadre de ce ticket de recherche** — c'est un constat, à traiter
par son propre ticket.

---

## 4. Types MIME et ce que `<video>` accepte

### Ce que devine cette machine

```
$ python3 -c "import mimetypes; print(mimetypes.guess_type('a.mp4'))"
```

| Extension | `guess_type` ici | Présent dans la table **intégrée** à Python ? |
|---|---|---|
| `.mp4` | `video/mp4` | **oui** |
| `.mov` | `video/quicktime` | **oui** |
| `.webm` | `video/webm` | **oui** |
| `.m4v` | `video/x-m4v` | **non** |
| `.mkv` | `video/x-matroska` | **non** |
| `.ogv` | `video/ogg` | **non** |
| `.avi` | `video/x-msvideo` | **non** |
| `.hevc` | *(None)* | non |

**Piège :** `mimetypes` s'initialise à partir des fichiers système (`mimetypes.knownfiles`) — ici
`/etc/apache2/mime.types` existe et fournit `.mkv`, `.m4v`, `.ogv`, `.avi`. **Ces trois-là disparaissent sur
une machine sans ce fichier** (image Docker minimale, autre distribution), où `FileResponse` retomberait
alors sur `application/octet-stream` (`responses.py:315`).

Autrement dit : la déduction automatique est **dépendante de la machine**. Un dictionnaire explicite dans le
code — ou un `mimetypes.add_type()` au démarrage — rend le comportement reproductible. À faire d'autant plus
volontiers que le `media_type` de `FileResponse` est un simple argument.

### Le `Content-Type` influence-t-il ce que `<video>` lit ?

**Oui, et de façon contre-intuitive : un type faux est pire que pas de type.**

Spécification HTML (WHATWG), algorithme de chargement d'une ressource média :

> The MIME type `application/octet-stream` with no parameters is never a type that the user agent knows it
> cannot render.

et il doit être traité

> as equivalent to the lack of any explicit `Content-Type` metadata when it is used to label a potential
> media resource.

Trois conséquences pratiques :

1. **`application/octet-stream` (sans paramètre) est sûr** — la spec le rattrape explicitement, le navigateur
   sonde le conteneur. Un `.mkv` servi en `octet-stream` sur une machine sans `/etc/apache2/mime.types`
   fonctionnera donc quand même, pour peu que le navigateur sache décoder le conteneur.
2. **Un type reconnu mais faux fait échouer la lecture** — servir un MP4 en `text/plain`, en
   `application/json` ou en `video/webm` désigne un type « que l'agent sait ne pas pouvoir rendre » pour cette
   ressource ; l'élément part en erreur. C'est le vrai risque : il vient d'une table MIME approximative, pas
   de l'absence de table.
3. **Attention au `application/octet-stream; charset=…`** — la spec est explicite : le rattrapage ne vaut que
   **sans paramètre**. `if any parameter appears with it, it will be treated just like any other MIME type`.

L'attribut `type` de `<source>` et `canPlayType()` opèrent **avant** la requête (sélection de la source), et
renvoient `""` / `"maybe"` / `"probably"`. Ils ne corrigent pas un `Content-Type` erroné servi ensuite.

Ne pas envoyer `X-Content-Type-Options: nosniff` sur cette réponse : cela supprimerait précisément la
tolérance du point 1.

### Ce qu'il faut servir

- **`video/mp4`** (`.mp4`, et `.m4v`) — le cas nominal, jouable partout.
- **`video/quicktime`** (`.mov`) — sort des caméras. **Le type est correct mais ne garantit rien** : un `.mov`
  en HEVC/H.265 n'est pas lu par tous les navigateurs. C'est le point « Transcodage » laissé en brouillard par
  la carte #2 ; il relève du codec, **pas** du `Content-Type`, et aucun en-tête ne le résoudra.
- **`video/webm`** (`.webm`) — si la source en produit.
- **Défaut `application/octet-stream`** pour tout le reste, plutôt qu'un type deviné approximativement.

Une liste blanche d'extensions sert d'ailleurs les deux objectifs à la fois : elle fixe le `Content-Type` et
elle restreint ce que l'endpoint accepte de servir (point 3).

---

## Sources

**Code réellement installé** (autorité pour « cet environnement ») :
`site-packages/starlette/responses.py` (`FileResponse`, l. 297-460) ·
`site-packages/starlette/staticfiles.py` (l. 101-187) ·
`site-packages/uvicorn/protocols/http/h11_impl.py` (l. 201).

**Amont :**
[release-notes.md](https://github.com/encode/starlette/blob/master/docs/release-notes.md) ·
[docs/responses.md](https://github.com/encode/starlette/blob/master/docs/responses.md) ·
[PR #2697](https://github.com/Kludex/starlette/pull/2697)

**Spécifications :**
[HTML — media elements](https://html.spec.whatwg.org/multipage/media.html#concept-media-load-resource) ·
[MDN — HTTP Range requests](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Range_requests)

**Dépôt :**
`api/app.py:38-46` · `api/routes.py:321-350` · `api/models.py:51-66` ·
`storage/session_manager.py:39-40, 300-307` · `storage/playback_engine.py:108-166`
