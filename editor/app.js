/* Belgium Concert — Performance Marker GUI
 * Three proxy videos, frame-synced, with a master clock (the audio track).
 * Mark in/out + title/composer per performance, save to markers.json.
 * Times are stored in SECONDS (float), valid against the original full-res clips.
 */
'use strict';

const $ = (s) => document.querySelector(s);
const fmtTC = (t) => {
  if (!isFinite(t) || t < 0) t = 0;
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = Math.floor(t % 60);
  const ms = Math.round((t - Math.floor(t)) * 1000);
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`;
};
const fmtDur = (t) => {
  const m = Math.floor(t / 60), s = Math.round(t % 60);
  return `${m}:${String(s).padStart(2,'0')}`;
};

const State = {
  meta: null,
  duration: 5764.7,
  fps: 60,
  clips: [],
  videos: [],          // <video> elements in track order
  master: null,        // master <video> (the audio clip)
  playing: false,
  pendingIn: null,
  pendingOut: null,
  perfs: [],
  selected: -1,
  seed: 42,
  dirty: false,
  undoStack: [],       // snapshots of perfs/seed taken before each edit
  transcript: { ready: false, segments: [], activeIdx: -1 },
  view: { start: 0, span: 0 },     // visible window of the main timeline (seconds)
  wave: { ready: false, peaks: null, pps: 100, duration: 0 },
  tl: {},                          // canvas refs / offscreen cache
  tlDirty: true,                   // main static layer needs redraw
  ovDirty: true,                   // overview static layer needs redraw
};

const MIN_SPAN = 4;                // most-zoomed-in window (seconds)

/* ----------------------------------------------------------------- boot */
async function boot() {
  const meta = await fetch('/api/meta').then(r => r.json());
  State.meta = meta;
  State.duration = meta.duration;
  State.fps = meta.fps || 60;
  State.clips = meta.clips;

  buildVideos();
  buildAudioSelect();

  State.view = { start: 0, span: State.duration };

  const m = await fetch('/api/markers').then(r => r.json()).catch(() => ({}));
  State.seed = (m && m.seed) || 42;
  $('#seedInput').value = State.seed;
  State.perfs = (m && m.performances) ? m.performances.slice() : [];
  renderPerfs();

  initTimeline();
  loadWaveform();
  loadTranscript();
  wireTranscript();

  wireTransport();
  wireForm();
  wireKeys();
  tickLoop();
  updateStatus();
}

async function loadWaveform() {
  const status = $('#waveStatus');
  const meta = await fetch('/api/waveform').then(r => r.json()).catch(() => ({ ready: false }));
  if (!meta.ready) {
    status.textContent = '⏳ generating waveform…';
    setTimeout(loadWaveform, 4000);
    return;
  }
  const buf = await fetch('/waveform.u8').then(r => r.arrayBuffer());
  State.wave = {
    ready: true, peaks: new Uint8Array(buf),
    pps: meta.peaks_per_second, duration: meta.duration,
  };
  status.textContent = '';
  markTimelineDirty();
}

function updateStatus() {
  const ready = State.clips.filter(c => c.proxy_ready).length;
  let msg = `${ready}/${State.clips.length} proxies ready · ${fmtDur(State.duration)} · ${State.fps}fps`;
  if (ready < State.clips.length) msg += '  ⚠ still encoding — reload when done';
  $('#status').textContent = msg;
}

/* --------------------------------------------------------------- videos */
function buildVideos() {
  const wrap = $('#videos');
  wrap.innerHTML = '';
  State.videos = [];
  State.clips.forEach((c, i) => {
    const track = document.createElement('div');
    track.className = 'track' + (c.is_audio ? ' audio-active' : '');
    track.dataset.id = c.id;
    track.innerHTML = `<div class="tlabel">${c.label}${c.is_audio ? ' <span class="aflag">♪ audio</span>' : ''}</div>`;
    const v = document.createElement('video');
    v.preload = 'auto';
    v.playsInline = true;
    v.muted = !c.is_audio;
    if (c.proxy_ready) {
      v.src = c.proxy_url;
    } else {
      const ld = document.createElement('div');
      ld.className = 'loading';
      ld.textContent = 'proxy still encoding…';
      track.appendChild(ld);
    }
    track.appendChild(v);
    wrap.appendChild(track);
    State.videos.push(v);
    if (c.is_audio) State.master = v;
  });
  if (!State.master) State.master = State.videos[0];
}

function buildAudioSelect() {
  const sel = $('#audioSource');
  sel.innerHTML = '';
  State.clips.forEach((c) => {
    const o = document.createElement('option');
    o.value = c.id; o.textContent = c.label;
    if (c.is_audio) o.selected = true;
    sel.appendChild(o);
  });
  sel.onchange = () => {
    State.videos.forEach((v, i) => {
      const on = State.clips[i].id === sel.value;
      v.muted = !on;
      v.closest('.track').classList.toggle('audio-active', on);
    });
    State.master = State.videos[State.clips.findIndex(c => c.id === sel.value)] || State.master;
    markDirty();
  };
}

/* ----------------------------------------------------------- sync clock */
function seekAll(t, force = false) {
  t = Math.max(0, Math.min(State.duration, t));
  State.videos.forEach(v => { if (v.src) v.currentTime = t; });
}

function syncFollowers() {
  if (!State.master) return;
  const t = State.master.currentTime;
  State.videos.forEach(v => {
    if (v === State.master || !v.src) return;
    const drift = v.currentTime - t;
    if (Math.abs(drift) > 0.06) {
      // hard correct large drift
      v.currentTime = t;
    } else if (State.playing) {
      // gentle rate nudge for small drift
      v.playbackRate = drift > 0.012 ? 0.96 : drift < -0.012 ? 1.04 : 1.0;
    }
  });
}

async function playAll() {
  State.playing = true;
  $('#playBtn').textContent = '❚❚ Pause';
  await Promise.allSettled(State.videos.map(v => v.src ? v.play() : null));
}
function pauseAll() {
  State.playing = false;
  $('#playBtn').textContent = '▶︎ Play';
  State.videos.forEach(v => { if (v.src) { v.pause(); v.playbackRate = 1.0; } });
  syncFollowers();
}
function togglePlay() { State.playing ? pauseAll() : playAll(); }

function tickLoop() {
  const t = State.master ? State.master.currentTime : 0;
  $('#timecode').textContent = fmtTC(t);
  if (State.playing) {
    syncFollowers();
    keepPlayheadInView(t);
  }
  drawTimeline(t);
  updateTranscriptHighlight(t);
  requestAnimationFrame(tickLoop);
}

function keepPlayheadInView(t) {
  const v = State.view;
  if (v.span >= State.duration) return;            // fully zoomed out
  if (t < v.start || t > v.start + v.span) {       // jumped out — recenter
    v.start = clampStart(t - v.span / 2, v.span);
    markTimelineDirty();
  } else if (t > v.start + v.span * 0.92) {         // scrolling page-by-page
    v.start = clampStart(t - v.span * 0.08, v.span);
    markTimelineDirty();
  }
}

/* ------------------------------------------------------------ transport */
function wireTransport() {
  $('#playBtn').onclick = togglePlay;
  document.querySelectorAll('[data-nudge]').forEach(b => {
    b.onclick = () => { pauseAll(); seekAll(State.master.currentTime + Number(b.dataset.nudge) / State.fps); };
  });
  document.querySelectorAll('[data-jump]').forEach(b => {
    b.onclick = () => seekAll(State.master.currentTime + Number(b.dataset.jump));
  });
  $('#markInBtn').onclick = markIn;
  $('#markOutBtn').onclick = markOut;
  $('#addBtn').onclick = addPending;
  $('#saveBtn').onclick = save;
  $('#undoBtn').onclick = undo;
  $('#audioSource');
}

// I / O write the playhead into the In / Out fields of the region being built
// in the form, then refresh the orange preview from those fields.
function markIn() {
  $('#fIn').value = +State.master.currentTime.toFixed(3);
  syncPendingFromForm();
}
function markOut() {
  $('#fOut').value = +State.master.currentTime.toFixed(3);
  syncPendingFromForm();
}
function refreshPending() {
  const i = State.pendingIn, o = State.pendingOut;
  $('#inoutLabel').textContent = `in: ${i==null?'—':fmtTC(i)}   out: ${o==null?'—':fmtTC(o)}`;
  markTimelineDirty();
}

/* ----------------------------------------------------------------- form */
// Mirror the form's In/Out fields onto the timeline as the pending (orange)
// preview region, so a marker shows up immediately while building a new
// performance — same feedback as the Mark In / Mark Out buttons.
function syncPendingFromForm() {
  const i = parseFloat($('#fIn').value);
  const o = parseFloat($('#fOut').value);
  State.pendingIn = isFinite(i) ? i : null;
  State.pendingOut = isFinite(o) ? o : null;
  refreshPending();
}

function wireForm() {
  $('#formSave').onclick = saveForm;
  $('#formCancel').onclick = () => {
    clearForm();
    State.pendingIn = State.pendingOut = null;   // also drop the preview marker
    refreshPending();
  };
  $('#formDelete').onclick = deleteSelected;
  $('#fIn').addEventListener('input', syncPendingFromForm);
  $('#fOut').addEventListener('input', syncPendingFromForm);
  document.querySelectorAll('[data-grab]').forEach(b => {
    b.onclick = () => {
      const t = +State.master.currentTime.toFixed(3);
      if (b.dataset.grab === 'in') $('#fIn').value = t; else $('#fOut').value = t;
      syncPendingFromForm();                       // reflect the grab on the waveform
    };
  });
  $('#seedInput').onchange = () => { State.seed = Number($('#seedInput').value) || 42; markDirty(); };
}

function addPending() {
  clearForm();                          // always start a fresh entry (not an edit)
  if (State.pendingIn != null) $('#fIn').value = State.pendingIn;
  if (State.pendingOut != null) $('#fOut').value = State.pendingOut;
  $('#fTitle').focus();
}

function saveForm() {
  const tin = parseFloat($('#fIn').value);
  const tout = parseFloat($('#fOut').value);
  if (!isFinite(tin) || !isFinite(tout) || tout <= tin) {
    alert('Need valid In and Out (Out > In). Use Mark In / Mark Out or the ⟵ playhead buttons.');
    return;
  }
  const perf = {
    title: $('#fTitle').value.trim() || 'Untitled',
    composer: $('#fComposer').value.trim(),
    in: +tin.toFixed(3),
    out: +tout.toFixed(3),
  };
  pushUndo();
  if (State.selected >= 0 && State.selected < State.perfs.length) {
    State.perfs[State.selected] = { ...State.perfs[State.selected], ...perf };  // edit in place
  } else {
    State.perfs.push(perf);                                                     // add new
  }
  State.perfs.sort((a, b) => a.in - b.in);
  State.selected = -1;                       // back to "new" mode so next save adds
  markDirty();
  State.pendingIn = State.pendingOut = null;
  refreshPending();
  renderPerfs(); renderBlocks();
  clearForm();
}

function clearForm(keepSel) {
  $('#fTitle').value = ''; $('#fComposer').value = '';
  $('#fIn').value = ''; $('#fOut').value = '';
  if (!keepSel) { State.selected = -1; renderPerfs(); }
  $('#formTitle').textContent = State.selected >= 0 ? `Edit #${State.selected+1}` : 'New performance';
  $('#formDelete').hidden = State.selected < 0;
}

