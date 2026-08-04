// ── Boot ────────────────────────────────────────────────────────────────────
// View-only frontend: observation is pushed by the backend over /api/ws at
// ~4 Hz and merely rendered here; commands stay REST. If the socket drops we
// fall back to REST polling (see store.js).
//
// Panels are laid out across workspace tabs (see tabs.js). Hidden tabs keep
// their DOM, so every module below initialises and renders unconditionally —
// only work that *measures* an element has to care which tab is showing.

import { connect } from "./store.js";
import { initTabs } from "./tabs.js";
import { initTopbar } from "./panels/topbar.js";
import { initHealth } from "./panels/health.js";
import { initLive } from "./panels/live.js";
import { initSession } from "./panels/session.js";
import { initRecording } from "./panels/recording.js";
import { initPlayback, refreshSessions } from "./panels/playback.js";
import { initTakes } from "./panels/takes.js";
import { initEsp } from "./panels/esp.js";
import { initScope } from "./panels/scope.js";
import { initParams } from "./panels/params.js";
import { initOsc } from "./panels/osc.js";
import { initShortcuts } from "./shortcuts.js";

initTopbar();
initHealth();
initLive();
initSession();
initRecording();
initPlayback();
initTakes();
initEsp();
initScope();
initParams();
initOsc();
initShortcuts();

// Last, so the first tab-change notification reaches every module that
// subscribed during its own init (the scope in particular).
initTabs();

refreshSessions();
connect();
