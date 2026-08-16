// sweep.js — le curseur lit la piste de pose, et rien d'autre.
//
// Sweeping a take moves the wheel without anything reaching the bus: no frame,
// no event, no OSC, and neither `LiveMonitor` nor the scope sees a thing
// (ADR 0004). That is not a discipline observed here — it is what this module
// *is*: a `GET` on `…/takes/{take}/pose` and an index over what comes back.
// Searching is not playing.
//
// Read in chunks, never whole
// ---------------------------
// A pose costs ~150 bytes of JSON, so a 15-minute take at 100 Hz is ~13 MB
// serialised on the event loop — the loop that owns `processing_loop` and can
// never drop a packet. And thinning the whole take to a few thousand points
// instead would put half a second between two poses, which for a wheel turning
// twice a second is not a trajectory but a series of unrelated orientations.
// So: full resolution, ten seconds at a time, around the cursor.
//
// The limit is a moving one
// -------------------------
// A track is streamed as it is computed, and the model runs at 50–77× real
// time, so the computation normally outruns a hand before it has finished
// grabbing the cursor. Normally — not always, and a take opened the instant it
// was recorded is exactly the case. Every reply carries where the track has got
// to (`duration_s`, `complete`), so the cursor knows where to stop and the page
// can show it. While it is still filling, that is asked again on a slow poll:
// the limit moving forward is the one change no request of ours would reveal.

const CHUNK_S = 10;      // seconds of take per request
const KEEP    = 6;       // chunks kept — a minute of take around the cursor
const EDGE_S  = 2.5;     // how early the neighbouring chunk is fetched
const POLL_MS = 1000;    // how often the limit is asked for while it moves

const enc = encodeURIComponent;