function selectPerf(idx, opts = {}) {
  const { seek = true } = opts;            // timeline clicks select without moving playhead
  State.selected = idx;
  const p = State.perfs[idx];
  $('#fTitle').value = p.title || '';
  $('#fComposer').value = p.composer || '';
  $('#fIn').value = p.in; $('#fOut').value = p.out;
  $('#formTitle').textContent = `Edit #${idx+1}`;
  $('#formDelete').hidden = false;
  renderPerfs(); renderBlocks();
  if (seek) { focusRange(p.in, p.out); seekAll(p.in); }
}

function deletePerf(idx) {
  if (idx < 0 || idx >= State.perfs.length) return;
  if (!confirm(`Delete "${State.perfs[idx].title}"?`)) return;
  pushUndo();
  State.perfs.splice(idx, 1);
  State.selected = -1; markDirty();
  renderPerfs(); renderBlocks(); clearForm();
}

function deleteSelected() { deletePerf(State.selected); }

function previewSelected() {
  if (State.selected < 0) return;
  const p = State.perfs[State.selected];
  seekAll(p.in);
  playAll();
}

/* ------------------------------------------------- zoomable canvas timeline */
function markTimelineDirty() { State.tlDirty = true; }
function markFullDirty() { State.tlDirty = true; State.ovDirty = true; }
function renderBlocks() { markFullDirty(); }   // kept name for existing call sites

