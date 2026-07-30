// ── Boot ────────────────────────────────────────────────────────────────────
// View-only frontend: observation is pushed by the backend over /api/ws at
// ~4 Hz and merely rendered here; commands stay REST. If the socket drops we
// fall back to REST polling (see store.js).

import { connect } from "./store.js";
import { initLayout } from "./panels/layout.js";
import { initTopbar } from "./panels/topbar.js";
import { initHealth } from "./panels/health.js";
import { initLive } from "./panels/live.js";
import { initSession } from "./panels/session.js";
import { initRecording } from "./panels/recording.js";
import { initPlayback, refreshSessions } from "./panels/playback.js";
import { initTakes } from "./panels/takes.js";
import { initEsp } from "./panels/esp.js";
import { initPresets } from "./panels/presets.js";
import { initScope } from "./panels/scope.js";
import { initParams } from "./panels/params.js";
import { initOsc } from "./panels/osc.js";
import { initShortcuts } from "./shortcuts.js";

initLayout();
initTopbar();
initHealth();
initLive();
initSession();
initRecording();
initPlayback();
initTakes();
initEsp();
initPresets();
initScope();
initParams();
initOsc();
initShortcuts();

refreshSessions();
connect();