async function defaultFetch(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

/**
 * The take's poses, indexed by take time, fetched around a cursor.
 *
 * `fetchJson` is injected so the bench can drive it without a server
 * (`_harness.js`); everything else has a default that suits the page.
 */
export class PoseCursor {
  constructor({ fetchJson = defaultFetch, keep = KEEP, pollMs = POLL_MS } = {}) {
    this._fetch  = fetchJson;
    this._chunkS = CHUNK_S;
    this._keep   = keep;
    this._pollMs = pollMs;

    this.session = null;
    this.take    = null;

    // What the track says about itself: `status` (absent/computing/partial/
    // ready/failed), `duration_s` — the take time it reaches — `complete`, and
    // the geometry it was computed at, which is reported and never repaired
    // (ADR 0004).
    this.info = null;

    /** Called whenever the limit moved or a chunk landed. */
    this.onChange = null;

    this._chunks   = new Map();   // chunk index → {t, cols} (insertion-ordered)
    this._inflight = new Set();   // chunk indices being fetched, and "info"
    this._poll     = null;
    this._gen      = 0;           // bumped on open/close: a late reply is stale
  }

  // ── The take on screen ─────────────────────────────────────────────────────

  /**
   * Open a take for sweeping. Idempotent: called from the 4 Hz snapshot and on
   * every change of the take selector, exactly like `video.js`'s `setTake`.
   *
   * The first request is what *starts* the computation server-side for a take
   * that has no track yet (`GET …/pose` calls `ensure`), which is why opening a
   * take is enough to make it sweepable — nothing has to be asked for by hand.
   */
  open(session, take) {
    if (session === this.session && take === this.take) return;
    this.close();
    this.session = session;
    this.take    = take;
    if (!session || !take) return;
    this.refresh();
    this._poll = setInterval(() => this.refresh(), this._pollMs);
  }

  close() {
    this._gen++;
    this.session = this.take = null;
    this.info = null;
    this._chunks.clear();
    this._inflight.clear();
    if (this._poll !== null) { clearInterval(this._poll); this._poll = null; }
  }

  // ── What the track reaches ─────────────────────────────────────────────────

  /** The take time the track reaches, or 0 while there is nothing to sweep. */
  get limitS() {
    return this.info ? this.info.duration_s || 0 : 0;
  }

  /** Is the whole take there, or is the limit still moving forward? */
  get complete() {
    return !!(this.info && this.info.complete);
  }

  /** A track computed at another wheel geometry: usable, and worth saying. */
  get geometryMismatch() {
    return !!(this.info && this.info.geometry && !this.info.geometry.matches);
  }

  /** A take time bounded by what the track actually holds. */
  clamp(tS) {
    const limit = this.limitS;
    const t = Math.max(0, tS);
    return limit > 0 ? Math.min(t, limit) : t;
  }

  // ── The pose under the cursor ──────────────────────────────────────────────

  /**
   * The last pose at or before `tS`, or null while that stretch is on its way.
   *
   * At or before, never the nearest — the same rule `read_pose_at` follows
   * server-side: a pose is a point the wheel actually passed through, and
   * rounding forward hands back a position it had not reached yet.
   *
   * Null is not an error and not a hole: it means "not here yet". The caller
   * holds the last pose it drew rather than dropping the wheel to the origin,
   * which is a position, and a wrong one.
   */
  poseAt(tS) {
    if (!this.session) return null;
    const i = Math.floor(tS / this._chunkS);
    this._want(i);
    // The hand is moving, so the chunk it is heading for is worth having before
    // it is reached: a request costs one round trip, and a drag crosses a
    // ten-second boundary in a fraction of that.
    const into = tS - i * this._chunkS;
    if (into > this._chunkS - EDGE_S) this._want(i + 1);
    if (into < EDGE_S && i > 0)       this._want(i - 1);

    const chunk = this._chunks.get(i);
    if (!chunk) return null;
    const k = lastAtOrBefore(chunk.t, tS);
    if (k < 0) {
      // Before the first pose of this chunk — the take's very beginning, or a
      // gap the model declined to integrate, which can be wider than the edge
      // the prefetch above watches. The chunk before holds it, so ask for it
      // even from the middle of this one.
      this._want(i - 1);
      const prev = this._chunks.get(i - 1);
      if (!prev || !prev.t.length) return null;
      return pose(prev, prev.t.length - 1);
    }
    return pose(chunk, k);
  }

  // ── Requests ───────────────────────────────────────────────────────────────

  _url(params) {
    return `/api/sessions/${enc(this.session)}/takes/${enc(this.take)}/pose?${params}`;
  }

  /** Ask again where the track has got to. Polled while it is still filling. */
  async refresh() {
    if (this._inflight.has("info")) return;
    // `points=1` is the cheapest request that still carries the whole status —
    // and it is a request for *poses*, so the endpoint starts the computation
    // for a take that has none. Asking for none would have been a stranger way
    // to say the same thing.
    await this._get("info", this._url("points=1"), null);
    if (this.complete && this._poll !== null) {
      // Nothing left to watch: the limit is the take's own length now.
      clearInterval(this._poll);
      this._poll = null;
    }
  }

  _want(index) {
    if (index < 0 || this._chunks.has(index) || this._inflight.has(index)) return;
    const start = index * this._chunkS;
    this._get(index, this._url(`start=${start}&end=${start + this._chunkS}`), index);
  }

  async _get(tag, url, index) {
    this._inflight.add(tag);
    const gen = this._gen;
    let data;
    try {
      data = await this._fetch(url);
    } catch {
      // A take deleted under us, a disk hiccup, the orchestrator restarting.
      // The cursor holds what it has and the next move asks again — there is
      // nothing here worth remembering, and a failure that stuck would leave a
      // take unsweepable until the page was reloaded.
      return;
    } finally {
      this._inflight.delete(tag);
    }
    if (gen !== this._gen) return;      // another take was opened meanwhile

    const before = this.info;
    this.info = {
      status:     data.status,
      records:    data.records,
      duration_s: data.duration_s,
      complete:   data.complete,
      geometry:   data.geometry,
      error:      data.error,
    };
    if (index !== null && data.poses && data.poses.t.length) {
      this._chunks.set(index, {
        t: data.poses.t,
        cols: data.poses,
      });
      // Insertion order is the eviction order, which is close enough to
      // "furthest from the cursor" for a hand moving through a take, and costs
      // no bookkeeping on the hot path.
      while (this._chunks.size > this._keep) {
        this._chunks.delete(this._chunks.keys().next().value);
      }
    }
    if (this.onChange && (index !== null || changed(before, this.info))) {
      this.onChange(this.info);
    }
  }
}

function changed(a, b) {
  return !a || a.status !== b.status || a.duration_s !== b.duration_s
         || a.complete !== b.complete || a.error !== b.error;
}

/** The index of the last `t` at or before `tS`, or −1. */
function lastAtOrBefore(t, tS) {
  let lo = 0, hi = t.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (t[mid] <= tS) lo = mid + 1; else hi = mid;
  }
  return lo - 1;
}

/**
 * One record as the renderer wants it.
 *
 * A missing component stays null — a wheel recorded without a gyro has no
 * horizontal position *at all*, and a nought there would draw it sitting at the
 * origin, which is a fact about a different take.
 */
function pose(chunk, k) {
  const c = chunk.cols;
  return {
    t:  c.t[k],
    qw: c.qw[k], qx: c.qx[k], qy: c.qy[k], qz: c.qz[k],
    x:  c.x[k],  y:  c.y[k],  z:  c.z[k],
  };
}