const fmtClock = (t) => {
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = Math.floor(t % 60);
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
           : `${m}:${String(s).padStart(2,'0')}`;
};
function playTime() { return State.master ? State.master.currentTime : 0; }
function timeToX(tm) { return (tm - State.view.start) / State.view.span * State.tl.w; }
function xToTime(x)  { return State.view.start + x / State.tl.w * State.view.span; }
function clampStart(start, span) { return Math.max(0, Math.min(State.duration - span, start)); }

let pendingClickBlock = -1;

function initTimeline() {
  const wave = $('#wave'), ov = $('#overview');
  State.tl = {
    wave, wctx: wave.getContext('2d'),
    ov, octx: ov.getContext('2d'),
    static: document.createElement('canvas'),
    ovStatic: document.createElement('canvas'),
    w: 0, h: 0, ow: 0, oh: 0, dpr: 1,
  };
  resizeTimeline();
  window.addEventListener('resize', resizeTimeline);
  wireTimelineInput();
  $('#zoomIn').onclick = () => zoomAround(playTime(), 0.6);
  $('#zoomOut').onclick = () => zoomAround(playTime(), 1 / 0.6);
  $('#zoomFit').onclick = () => { State.view = { start: 0, span: State.duration }; markFullDirty(); };
}

function resizeTimeline() {
  const t = State.tl, dpr = window.devicePixelRatio || 1;
  t.dpr = dpr;
  t.w = t.wave.clientWidth; t.h = t.wave.clientHeight;
  t.ow = t.ov.clientWidth;  t.oh = t.ov.clientHeight;
  for (const [cv, w, h] of [[t.wave, t.w, t.h], [t.ov, t.ow, t.oh],
                            [t.static, t.w, t.h], [t.ovStatic, t.ow, t.oh]]) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  markFullDirty();
}

function zoomAround(focusT, factor) {
  const v = State.view;
  const newSpan = Math.max(MIN_SPAN, Math.min(State.duration, v.span * factor));
  const frac = (focusT - v.start) / v.span;
  v.start = clampStart(focusT - frac * newSpan, newSpan);
  v.span = newSpan;
  markTimelineDirty();
}

// Pan the visible window by a horizontal pixel delta (positive = scroll right).
function panBy(dxPixels) {
  const v = State.view;
  v.start = clampStart(v.start + dxPixels / State.tl.w * v.span, v.span);
  markTimelineDirty();
}

function focusRange(a, b) {
  const v = State.view, len = b - a;
  if (a >= v.start && b <= v.start + v.span) return;
  if (len > v.span * 0.9) v.span = Math.min(State.duration, len * 1.3);
  v.start = clampStart(a - (v.span - len) / 2, v.span);
  markTimelineDirty();
}

function updateZoomLabel() {
  const v = State.view;
  $('#zoomLabel').textContent =
    `view ${fmtClock(v.start)}–${fmtClock(v.start + v.span)} · ` +
    (v.span < 60 ? v.span.toFixed(1) + 's' : fmtDur(v.span));
}

function tickStep(span, w) {
  const want = span / (w / 90);
  for (const s of [0.5,1,2,5,10,15,30,60,120,300,600,900,1800]) if (s >= want) return s;
  return 3600;
}

/* max waveform peak in [t0,t1] -> 0..1 */
function peakAt(t0, t1) {
  const w = State.wave;
  if (!w.ready) return 0;
  let i0 = Math.max(0, Math.floor(t0 * w.pps));
  let i1 = Math.min(w.peaks.length, Math.max(Math.ceil(t1 * w.pps), i0 + 1));
  let mx = 0;
  for (let i = i0; i < i1; i++) if (w.peaks[i] > mx) mx = w.peaks[i];
  return mx / 255;
}

function drawWaveformInto(ctx, w, h, vStart, vSpan, color) {
  const mid = h / 2, secPerPx = vSpan / w;
  ctx.fillStyle = color;
  ctx.beginPath();
  for (let x = 0; x < w; x++) {
    const t0 = vStart + x * secPerPx;
    const amp = Math.pow(peakAt(t0, t0 + secPerPx), 0.7);
    const hh = Math.max(0.5, amp * (mid - 1));
    ctx.rect(x, mid - hh, 1, hh * 2);
  }
  ctx.fill();
}

function rebuildOverviewStatic() {
  const t = State.tl, ctx = t.ovStatic.getContext('2d');
  ctx.setTransform(t.dpr, 0, 0, t.dpr, 0, 0);
  ctx.clearRect(0, 0, t.ow, t.oh);
  if (State.wave.ready) drawWaveformInto(ctx, t.ow, t.oh, 0, State.duration, 'rgba(120,170,255,0.30)');
  State.perfs.forEach((p, i) => {
    const x0 = p.in / State.duration * t.ow, x1 = p.out / State.duration * t.ow;
    ctx.fillStyle = i === State.selected ? 'rgba(52,211,153,0.55)' : 'rgba(79,140,255,0.45)';
    ctx.fillRect(x0, 0, Math.max(1, x1 - x0), t.oh);
  });
}

function rebuildStatic() {
  const t = State.tl, v = State.view, ctx = t.static.getContext('2d');
  ctx.setTransform(t.dpr, 0, 0, t.dpr, 0, 0);
  ctx.clearRect(0, 0, t.w, t.h);
  ctx.font = '10px -apple-system, sans-serif';

  if (State.wave.ready) drawWaveformInto(ctx, t.w, t.h, v.start, v.span, 'rgba(120,170,255,0.55)');

  // ticks + gridlines
  const step = tickStep(v.span, t.w);
  const first = Math.ceil(v.start / step) * step;
  ctx.lineWidth = 1;
  for (let tm = first; tm <= v.start + v.span + 1e-6; tm += step) {
    const x = Math.round(timeToX(tm)) + 0.5;
    ctx.strokeStyle = 'rgba(255,255,255,0.07)';
    ctx.beginPath(); ctx.moveTo(x, 12); ctx.lineTo(x, t.h); ctx.stroke();
    ctx.fillStyle = 'rgba(160,170,190,0.85)';
    ctx.fillText(step < 1 ? tm.toFixed(1) + 's' : fmtClock(tm), x + 3, 10);
  }

  // performance blocks
  State.perfs.forEach((p, i) => {
    const x0 = timeToX(p.in), x1 = timeToX(p.out);
    if (x1 < 0 || x0 > t.w) return;
    const sel = i === State.selected;
    ctx.fillStyle = sel ? 'rgba(52,211,153,0.20)' : 'rgba(79,140,255,0.16)';
    ctx.fillRect(x0, 14, x1 - x0, t.h - 14);
    ctx.strokeStyle = sel ? '#34d399' : '#4f8cff';
    ctx.lineWidth = sel ? 2 : 1;
    ctx.strokeRect(x0 + 0.5, 14.5, x1 - x0 - 1, t.h - 15);
    // in/out drag handles (grips on each edge)
    const hw = 3, grip = sel ? '#34d399' : '#7aa6ff';
    ctx.fillStyle = grip;
    ctx.fillRect(x0, 14, hw, t.h - 14);
    ctx.fillRect(x1 - hw, 14, hw, t.h - 14);
    ctx.strokeStyle = 'rgba(255,255,255,0.55)'; ctx.lineWidth = 1;
    for (const hx of [x0 + hw / 2, x1 - hw / 2]) {
      ctx.beginPath(); ctx.moveTo(hx + 0.5, 18); ctx.lineTo(hx + 0.5, t.h - 4); ctx.stroke();
    }
    ctx.fillStyle = sel ? '#d6ffe9' : '#cfe0ff';
    ctx.save();
    ctx.beginPath(); ctx.rect(x0 + hw + 2, 14, Math.max(0, x1 - x0 - 2 * hw - 4), t.h - 14); ctx.clip();
    ctx.fillText(`${i + 1}. ${p.title}`, x0 + hw + 4, 26);
    ctx.restore();
  });

  // pending in/out
  const pi = State.pendingIn, po = State.pendingOut;
  if (pi != null && po != null && po > pi) {
    ctx.fillStyle = 'rgba(245,158,11,0.16)';
    ctx.fillRect(timeToX(pi), 14, timeToX(po) - timeToX(pi), t.h - 14);
  }
  ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 1.5;
  for (const m of [pi, po]) if (m != null) {
    const x = timeToX(m);
    ctx.beginPath(); ctx.moveTo(x, 12); ctx.lineTo(x, t.h); ctx.stroke();
  }
  updateZoomLabel();
}

function drawTimeline(playT) {
  const t = State.tl;
  if (!t.wctx) return;
  if (State.ovDirty) { rebuildOverviewStatic(); State.ovDirty = false; }
  if (State.tlDirty) { rebuildStatic(); State.tlDirty = false; }

  const ctx = t.wctx;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, t.wave.width, t.wave.height);
  ctx.drawImage(t.static, 0, 0);
  ctx.setTransform(t.dpr, 0, 0, t.dpr, 0, 0);
  const px = timeToX(playT);
  if (px >= 0 && px <= t.w) {
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, t.h); ctx.stroke();
  }

  const o = t.octx;
  o.setTransform(1, 0, 0, 1, 0, 0);
  o.clearRect(0, 0, t.ov.width, t.ov.height);
  o.drawImage(t.ovStatic, 0, 0);
  o.setTransform(t.dpr, 0, 0, t.dpr, 0, 0);
  const vx0 = State.view.start / State.duration * t.ow;
  const vx1 = (State.view.start + State.view.span) / State.duration * t.ow;
  o.fillStyle = 'rgba(255,255,255,0.10)';
  o.fillRect(vx0, 0, vx1 - vx0, t.oh);
  o.strokeStyle = '#fff'; o.lineWidth = 1;
  o.strokeRect(vx0 + 0.5, 0.5, Math.max(1, vx1 - vx0 - 1), t.oh - 1);
  const opx = playT / State.duration * t.ow;
  o.strokeStyle = 'rgba(255,255,255,0.55)';
  o.beginPath(); o.moveTo(opx, 0); o.lineTo(opx, t.oh); o.stroke();
}

function blockHit(x, y) {
  if (y < 14) return -1;
  for (let i = State.perfs.length - 1; i >= 0; i--) {
    const p = State.perfs[i];
    if (x >= timeToX(p.in) && x <= timeToX(p.out)) return i;
  }
  return -1;
}

const HANDLE_TOL = 6;   // px tolerance for grabbing a region edge
const MIN_LEN = 0.1;    // shortest allowed region (seconds)

// Is the cursor over an in/out drag handle? Returns { i, edge } or null.
function handleHit(x, y) {
  if (y < 14) return null;
  for (let i = State.perfs.length - 1; i >= 0; i--) {
    const p = State.perfs[i];
    if (Math.abs(x - timeToX(p.in))  <= HANDLE_TOL) return { i, edge: 'in' };
    if (Math.abs(x - timeToX(p.out)) <= HANDLE_TOL) return { i, edge: 'out' };
  }
  return null;
}

// Live-adjust a region edge while dragging its handle.
function applyHandleDrag(drag, tm) {
  const p = State.perfs[drag.i];
  tm = Math.max(0, Math.min(State.duration, +tm.toFixed(3)));
  if (drag.edge === 'in') p.in = Math.min(tm, p.out - MIN_LEN);
  else                    p.out = Math.max(tm, p.in + MIN_LEN);
  seekAll(drag.edge === 'in' ? p.in : p.out);   // playhead follows the edge
  if (State.selected === drag.i) { $('#fIn').value = p.in; $('#fOut').value = p.out; }
  renderPerfs(); markFullDirty();
}

function wireTimelineInput() {
  const t = State.tl;
  t.wave.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = t.wave.getBoundingClientRect();
    // Pinch-to-zoom (trackpad sends ctrlKey) or a vertical wheel -> zoom.
    // A horizontal two-finger swipe (deltaX dominant) -> pan the timeline.
    if (!e.ctrlKey && Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      panBy(e.deltaX);
    } else {
      zoomAround(xToTime(e.clientX - r.left), e.deltaY < 0 ? 0.85 : 1 / 0.85);
    }
  }, { passive: false });

  let scrubbing = false, downX = 0, moved = false;
  let drag = null, dragSnapped = false;             // { i, edge } while resizing a region
  const localX = (e) => e.clientX - t.wave.getBoundingClientRect().left;

  t.wave.addEventListener('mousedown', (e) => {
    const r = t.wave.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    downX = x; moved = false;
    const h = handleHit(x, y);
    if (h) {                                        // grab an edge -> resize, don't scrub
      drag = h; dragSnapped = false;
      selectPerf(h.i, { seek: false });
      return;
    }
    pendingClickBlock = blockHit(x, y);
    scrubbing = true;
    seekAll(xToTime(x));
  });
  window.addEventListener('mousemove', (e) => {
    if (drag) {
      if (!dragSnapped) { pushUndo(); dragSnapped = true; }   // snapshot on first move
      applyHandleDrag(drag, xToTime(localX(e)));
      return;
    }
    if (!scrubbing) return;
    const x = localX(e);
    if (Math.abs(x - downX) > 3) { moved = true; pendingClickBlock = -1; }
    seekAll(xToTime(x));
  });
  window.addEventListener('mouseup', () => {
    if (drag) {                                     // finalize resize, keep selection, autosave
      if (dragSnapped) {                            // only if it actually moved
        const p = State.perfs[drag.i];
        State.perfs.sort((a, b) => a.in - b.in);
        State.selected = State.perfs.indexOf(p);
        markDirty();
        selectPerf(State.selected, { seek: false });
      }
      drag = null;
      return;
    }
    if (scrubbing && !moved && pendingClickBlock >= 0) selectPerf(pendingClickBlock, { seek: false });
    scrubbing = false; pendingClickBlock = -1;
  });
  // cursor hint when hovering an edge handle
  t.wave.addEventListener('mousemove', (e) => {
    if (drag || scrubbing) return;
    const r = t.wave.getBoundingClientRect();
    t.wave.style.cursor = handleHit(e.clientX - r.left, e.clientY - r.top) ? 'ew-resize' : 'default';
  });

  let panning = false;
  const panTo = (e) => {
    const r = t.ov.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    State.view.start = clampStart(frac * State.duration - State.view.span / 2, State.view.span);
    markTimelineDirty();
  };
  t.ov.addEventListener('mousedown', (e) => { panning = true; panTo(e); });
  window.addEventListener('mousemove', (e) => { if (panning) panTo(e); });
  window.addEventListener('mouseup', () => { panning = false; });
}

function renderPerfs() {
  const ol = $('#perfList'); ol.innerHTML = '';
  $('#perfCount').textContent = State.perfs.length;
  State.perfs.forEach((p, i) => {
    const li = document.createElement('li');
    li.className = i === State.selected ? 'sel' : '';
    li.innerHTML = `
      <span class="num">${i+1}</span>
      <span class="meta"><b>${escapeHtml(p.title)}</b>
        <small>${escapeHtml(p.composer||'—')} · ${fmtTC(p.in)} → ${fmtTC(p.out)}</small></span>
      <span class="dur">${fmtDur(p.out - p.in)}</span>
      <span class="rowbtns">
        <button class="small" data-act="edit" title="Edit in form">Edit</button>
        <button class="small danger" data-act="del" title="Delete">✕</button>
      </span>`;
    li.onclick = () => selectPerf(i);
    li.querySelector('[data-act=edit]').onclick = (e) => {
      e.stopPropagation(); selectPerf(i); $('#fTitle').focus();
    };
    li.querySelector('[data-act=del]').onclick = (e) => {
      e.stopPropagation(); deletePerf(i);
    };
    ol.appendChild(li);
  });
}

/* ----------------------------------------------------- caption transcript */
// The transcript is produced by tools/transcribe.py in 10 chunk passes and
// re-merged after each one, so cache/transcript.json grows while the editor is
// open. We poll until the segment count stops changing (transcription done).
let _trPollStable = 0, _trLastCount = -1;

async function loadTranscript() {
  const data = await fetch('/api/transcript').then(r => r.json()).catch(() => ({ ready: false }));
  const segs = data.segments || [];
  const status = $('#transcriptStatus');

  if (segs.length !== State.transcript.segments.length) {
    State.transcript.segments = segs;
    State.transcript.ready = !!data.ready;
    State.transcript.activeIdx = -1;
    renderTranscript();
  }

  const done = data.ready && segs.length === _trLastCount;
  _trPollStable = done ? _trPollStable + 1 : 0;
  _trLastCount = segs.length;

  if (!data.ready) {
    status.textContent = '⏳ transcribing…';
  } else if (_trPollStable < 3) {
    status.textContent = `${segs.length} lines · transcribing…`;
  } else {
    status.textContent = `${segs.length} lines`;
    return;                                 // stable -> stop polling
  }
  setTimeout(loadTranscript, 6000);
}

function renderTranscript() {
  const box = $('#transcript');
  const segs = State.transcript.segments;
  if (!segs.length) {
    box.innerHTML = '<div class="trempty">No transcript yet — generating from the Back Camera audio…</div>';
    return;
  }
  const q = ($('#trSearch').value || '').trim().toLowerCase();
  box.innerHTML = '';
  segs.forEach((s, i) => {
    if (q && !s.text.toLowerCase().includes(q)) return;
    const line = document.createElement('div');
    line.className = 'trline';
    line.dataset.i = i;
    line.innerHTML =
      `<span class="trtc">${fmtClock(s.start)}</span>` +
      `<span class="trtext">${escapeHtml(s.text)}</span>`;
    line.onclick = () => { seekAll(s.start); focusRange(s.start, s.end); };
    box.appendChild(line);
  });
  State.transcript.activeIdx = -1;          // force re-highlight next tick
}

// Binary-search the segment covering time t (segments are sorted by start).
function transcriptIndexAt(t) {
  const segs = State.transcript.segments;
  let lo = 0, hi = segs.length - 1, hit = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (segs[mid].start <= t) { hit = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  if (hit >= 0 && t <= segs[hit].end + 0.25) return hit;   // small tail grace
  return -1;
}

function updateTranscriptHighlight(t) {
  const tr = State.transcript;
  if (!tr.segments.length) return;
  const idx = transcriptIndexAt(t);
  if (idx === tr.activeIdx) return;
  const box = $('#transcript');
  const prev = box.querySelector('.trline.active');
  if (prev) prev.classList.remove('active');
  tr.activeIdx = idx;
  if (idx < 0) return;
  const el = box.querySelector(`.trline[data-i="${idx}"]`);
  if (!el) return;                          // filtered out by search
  el.classList.add('active');
  if ($('#trFollow').checked) {
    el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

function wireTranscript() {
  $('#trSearch').addEventListener('input', renderTranscript);
  const wrap = $('#transcriptWrap'), btn = $('#trToggle');
  const setCollapsed = (on) => {
    wrap.classList.toggle('collapsed', on);
    btn.title = on ? 'Show transcript' : 'Hide transcript';
    try { localStorage.setItem('transcriptCollapsed', on ? '1' : '0'); } catch (e) {}
  };
  btn.onclick = () => setCollapsed(!wrap.classList.contains('collapsed'));
  let saved = '0';
  try { saved = localStorage.getItem('transcriptCollapsed') || '0'; } catch (e) {}
  setCollapsed(saved === '1');
}

/* ----------------------------------------------------------------- undo */
// Snapshot the editable state (performances + seed) BEFORE a mutation so it
// can be restored. Call pushUndo() at the start of any edit.
function pushUndo() {
  State.undoStack.push({
    perfs: State.perfs.map(p => ({ ...p })),
    seed: State.seed,
  });
  if (State.undoStack.length > 100) State.undoStack.shift();
  updateUndoBtn();
}

function undo() {
  const snap = State.undoStack.pop();
  if (!snap) return;
  State.perfs = snap.perfs.map(p => ({ ...p }));
  State.seed = snap.seed;
  $('#seedInput').value = snap.seed;
  State.selected = -1;
  clearForm();
  State.pendingIn = State.pendingOut = null;
  refreshPending();
  renderPerfs(); renderBlocks();
  markDirty();                 // persist the reverted state
  updateUndoBtn();
}

function updateUndoBtn() {
  const b = $('#undoBtn');
  if (b) b.disabled = State.undoStack.length === 0;
}

/* ----------------------------------------------------------------- save */
// Autosave: any change to the performance list / settings marks State dirty
// and schedules a debounced background save to the SQLite store. The "Save
// markers" button still forces an immediate save.
let _saveTimer = null;
let _saveInFlight = false;

function markDirty() {
  State.dirty = true;
  scheduleAutosave();
}

function scheduleAutosave(delay = 800) {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => { save({ auto: true }); }, delay);
}

async function save({ auto = false } = {}) {
  clearTimeout(_saveTimer);                 // cancel any pending debounce
  if (_saveInFlight) {                       // a save is already running
    if (State.dirty) scheduleAutosave(300);  // retry once it lands
    return;
  }
  const s = $('#status');
  const payload = {
    seed: Number($('#seedInput').value) || 42,
    project: 'Belgium Concert Highlights',
    fps: State.fps,
    duration: State.duration,
    audio_source: $('#audioSource').value,
    performances: State.perfs.map(p => ({
      title: p.title, composer: p.composer, in: p.in, out: p.out,
    })),
  };
  _saveInFlight = true;
  if (!auto) { s.textContent = 'saving…'; }
  let res;
  try {
    res = await fetch('/api/markers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(r => r.json());
  } catch (err) {
    res = { ok: false, error: String(err) };
  } finally {
    _saveInFlight = false;
  }
  if (res.ok) {
    State.dirty = false;
    s.textContent = `✓ saved ${State.perfs.length} performances`;
    s.classList.add('flash'); setTimeout(() => s.classList.remove('flash'), 600);
  } else if (auto) {
    s.textContent = `⚠ autosave failed (${res.error || 'unknown'}) — will retry`;
    scheduleAutosave(3000);                  // keep trying in the background
  } else {
    alert('Save failed: ' + (res.error || 'unknown'));
  }
}

/* ----------------------------------------------------------------- keys */
function wireKeys() {
  window.addEventListener('keydown', (e) => {
    // Undo works globally (incl. while typing), unless an input has a text
    // selection/native-undo the user is actively editing.
    if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z') && !e.shiftKey) {
      const inField = ['INPUT', 'TEXTAREA'].includes(e.target.tagName);
      if (!inField) { e.preventDefault(); undo(); return; }
    }
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) {
      if (e.key === 'Enter' && e.target.id !== 'fTitle' && e.target.id !== 'fComposer') {} else return;
    }
    switch (e.key) {
      case ' ': e.preventDefault(); togglePlay(); break;
      case 'ArrowLeft': e.preventDefault();
        pauseAll(); seekAll(State.master.currentTime - (e.shiftKey ? 5 : 1/State.fps)); break;
      case 'ArrowRight': e.preventDefault();
        pauseAll(); seekAll(State.master.currentTime + (e.shiftKey ? 5 : 1/State.fps)); break;
      case 'i': case 'I': markIn(); break;
      case 'o': case 'O': markOut(); break;
      case 'Enter': addPending(); break;
      case 'p': case 'P': previewSelected(); break;
      case '=': case '+': e.preventDefault(); zoomAround(playTime(), 0.6); break;
      case '-': case '_': e.preventDefault(); zoomAround(playTime(), 1 / 0.6); break;
      case '0': e.preventDefault(); State.view = { start: 0, span: State.duration }; markFullDirty(); break;
    }
  });
}

function escapeHtml(s) {
  return (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

window.addEventListener('beforeunload', (e) => {
  if (!State.dirty) return;
  // Flush the latest state past the debounce window. sendBeacon survives the
  // page teardown that a normal fetch would not.
  const payload = {
    seed: Number($('#seedInput').value) || 42,
    project: 'Belgium Concert Highlights',
    fps: State.fps,
    duration: State.duration,
    audio_source: $('#audioSource').value,
    performances: State.perfs.map(p => ({
      title: p.title, composer: p.composer, in: p.in, out: p.out,
    })),
  };
  navigator.sendBeacon('/api/markers', new Blob([JSON.stringify(payload)], { type: 'application/json' }));
});

boot();
