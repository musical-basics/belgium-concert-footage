/* Reels production interface.
 *
 * One COMPOUND clip: an ordered list of concert-time segments played
 * back-to-back. Every edit (split at playhead, edge trim, delete) hits all
 * three stacked cameras and the audio together, and the reel ripples closed —
 * there are no gaps in output time.
 *
 * Geometry: #stage is a real 1080x1920 div scaled down with a CSS transform,
 * so all pane math happens in OUTPUT pixels. The per-camera framing
 * (zoom/x/y) uses the exact same cover-fit + pan-crop formula as
 * render/style_reels.py, so the preview is the export.
 *
 * Persistence: the whole document (segments + cams + layout) lives in
 * reels.json via GET/POST /api/reels (debounced autosave).
 */
'use strict';

const OUT_W = 1080, OUT_H = 1920;
const SRC_W = 1920, SRC_H = 1080;          // all three stationary cameras
const MIN_SEG = 0.1;                        // shortest segment (s)
const EDGE_PX = 6;                          // trim-handle hit zone

const State = {
  fps: 60,
  duration: 0,                              // concert length (s)
  doc: null,                                // {segments, cams, layout, ...}
  clips: [],                                // /api/meta cameras used here
  videos: {},                               // cam id -> <video>
  panes: {},                                // cam id -> .pane element
  master: null,                             // back camera <video> (audio bed)
  stageK: 1,
  ph: { idx: 0, srcT: 0 },                  // playhead: segment + concert time
  playing: false,
  selected: -1,
  selMarker: -1,
  view: { start: 0, span: 60 },             // timeline window (output time)
  wave: { ready: false, peaks: null, pps: 100 },
  undoStack: [],
  saveTimer: null,
  exportTimer: null,
  color: null,                              // shared camera grades (loadGrades)
  projects: [],                             // reel project metas {id,name,...}
  projectId: null,                          // the open (active) project
};

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const api = (url, opts) => fetch(url, Object.assign({ credentials: 'same-origin' }, opts));

/* ---------- time helpers ---------- */
function segs() { return State.doc.segments; }
function outDur() { return segs().reduce((a, s) => a + (s.out - s.in), 0); }
function outBase(idx) {
  let a = 0;
  for (let i = 0; i < idx; i++) a += segs()[i].out - segs()[i].in;
  return a;
}
function phOut() {
  const s = segs()[State.ph.idx];
  if (!s) return 0;
  return outBase(State.ph.idx) + clamp(State.ph.srcT - s.in, 0, s.out - s.in);
}
function outToSrc(T) {
  T = clamp(T, 0, Math.max(0, outDur() - 1e-4));
  let a = 0;
  for (let i = 0; i < segs().length; i++) {
    const d = segs()[i].out - segs()[i].in;
    if (T < a + d || i === segs().length - 1)
      return { idx: i, t: segs()[i].in + clamp(T - a, 0, d) };
    a += d;
  }
  return { idx: 0, t: segs()[0] ? segs()[0].in : 0 };
}

function fmtOut(t) {
  const ms = Math.round((t % 1) * 1000);
  const s = Math.floor(t % 60), m = Math.floor(t / 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(ms).padStart(3, '0')}`;
}
function fmtSrc(t) {
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60);
  const s = (t % 60).toFixed(2).padStart(5, '0');
  return `${h}:${String(m).padStart(2, '0')}:${s}`;
}

/* ---------- boot ---------- */
async function boot() {
  const [meta, reels] = await Promise.all([
    api('/api/meta').then(r => r.json()),
    api('/api/reels').then(r => r.json()),
  ]);
  State.fps = meta.fps || 60;
  State.duration = meta.duration || 0;
  State.projects = reels.projects || [];
  State.projectId = reels.active;
  State.doc = reels.doc;
  State.doc.markers = State.doc.markers || [];
  const byId = {};
  for (const c of meta.clips) byId[c.id] = c;
  State.clips = State.doc.layout.map(id => byId[id]).filter(Boolean);

  buildStage();
  buildCamRows();
  buildProjectBar();
  loadGrades();
  layoutStage();
  setupTimeline();
  fitView();
  loadWaveform();
  bindTransport();
  bindKeys();
  bindExport();
  seekOut(0);
  updateStatus();
  requestAnimationFrame(tick);
  window.addEventListener('resize', () => { layoutStage(); resizeTl(); drawTl(); });
}

function updateStatus() {
  const n = segs().length;
  $('status').textContent =
    `${State.clips.map(c => c.label).join(' / ')} · concert ${fmtSrc(State.duration)}`;
  $('reelMeta').textContent = `${fmtOut(outDur())} · ${n} segment${n === 1 ? '' : 's'}`;
  $('deleteBtn').disabled = !(State.selected >= 0 && n > 1);
  const mk = State.doc.markers[State.selMarker];
  $('deleteMarkerBtn').hidden = !mk;
  const sel = segs()[State.selected];
  $('segInfo').innerHTML = mk
    ? `Marker <b>${State.selMarker + 1}</b> ⚑ concert <b>${fmtSrc(mk.t)}</b>` +
      (mk.label ? ` · ${mk.label}` : '') + ' · <b>⌫</b> deletes'
    : sel
    ? `Segment <b>${State.selected + 1}/${n}</b> · concert <b>${fmtSrc(sel.in)}</b> → ` +
      `<b>${fmtSrc(sel.out)}</b> · <b>${(sel.out - sel.in).toFixed(2)}s</b>`
    : 'Click a segment on the timeline to select it.';
  $('undoBtn').disabled = !State.undoStack.length;
}

/* ---------- stage: stacked panes + XY framing ---------- */
function buildStage() {
  const stage = $('stage');
  stage.innerHTML = '';
  const n = State.clips.length;
  const paneH = Math.floor(OUT_H / n);
  State.clips.forEach((c, i) => {
    const pane = document.createElement('div');
    pane.className = 'pane';
    pane.dataset.cam = c.id;
    pane.style.height = (i === n - 1 ? OUT_H - paneH * (n - 1) : paneH) + 'px';
    const v = document.createElement('video');
    v.src = c.proxy_url || ('/proxies/' + c.proxy);
    v.preload = 'auto';
    v.muted = !c.is_audio;
    v.playsInline = true;
    pane.appendChild(v);
    const label = document.createElement('div');
    label.className = 'pane-label';
    pane.appendChild(label);
    stage.appendChild(pane);
    State.videos[c.id] = v;
    State.panes[c.id] = pane;
    if (c.is_audio) State.master = v;
    bindPane(pane, c.id);
    applyCam(c.id);
  });
  if (!State.master) State.master = State.videos[State.clips[0].id];
}

function paneSize(cam) {
  const pane = State.panes[cam];
  return { w: OUT_W, h: pane.clientHeight || Math.floor(OUT_H / State.clips.length) };
}

/* The one framing formula (mirrored by style_reels.py's cam_chain). */
function applyCam(cam) {
  const t = State.doc.cams[cam] || (State.doc.cams[cam] = { scale: 1, x: 0, y: 0 });
  const { w: pw, h: ph } = paneSize(cam);
  const cover = Math.max(pw / SRC_W, ph / SRC_H);
  t.scale = clamp(t.scale, 1, 4);
  const s = cover * t.scale;
  const w = SRC_W * s, h = SRC_H * s;
  const maxX = (w - pw) / 2, maxY = (h - ph) / 2;
  t.x = clamp(t.x, -maxX, maxX);
  t.y = clamp(t.y, -maxY, maxY);
  const cx = (w - pw) / 2 - t.x, cy = (h - ph) / 2 - t.y;
  const v = State.videos[cam];
  v.style.width = w + 'px';
  v.style.height = h + 'px';
  v.style.left = -cx + 'px';
  v.style.top = -cy + 'px';
  const clip = State.clips.find(c => c.id === cam);
  State.panes[cam].querySelector('.pane-label').textContent =
    `${clip.label} · ${Math.round(t.scale * 100)}%` +
    (t.x || t.y ? ` · ${Math.round(t.x)},${Math.round(t.y)}` : '');
  syncCamRow(cam);
}

function bindPane(pane, cam) {
  pane.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    pane.setPointerCapture(e.pointerId);
    pane.classList.add('dragging');
    let lastX = e.clientX, lastY = e.clientY;
    const move = (ev) => {
      const t = State.doc.cams[cam];
      t.x += (ev.clientX - lastX) / State.stageK;
      t.y += (ev.clientY - lastY) / State.stageK;
      lastX = ev.clientX; lastY = ev.clientY;
      applyCam(cam);
      scheduleSave();
    };
    const up = () => {
      pane.classList.remove('dragging');
      pane.removeEventListener('pointermove', move);
      pane.removeEventListener('pointerup', up);
    };
    pushUndoOnce('pane-' + cam);
    pane.addEventListener('pointermove', move);
    pane.addEventListener('pointerup', up);
  });
  pane.addEventListener('wheel', (e) => {
    e.preventDefault();
    pushUndoOnce('zoom-' + cam);
    const t = State.doc.cams[cam];
    t.scale = clamp(t.scale * Math.exp(-e.deltaY * 0.0018), 1, 4);
    applyCam(cam);
    scheduleSave();
  }, { passive: false });
  pane.addEventListener('dblclick', () => {
    pushUndo();
    State.doc.cams[cam] = { scale: 1, x: 0, y: 0 };
    applyCam(cam);
    scheduleSave();
  });
}

function layoutStage() {
  const main = $('reelsMain');
  const availH = main.clientHeight - 20;
  const availW = Math.max(240, main.clientWidth * 0.44);
  State.stageK = Math.min(availH / OUT_H, availW / OUT_W);
  const stage = $('stage');
  stage.style.transform = `scale(${State.stageK})`;
  const wrap = $('stageWrap');
  wrap.style.width = OUT_W * State.stageK + 'px';
  wrap.style.height = OUT_H * State.stageK + 'px';
}

/* ---------- camera framing rows ---------- */
function buildCamRows() {
  const box = $('camRows');
  box.innerHTML = '';
  const posName = (i, n) => n === 3 ? ['top', 'middle', 'bottom'][i] : `#${i + 1}`;
  State.clips.forEach((c, ci) => {
    const row = document.createElement('div');
    row.className = 'camrow';
    row.dataset.cam = c.id;
    row.innerHTML =
      `<div class="cr-head">` +
      `<span class="cr-pos">${posName(ci, State.clips.length)}</span>` +
      `<span class="cr-name">${c.label}` +
      `${c.is_audio ? ' <span style="color:var(--muted);font-weight:400">· audio</span>' : ''}</span>` +
      `<span class="cr-vals">—</span><div class="spacer"></div>` +
      `<button class="small cr-up" title="Move this camera up in the stack" ${ci === 0 ? 'disabled' : ''}>▲</button>` +
      `<button class="small cr-down" title="Move this camera down in the stack" ${ci === State.clips.length - 1 ? 'disabled' : ''}>▼</button>` +
      `<button class="small cr-reset" title="Reset this camera's framing">↺</button></div>` +
      `<div class="cr-slider"><span class="klbl">zoom</span>` +
      `<input data-k="scale" type="range" min="100" max="400" step="1" value="100" />` +
      `<span class="kval k-scale">100%</span></div>` +
      `<div class="cr-slider"><span class="klbl">x</span>` +
      `<input data-k="x" type="range" min="0" max="0" step="1" value="0" />` +
      `<span class="kval k-x">0</span></div>` +
      `<div class="cr-slider"><span class="klbl">y</span>` +
      `<input data-k="y" type="range" min="0" max="0" step="1" value="0" />` +
      `<span class="kval k-y">0</span></div>`;
    box.appendChild(row);
    row.querySelectorAll('input[type=range]').forEach((inp) => {
      inp.addEventListener('input', () => {
        const t = State.doc.cams[c.id];
        pushUndoOnce('fr-' + inp.dataset.k + '-' + c.id);
        if (inp.dataset.k === 'scale') t.scale = Number(inp.value) / 100;
        else t[inp.dataset.k] = Number(inp.value);
        applyCam(c.id);
        scheduleSave();
      });
    });
    row.querySelector('.cr-up').addEventListener('click', () => moveCam(c.id, -1));
    row.querySelector('.cr-down').addEventListener('click', () => moveCam(c.id, 1));
    row.querySelector('.cr-reset').addEventListener('click', () => {
      pushUndo();
      State.doc.cams[c.id] = { scale: 1, x: 0, y: 0 };
      applyCam(c.id);
      scheduleSave();
    });
    syncCamRow(c.id);
  });
  // onclick (not addEventListener) so rebuilds after a reorder stay idempotent
  $('camResetAll').onclick = () => {
    pushUndo();
    for (const c of State.clips) {
      State.doc.cams[c.id] = { scale: 1, x: 0, y: 0 };
      applyCam(c.id);
    }
    scheduleSave();
  };
}

function syncCamRow(cam) {
  const row = document.querySelector(`.camrow[data-cam="${cam}"]`);
  if (!row) return;
  const t = State.doc.cams[cam];
  const { w: pw, h: ph } = paneSize(cam);
  const cover = Math.max(pw / SRC_W, ph / SRC_H);
  const s = cover * t.scale;
  // pan travel available at the current zoom — the same clamp applyCam enforces
  const maxX = Math.floor((SRC_W * s - pw) / 2);
  const maxY = Math.floor((SRC_H * s - ph) / 2);
  const set = (k, min, max, val, txt) => {
    const inp = row.querySelector(`input[data-k="${k}"]`);
    inp.min = min; inp.max = max; inp.value = val;
    inp.disabled = min >= max;
    row.querySelector('.k-' + k).textContent = txt;
  };
  set('scale', 100, 400, Math.round(t.scale * 100), Math.round(t.scale * 100) + '%');
  set('x', -maxX, maxX, Math.round(t.x), `${Math.round(t.x)} px`);
  set('y', -maxY, maxY, Math.round(t.y), `${Math.round(t.y)} px`);
  row.querySelector('.cr-vals').textContent =
    `x ${Math.round(t.x)} · y ${Math.round(t.y)}`;
}

/* ---------- camera stack order ---------- */
function moveCam(camId, dir) {
  const L = State.doc.layout;
  const i = L.indexOf(camId), j = i + dir;
  if (i < 0 || j < 0 || j >= L.length) return;
  pushUndo();
  [L[i], L[j]] = [L[j], L[i]];
  applyLayoutOrder();
  scheduleSave();
}

/* Re-stack the existing panes to match doc.layout (no video reload) and
   rebuild the control rows in the new order. */
function applyLayoutOrder() {
  State.clips = State.doc.layout.map(id => State.clips.find(c => c.id === id))
    .filter(Boolean);
  const stage = $('stage');
  const n = State.clips.length;
  const paneH = Math.floor(OUT_H / n);
  State.clips.forEach((c, i) => {
    const pane = State.panes[c.id];
    pane.style.height = (i === n - 1 ? OUT_H - paneH * (n - 1) : paneH) + 'px';
    stage.appendChild(pane);      // append in order = final stack order
    applyCam(c.id);
  });
  buildCamRows();
  buildColorRows();
  updateStatus();
}

/* ---------- camera color grades (shared with the main editor) ----------
 * Same /api/camera-grades document the marker editor edits: one per-camera
 * {brightness, gamma, contrast, saturation} used by BOTH renders. Live pane
 * preview replicates render.py's ffmpeg `eq` with an SVG lookup-table filter
 * on the LIMITED-RANGE luma bytes (16..235) — the exact math app.js uses, so
 * the preview matches the export. Explicit Save (grades are project-wide).
 */
const _svgNS = 'http://www.w3.org/2000/svg';
const _gradeFilterIds = {};
let _gradeFilterSeq = 0;

function _gradeNeutral(g) {
  return Math.abs((+g.brightness || 0)) < 1e-6 &&
         Math.abs((+g.gamma || 1) - 1) < 1e-6 &&
         Math.abs((+g.contrast || 1) - 1) < 1e-6 &&
         Math.abs((+g.saturation || 1) - 1) < 1e-6;
}

function ensureGradeFilter(g) {
  if (!g || _gradeNeutral(g)) return '';
  const key = `${+g.brightness || 0}|${+g.gamma || 1}|${+g.contrast || 1}|${+g.saturation || 1}`;
  if (_gradeFilterIds[key]) return _gradeFilterIds[key];
  const b = +g.brightness || 0, gamma = +g.gamma || 1;
  const contrast = +g.contrast || 1, sat = +g.saturation || 1;
  const svg = document.getElementById('gradeFilters');
  const id = `grade${_gradeFilterSeq++}`;
  const filter = document.createElementNS(_svgNS, 'filter');
  filter.setAttribute('id', id);
  filter.setAttribute('color-interpolation-filters', 'sRGB');
  // ffmpeg eq curve (contrast -> brightness -> gamma) applied to the video's
  // limited-range bytes, sampled into one LUT — see app.js for the derivation.
  const N = 65;
  const table = [];
  for (let k = 0; k < N; k++) {
    const d = k / (N - 1);
    const v = (16 + 219 * d) / 255;
    let y = (v - 0.5) * contrast + 0.5 + b;
    y = Math.max(0, Math.min(1, y));
    y = Math.pow(y, 1 / gamma);
    const out = Math.max(0, Math.min(255, 255 * y));
    table.push(Math.max(0, Math.min(1, (out - 16) / 219)).toFixed(5));
  }
  const lut = document.createElementNS(_svgNS, 'feComponentTransfer');
  for (const ch of ['R', 'G', 'B']) {
    const fn = document.createElementNS(_svgNS, `feFunc${ch}`);
    fn.setAttribute('type', 'table');
    fn.setAttribute('tableValues', table.join(' '));
    lut.appendChild(fn);
  }
  filter.appendChild(lut);
  if (Math.abs(sat - 1) > 1e-6) {
    const cm = document.createElementNS(_svgNS, 'feColorMatrix');
    cm.setAttribute('type', 'saturate');
    cm.setAttribute('values', sat.toFixed(5));
    filter.appendChild(cm);
  }
  svg.appendChild(filter);
  _gradeFilterIds[key] = id;
  return id;
}

function applyCameraFilters() {
  if (!State.color) return;
  for (const c of State.clips) {
    const id = ensureGradeFilter(State.color.grades[c.id]);
    State.videos[c.id].style.filter = id ? `url(#${id})` : 'none';
  }
}

async function loadGrades() {
  try {
    const d = await api('/api/camera-grades').then(r => r.json());
    State.color = {
      keys: d.keys, bounds: d.bounds, defaults: d.defaults,
      grades: d.grades, dirty: false,
    };
    buildColorRows();
    applyCameraFilters();
  } catch (e) {
    $('colorStatus').textContent = '⚠ failed to load grades';
  }
}

function colorDirty(on) {
  State.color.dirty = on;
  $('colorStatus').textContent = on ? 'unsaved — hit Save color' : '—';
}

function buildColorRows() {
  if (!State.color) return;
  const box = $('colorCams');
  box.innerHTML = '';
  const K = State.color.keys;
  for (const c of State.clips) {
    const g = State.color.grades[c.id] ||
      (State.color.grades[c.id] = Object.assign({}, State.color.defaults[c.id]));
    const row = document.createElement('div');
    row.className = 'gradecam';
    row.dataset.cam = c.id;
    row.innerHTML =
      `<div class="cr-head"><span class="cr-name">${c.label}</span>` +
      `<div class="spacer"></div>` +
      `<button class="small gr-reset" title="Reset this camera to the built-in default grade">↺</button></div>` +
      K.map(k => {
        const [lo, hi] = State.color.bounds[k];
        return `<div class="cr-slider"><span class="klbl">${k}</span>` +
          `<input data-k="${k}" type="range" min="${lo}" max="${hi}" step="0.01" value="${g[k]}" />` +
          `<span class="kval k-${k}">${(+g[k]).toFixed(2)}</span></div>`;
      }).join('');
    box.appendChild(row);
    row.querySelectorAll('input[type=range]').forEach((inp) => {
      inp.addEventListener('input', () => {
        const k = inp.dataset.k;
        State.color.grades[c.id][k] = Number(inp.value);
        row.querySelector('.k-' + k).textContent = Number(inp.value).toFixed(2);
        applyCameraFilters();
        colorDirty(true);
      });
    });
    row.querySelector('.gr-reset').addEventListener('click', () => {
      State.color.grades[c.id] = Object.assign({}, State.color.defaults[c.id]);
      buildColorRows();
      applyCameraFilters();
      colorDirty(true);
    });
  }
  // onclick so rebuilds (reorder/reset) don't stack handlers
  $('colorResetAll').onclick = () => {
    for (const id in State.color.grades)
      State.color.grades[id] = Object.assign({}, State.color.defaults[id] || {});
    buildColorRows();
    applyCameraFilters();
    colorDirty(true);
  };
  $('colorSave').onclick = async () => {
    $('colorStatus').textContent = 'saving…';
    try {
      // send ALL cameras (incl. ones not shown here, e.g. the 5D 2) so the
      // shared grade document is replaced whole without dropping anything
      const r = await api('/api/camera-grades', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ grades: State.color.grades }),
      }).then(r => r.json());
      if (!r.ok) throw new Error(r.error || 'save failed');
      colorDirty(false);
      $('colorStatus').textContent = '✓ saved ' + new Date().toLocaleTimeString();
    } catch (e) {
      $('colorStatus').textContent = '⚠ save failed — ' + e.message;
    }
  };
}

/* ---------- playback ---------- */
function seekSrc(idx, t) {
  State.ph.idx = clamp(idx, 0, segs().length - 1);
  const s = segs()[State.ph.idx];
  State.ph.srcT = clamp(t, s.in, s.out);
  for (const id in State.videos) State.videos[id].currentTime = State.ph.srcT;
}
function seekOut(T) {
  const m = outToSrc(T);
  seekSrc(m.idx, m.t);
}

function playAll() {
  State.playing = true;
  $('playBtn').textContent = '❚❚ Pause';
  for (const id in State.videos) State.videos[id].play().catch(() => {});
}
/* Transport play: if a segment is selected, start from ITS beginning (unless
   the playhead is already somewhere inside it); otherwise from the playhead. */
function togglePlay() {
  if (State.playing) { pauseAll(); return; }
  const i = State.selected;
  if (i >= 0 && i !== State.ph.idx) seekSrc(i, segs()[i].in);
  playAll();
}
function pauseAll() {
  State.playing = false;
  $('playBtn').textContent = '▶︎ Play';
  for (const id in State.videos) {
    State.videos[id].pause();
    State.videos[id].playbackRate = 1;
  }
}

function tick() {
  const m = State.master;
  if (m && segs().length) {
    if (State.playing && !m.seeking) {
      State.ph.srcT = m.currentTime;
      const s = segs()[State.ph.idx];
      if (m.currentTime >= s.out - 0.02) {
        if (State.ph.idx + 1 < segs().length) {
          seekSrc(State.ph.idx + 1, segs()[State.ph.idx + 1].in);
        } else {
          pauseAll();
          seekSrc(State.ph.idx, s.out - 1e-3);
        }
      } else {
        // keep the two follower cameras locked to the master clock
        for (const id in State.videos) {
          const v = State.videos[id];
          if (v === m || v.seeking) continue;
          const d = v.currentTime - m.currentTime;
          if (Math.abs(d) > 0.09) { v.currentTime = m.currentTime; v.playbackRate = 1; }
          else if (Math.abs(d) > 0.02) v.playbackRate = clamp(1 - d * 0.6, 0.9, 1.1);
          else v.playbackRate = 1;
        }
      }
    }
    const T = phOut();
    $('tcOut').textContent = `${fmtOut(T)} / ${fmtOut(outDur())}`;
    $('tcSrc').textContent =
      `concert ${fmtSrc(State.ph.srcT)} · seg ${State.ph.idx + 1}/${segs().length}`;
    if (State.playing) {
      // keep the playhead in view while playing
      const { start, span } = State.view;
      if (T > start + span * 0.97 || T < start) {
        State.view.start = clamp(T - span * 0.1, 0, Math.max(0, outDur() - span));
        invalidate();
      }
    }
    drawTl();
  }
  requestAnimationFrame(tick);
}

function bindTransport() {
  $('playBtn').addEventListener('click', () => togglePlay());
  document.querySelectorAll('[data-nudge]').forEach(b =>
    b.addEventListener('click', () =>
      seekOut(phOut() + Number(b.dataset.nudge) / State.fps)));
  document.querySelectorAll('[data-jump]').forEach(b =>
    b.addEventListener('click', () => seekOut(phOut() + Number(b.dataset.jump))));
  $('splitBtn').addEventListener('click', splitAtPlayhead);
  $('markerBtn').addEventListener('click', addMarkerAtPlayhead);
  $('deleteBtn').addEventListener('click', deleteSelected);
  $('deleteMarkerBtn').addEventListener('click', deleteSelectedMarker);
  $('undoBtn').addEventListener('click', undo);
}

/* ---------- editing ---------- */
function pushUndo() {
  State.undoStack.push(JSON.stringify({
    segments: segs(), cams: State.doc.cams, layout: State.doc.layout,
    markers: State.doc.markers, selected: State.selected,
  }));
  if (State.undoStack.length > 120) State.undoStack.shift();
  State._undoTag = null;
  updateStatus();
}
/* Collapse a continuous gesture (drag/wheel/slider) into ONE undo step. */
function pushUndoOnce(tag) {
  if (State._undoTag === tag) return;
  pushUndo();
  State._undoTag = tag;
}
function undo() {
  const snap = State.undoStack.pop();
  if (!snap) return;
  const st = JSON.parse(snap);
  State.doc.segments = st.segments;
  State.doc.cams = st.cams;
  State.doc.markers = st.markers || [];
  State.selected = clamp(st.selected, -1, segs().length - 1);
  State.selMarker = -1;
  State._undoTag = null;
  invalidate();
  if (st.layout && st.layout.join() !== State.doc.layout.join()) {
    State.doc.layout = st.layout;
    applyLayoutOrder();
  }
  for (const c of State.clips) applyCam(c.id);
  seekOut(phOut());
  scheduleSave();
  updateStatus();
  drawTl();
}

function splitAtPlayhead() {
  const i = State.ph.idx, s = segs()[i], t = State.ph.srcT;
  if (!s || t - s.in < 0.05 || s.out - t < 0.05) {
    flashSave('⚠ playhead too close to a cut');
    return;
  }
  pushUndo();
  segs().splice(i, 1,
    { in: s.in, out: Math.round(t * 1000) / 1000 },
    { in: Math.round(t * 1000) / 1000, out: s.out });
  State.selected = i + 1;
  State.ph.idx = i + 1;
  invalidate();
  scheduleSave();
  updateStatus();
  drawTl();
}

function deleteSelected() {
  if (!(State.selected >= 0 && segs().length > 1)) return;
  pushUndo();
  const T = Math.min(phOut(), outBase(State.selected));
  segs().splice(State.selected, 1);
  State.selected = -1;
  seekOut(T);
  invalidate();
  scheduleSave();
  updateStatus();
  drawTl();
}

/* ---------- persistence ---------- */
async function doSave() {
  clearTimeout(State.saveTimer);
  State.saveTimer = null;
  try {
    const r = await api('/api/reels', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(
        Object.assign({}, State.doc, { project: State.projectId })),
    }).then(r => r.json());
    if (!r.ok) throw new Error(r.error || 'save failed');
    $('saveState').textContent = '✓ saved ' + new Date().toLocaleTimeString();
    const meta = State.projects.find(p => p.id === State.projectId);
    if (meta) { meta.segments = segs().length; meta.reel = outDur(); }
  } catch (e) {
    $('saveState').textContent = '⚠ save failed — ' + e.message;
  }
}
function scheduleSave() {
  $('saveState').textContent = 'saving…';
  clearTimeout(State.saveTimer);
  State.saveTimer = setTimeout(doSave, 500);
  updateStatus();
}
async function flushSave() {
  if (State.saveTimer) await doSave();
}

/* ---------- reel projects ---------- */
/* Each project is a complete independent cut of the show (segments, framing,
   markers). "New" starts from the default doc: the ENTIRE show as one
   compound clip. The open project is the server's `active` one — that's what
   Export renders. */
function refreshProjectSelect() {
  const sel = $('projSelect');
  sel.innerHTML = '';
  for (const p of State.projects) {
    const o = document.createElement('option');
    o.value = p.id;
    o.textContent = p.name;
    sel.appendChild(o);
  }
  sel.value = State.projectId;
}

async function projectRequest(path, body) {
  const r = await api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }).then(r => r.json());
  if (!r.ok) throw new Error(r.error || 'request failed');
  return r;
}

function applyProjectState(st) {
  State.projects = st.projects || State.projects;
  State.projectId = st.active;
  loadDoc(st.doc);
}

/* Swap the whole editing state over to another project's doc. */
function loadDoc(doc) {
  pauseAll();
  State.doc = doc;
  State.doc.markers = doc.markers || [];
  State.selected = -1;
  State.selMarker = -1;
  State.undoStack = [];
  State._undoTag = null;
  applyLayoutOrder();            // panes/rows follow the doc's layout + cams
  fitView();
  seekOut(0);
  refreshProjectSelect();
  invalidate();
  updateStatus();
  drawTl();
  refreshExport();               // the Open button belongs to the new reel
}

function buildProjectBar() {
  refreshProjectSelect();
  $('projSelect').addEventListener('change', async (e) => {
    const id = e.target.value;
    try {
      await flushSave();
      applyProjectState(await projectRequest('/api/reels/open', { id }));
    } catch (err) {
      flashSave('⚠ ' + err.message);
      refreshProjectSelect();
    }
  });
  $('projNew').addEventListener('click', async () => {
    const name = prompt(
      'Name for the new reel (it starts with the entire show):',
      `Reel ${State.projects.length + 1}`);
    if (name === null) return;
    try {
      await flushSave();
      applyProjectState(await projectRequest('/api/reels/new', { name }));
      flashSave('✓ new reel — full show loaded');
    } catch (err) { flashSave('⚠ ' + err.message); }
  });
  $('projRename').addEventListener('click', async () => {
    const cur = State.projects.find(p => p.id === State.projectId);
    const name = prompt('Rename this reel:', cur ? cur.name : '');
    if (!name) return;
    try {
      const st = await projectRequest('/api/reels/rename',
                                      { id: State.projectId, name });
      State.projects = st.projects;
      refreshProjectSelect();
    } catch (err) { flashSave('⚠ ' + err.message); }
  });
  $('projDelete').addEventListener('click', async () => {
    const cur = State.projects.find(p => p.id === State.projectId);
    if (!confirm(`Delete reel "${cur ? cur.name : ''}"? Only its cut list is ` +
                 'removed — the footage is untouched.')) return;
    try {
      applyProjectState(
        await projectRequest('/api/reels/delete', { id: State.projectId }));
    } catch (err) { flashSave('⚠ ' + err.message); }
  });
}
function flashSave(msg) {
  $('saveState').textContent = msg;
  setTimeout(() => { if ($('saveState').textContent === msg) $('saveState').textContent = ''; }, 2500);
}

/* ---------- waveform ---------- */
async function loadWaveform() {
  try {
    const meta = await api('/api/waveform').then(r => r.json());
    if (!meta.ready) return;
    const buf = await api('/waveform.u8').then(r => r.arrayBuffer());
    State.wave = {
      ready: true, peaks: new Uint8Array(buf),
      pps: meta.peaks_per_second || 100,
    };
    invalidate();
    drawTl();
  } catch (e) { /* waveform is optional */ }
}
/* [max, mean] of the waveform peaks in [t0,t1], each 0..1 */
function peakStats(t0, t1) {
  const w = State.wave;
  if (!w.ready) return [0, 0];
  let i0 = Math.max(0, Math.floor(t0 * w.pps));
  const i1 = Math.min(w.peaks.length, Math.max(Math.ceil(t1 * w.pps), i0 + 1));
  let m = 0, sum = 0, n = 0;
  for (; i0 < i1; i0++) {
    const p = w.peaks[i0];
    if (p > m) m = p;
    sum += p; n++;
  }
  return n ? [m / 255, sum / (n * 255)] : [0, 0];
}

/* ---------- timeline ---------- */
/* Static layer (ruler, blocks, waveform, markers, selection) is cached in an
   offscreen canvas and only rebuilt when invalidate()d; the per-frame drawTl
   just blits it and draws the playhead + hover cursor on top. That's what
   makes the 1px-resolution waveform affordable at 60fps. */
const Tl = { cvs: null, ctx: null, w: 0, h: 0, drag: null,
             static: null, dirty: true, hoverX: null };

function invalidate() { Tl.dirty = true; }

function setupTimeline() {
  Tl.cvs = $('tl');
  Tl.ctx = Tl.cvs.getContext('2d');
  Tl.static = document.createElement('canvas');
  resizeTl();
  Tl.cvs.addEventListener('mousedown', tlDown);
  window.addEventListener('mousemove', tlMove);
  window.addEventListener('mouseup', tlUp);
  Tl.cvs.addEventListener('mouseleave', () => { Tl.hoverX = null; });
  Tl.cvs.addEventListener('wheel', tlWheel, { passive: false });
  $('zoomFit').addEventListener('click', () => { fitView(); drawTl(); });
  $('zoomIn').addEventListener('click', () => { zoomView(1 / 1.5); drawTl(); });
  $('zoomOut').addEventListener('click', () => { zoomView(1.5); drawTl(); });
}
function resizeTl() {
  const dpr = window.devicePixelRatio || 1;
  Tl.w = Tl.cvs.clientWidth;
  Tl.h = Tl.cvs.clientHeight;
  Tl.cvs.width = Math.round(Tl.w * dpr);
  Tl.cvs.height = Math.round(Tl.h * dpr);
  Tl.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  Tl.static.width = Tl.cvs.width;
  Tl.static.height = Tl.cvs.height;
  invalidate();
}
function fitView() {
  State.view = { start: 0, span: Math.max(1, outDur()) };
  invalidate();
}
function zoomView(f, cx) {
  const v = State.view;
  const T = cx !== undefined ? xToTime(cx) : v.start + v.span / 2;
  v.span = clamp(v.span * f, 0.5, Math.max(1, outDur()));
  v.start = clamp(T - (T - v.start) * f, 0, Math.max(0, outDur() - v.span));
  invalidate();
}
/* ⌘+ / ⌘− zoom keeps the playhead anchored (falls back to view center). */
function zoomAtPlayhead(f) {
  const x = timeToX(phOut());
  zoomView(f, x >= 0 && x <= Tl.w ? x : undefined);
  drawTl();
}

/* ---------- markers + snapping ---------- */
/* Where each marker is visible: its concert time mapped into output time,
   once per segment whose window contains it. */
function markerOccurrences() {
  const out = [];
  let base = 0;
  for (const s of segs()) {
    State.doc.markers.forEach((m, mi) => {
      if (m.t >= s.in && m.t <= s.out) out.push({ mi, T: base + (m.t - s.in) });
    });
    base += s.out - s.in;
  }
  return out;
}

const SNAP_PX = 8;

/* Snap an OUTPUT time to nearby markers / cut boundaries (scrubbing). */
function snapOut(T, disable) {
  if (disable) return T;
  const thr = SNAP_PX * State.view.span / Tl.w;
  let best = null, bd = thr;
  const consider = (t) => {
    const dd = Math.abs(t - T);
    if (dd < bd) { bd = dd; best = t; }
  };
  for (const o of markerOccurrences()) consider(o.T);
  let a = 0;
  for (const s of segs()) { consider(a); a += s.out - s.in; }
  consider(a);
  return best !== null ? best : T;
}

/* Snap a CONCERT time to nearby markers (edge trims). */
function snapSrc(t, disable) {
  if (disable) return t;
  const thr = SNAP_PX * State.view.span / Tl.w;
  let best = t, bd = thr;
  for (const m of State.doc.markers) {
    const dd = Math.abs(m.t - t);
    if (dd < bd) { bd = dd; best = m.t; }
  }
  return best;
}

function addMarkerAtPlayhead() {
  const t = Math.round(State.ph.srcT * 1000) / 1000;
  if (State.doc.markers.some(m => Math.abs(m.t - t) < 0.05)) {
    flashSave('⚠ marker already at the playhead');
    return;
  }
  pushUndo();
  State.doc.markers.push({ t });
  State.doc.markers.sort((a, b) => a.t - b.t);
  State.selMarker = State.doc.markers.findIndex(m => m.t === t);
  State.selected = -1;
  invalidate();
  scheduleSave();
  updateStatus();
  drawTl();
}

function deleteSelectedMarker() {
  if (!(State.selMarker >= 0)) return;
  pushUndo();
  State.doc.markers.splice(State.selMarker, 1);
  State.selMarker = -1;
  invalidate();
  scheduleSave();
  updateStatus();
  drawTl();
}
const timeToX = (t) => (t - State.view.start) / State.view.span * Tl.w;
const xToTime = (x) => State.view.start + x / Tl.w * State.view.span;

function tickStep(target) {
  const steps = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  for (const s of steps) if (s / State.view.span * Tl.w >= target) return s;
  return 600;
}

function rebuildStatic() {
  Tl.dirty = false;
  const dpr = window.devicePixelRatio || 1;
  const ctx = Tl.static.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = Tl.w, H = Tl.h;
  ctx.clearRect(0, 0, W, H);
  const css = getComputedStyle(document.documentElement);
  const cAccent = css.getPropertyValue('--accent').trim() || '#4f8cff';
  const cMuted = css.getPropertyValue('--muted').trim() || '#8b93a7';
  const cWarn = css.getPropertyValue('--warn').trim() || '#f59e0b';
  const y0 = 20, bh = H - y0 - 6;

  // ruler
  ctx.font = '10px ui-monospace, Menlo, monospace';
  ctx.fillStyle = cMuted;
  ctx.strokeStyle = 'rgba(139,147,167,0.25)';
  const step = tickStep(70);
  for (let t = Math.ceil(State.view.start / step) * step;
       t <= State.view.start + State.view.span; t += step) {
    const x = timeToX(t);
    ctx.beginPath(); ctx.moveTo(x, 12); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillText(fmtOut(t).replace(/\.\d+$/, ''), x + 3, 10);
  }

  // segments (contiguous in output time)
  let base = 0;
  const fills = ['rgba(79,140,255,0.16)', 'rgba(52,211,153,0.13)'];
  segs().forEach((s, i) => {
    const d = s.out - s.in;
    const x0 = timeToX(base), x1 = timeToX(base + d);
    base += d;
    if (x1 < -20 || x0 > W + 20) return;
    const bx = x0 + 1, bw = Math.max(2, x1 - x0 - 2);
    ctx.fillStyle = fills[i % 2];
    ctx.fillRect(bx, y0, bw, bh);
    // waveform: mirrored around the block's midline, 1px columns, two-tone
    // (dim = max peak, bright = mean) so transients stand out precisely
    if (State.wave.ready && bw > 4) {
      const mid = y0 + bh / 2, amp = bh / 2 - 3;
      const xs = Math.max(0, Math.floor(-bx));            // clip to viewport
      const xe = Math.min(bw, Math.ceil(W - bx));
      for (let x = xs; x < xe; x++) {
        const t0 = s.in + (x / bw) * d, t1 = s.in + ((x + 1) / bw) * d;
        const st = peakStats(t0, t1);
        if (!st[0]) continue;
        ctx.fillStyle = 'rgba(160,190,255,0.30)';
        ctx.fillRect(bx + x, mid - st[0] * amp, 1, Math.max(1, st[0] * amp * 2));
        ctx.fillStyle = 'rgba(215,230,255,0.80)';
        ctx.fillRect(bx + x, mid - st[1] * amp, 1, Math.max(1, st[1] * amp * 2));
      }
    }
    // border + selection
    ctx.strokeStyle = i === State.selected ? cAccent : 'rgba(42,48,64,0.9)';
    ctx.lineWidth = i === State.selected ? 2 : 1;
    ctx.strokeRect(bx + 0.5, y0 + 0.5, bw - 1, bh - 1);
    if (i === State.selected) {
      ctx.fillStyle = cAccent;
      ctx.fillRect(bx, y0, 3, bh);
      ctx.fillRect(bx + bw - 3, y0, 3, bh);
    }
    // labels
    if (bw > 90) {
      ctx.fillStyle = 'rgba(230,233,239,0.85)';
      ctx.fillText(`${d.toFixed(1)}s`, bx + 5, y0 + 12);
      ctx.fillStyle = 'rgba(139,147,167,0.9)';
      ctx.fillText(fmtSrc(s.in), bx + 5, y0 + bh - 4);
      const lbl = fmtSrc(s.out);
      ctx.fillText(lbl, bx + bw - ctx.measureText(lbl).width - 5, y0 + bh - 4);
    }
    ctx.lineWidth = 1;
  });

  // markers (⚑ amber; selected = white)
  for (const o of markerOccurrences()) {
    const x = timeToX(o.T);
    if (x < -10 || x > W + 10) continue;
    const sel = o.mi === State.selMarker;
    ctx.strokeStyle = sel ? '#fff' : cWarn;
    ctx.fillStyle = sel ? '#fff' : cWarn;
    ctx.lineWidth = sel ? 2 : 1;
    ctx.beginPath(); ctx.moveTo(x, 12); ctx.lineTo(x, H); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(x, 12); ctx.lineTo(x + 8, 16.5); ctx.lineTo(x, 21);
    ctx.closePath(); ctx.fill();
    ctx.lineWidth = 1;
  }
}

function drawTl() {
  const ctx = Tl.ctx;
  if (!ctx || !State.doc) return;
  if (Tl.dirty) rebuildStatic();
  const W = Tl.w, H = Tl.h;
  ctx.clearRect(0, 0, W, H);
  ctx.drawImage(Tl.static, 0, 0, W, H);

  // hover cursor: shows exactly where a click would land (after snapping)
  // with its precise output + concert timecodes
  if (Tl.hoverX !== null && !Tl.drag) {
    const T = snapOut(xToTime(Tl.hoverX), false);
    const x = timeToX(T);
    const snapped = Math.abs(x - Tl.hoverX) > 0.5;
    ctx.strokeStyle = snapped ? 'rgba(245,158,11,0.9)' : 'rgba(255,255,255,0.4)';
    ctx.beginPath(); ctx.moveTo(x, 12); ctx.lineTo(x, H); ctx.stroke();
    const m = outToSrc(T);
    const label = `${fmtOut(T)}  ·  concert ${fmtSrc(m.t)}`;
    ctx.font = '11px ui-monospace, Menlo, monospace';
    const tw = ctx.measureText(label).width;
    const lx = clamp(x + 8, 2, W - tw - 10);
    ctx.fillStyle = 'rgba(14,16,20,0.92)';
    ctx.fillRect(lx - 4, 14, tw + 8, 16);
    ctx.strokeStyle = 'rgba(42,48,64,0.9)';
    ctx.strokeRect(lx - 3.5, 14.5, tw + 7, 15);
    ctx.fillStyle = snapped ? '#f5c518' : '#e6e9ef';
    ctx.fillText(label, lx, 26);
  }

  // move-drag: insertion caret at the nearest cut + a ghost of the clip
  if (Tl.drag && Tl.drag.mode === 'move') {
    const i = Tl.drag.idx, s = segs()[i], d = s.out - s.in;
    const k = moveTargetAt(Tl.drag.x);
    let a = 0;
    for (let j = 0; j < k; j++) a += segs()[j].out - segs()[j].in;
    const cx = timeToX(a);
    ctx.strokeStyle = '#f5c518';
    ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(cx, 12); ctx.lineTo(cx, H); ctx.stroke();
    ctx.fillStyle = '#f5c518';
    ctx.beginPath();
    ctx.moveTo(cx - 6, 12); ctx.lineTo(cx + 6, 12); ctx.lineTo(cx, 20);
    ctx.closePath(); ctx.fill();
    ctx.lineWidth = 1;
    const gw = clamp(d / State.view.span * W, 26, 200);
    const gx = clamp(Tl.drag.x - gw / 2, 0, W - gw);
    ctx.fillStyle = 'rgba(79,140,255,0.30)';
    ctx.fillRect(gx, 28, gw, H - 42);
    ctx.strokeStyle = '#4f8cff';
    ctx.strokeRect(gx + 0.5, 28.5, gw - 1, H - 43);
    ctx.fillStyle = '#e6e9ef';
    ctx.font = '10px ui-monospace, Menlo, monospace';
    const at = k > i ? k : k + 1;
    ctx.fillText(`clip ${i + 1} (${d.toFixed(1)}s) → position ${at}`, gx + 5, 41);
  }

  // playhead
  const px = timeToX(phOut());
  if (px >= 0 && px <= W) {
    ctx.strokeStyle = '#fff';
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
    ctx.fillStyle = '#fff';
    ctx.beginPath();
    ctx.moveTo(px - 5, 0); ctx.lineTo(px + 5, 0); ctx.lineTo(px, 7);
    ctx.closePath(); ctx.fill();
  }

  $('zoomLabel').textContent =
    `${fmtOut(State.view.start).replace(/\.\d+$/, '')} – ` +
    `${fmtOut(State.view.start + State.view.span).replace(/\.\d+$/, '')}`;
}

function hitTest(x) {
  // Boundary xs: bnd[k] is the cut between clip k-1 and clip k (k = 0..n).
  const n = segs().length;
  const bnd = [];
  let base = 0;
  for (let i = 0; i < n; i++) { bnd.push(timeToX(base)); base += segs()[i].out - segs()[i].in; }
  bnd.push(timeToX(base));

  // Nearest boundary to the cursor; if within the handle zone it's an edge.
  let k = 0, bd = Infinity;
  for (let j = 0; j <= n; j++) {
    const dd = Math.abs(x - bnd[j]);
    if (dd < bd) { bd = dd; k = j; }
  }
  if (bd <= EDGE_PX) {
    // An internal cut is shared by clip k-1's OUT and clip k's IN. Pick by the
    // side of the cut the cursor is on so BOTH edges are reachable: left of the
    // line trims the left clip's right edge, right of it the right clip's left.
    const leftIdx = k - 1, rightIdx = k;
    if (leftIdx < 0) return { idx: 0, edge: 'in' };
    if (rightIdx >= n) return { idx: n - 1, edge: 'out' };
    return x < bnd[k]
      ? { idx: leftIdx, edge: 'out' }
      : { idx: rightIdx, edge: 'in' };
  }
  // Otherwise, the body of whichever clip contains x.
  for (let i = 0; i < n; i++) {
    if (x > bnd[i] && x < bnd[i + 1]) return { idx: i, edge: null };
  }
  return null;
}

/* A marker flag under the cursor (top strip of the timeline). */
function markerHit(x, y) {
  if (y > 24) return null;
  let best = null, bd = 9;
  for (const o of markerOccurrences()) {
    const dd = Math.abs(timeToX(o.T) - x);
    if (dd < bd) { bd = dd; best = o; }
  }
  return best;
}

function tlDown(e) {
  const rect = Tl.cvs.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  const mk = markerHit(x, y);
  if (mk) {
    // select the marker and park the playhead exactly on it (S cuts there)
    State.selMarker = mk.mi;
    State.selected = -1;
    seekOut(mk.T);
    invalidate();
    updateStatus();
    drawTl();
    return;
  }
  const hit = hitTest(x);
  const onPlayhead = Math.abs(timeToX(phOut()) - x) <= 6;
  if (hit && hit.edge) {
    pushUndo();
    State.selected = hit.idx;
    State.selMarker = -1;
    const s = segs()[hit.idx];
    Tl.drag = { mode: 'trim', idx: hit.idx, edge: hit.edge, lastX: x,
                val: hit.edge === 'in' ? s.in : s.out };
  } else if (hit && y > 20 && !onPlayhead) {
    // block body: click = select + seek; dragging >5px turns into a MOVE
    // (reorder). Scrub-drags live on the ruler strip or the playhead itself.
    State.selected = hit.idx;
    State.selMarker = -1;
    seekOut(snapOut(xToTime(x), e.shiftKey));
    Tl.drag = { mode: 'maybemove', idx: hit.idx, startX: x, x };
  } else {
    State.selected = hit ? hit.idx : -1;
    State.selMarker = -1;
    Tl.drag = { mode: 'scrub' };
    seekOut(snapOut(xToTime(x), e.shiftKey));
  }
  invalidate();
  updateStatus();
  drawTl();
}

/* Cut boundary nearest an output x = the insertion point (0..n). */
function moveTargetAt(dragX) {
  const T = xToTime(dragX);
  let a = 0, best = 0, bd = Infinity;
  for (let k = 0; k <= segs().length; k++) {
    const dd = Math.abs(T - a);
    if (dd < bd) { bd = dd; best = k; }
    if (k < segs().length) a += segs()[k].out - segs()[k].in;
  }
  return best;
}

/* Move the segment at `i` so it sits at insertion boundary `k`; keeps the
   playhead on the moved clip. Shared by drag-drop and the [ ] keys. */
function commitMove(i, k) {
  if (k === i || k === i + 1) return false;      // dropping where it already is
  pushUndo();
  const [seg] = segs().splice(i, 1);
  const at = k > i ? k - 1 : k;
  segs().splice(at, 0, seg);
  State.selected = at;
  seekSrc(at, clamp(State.ph.srcT, seg.in, seg.out));
  invalidate();
  scheduleSave();
  updateStatus();
  drawTl();
  return true;
}

function moveSelected(dir) {
  const i = State.selected;
  if (i < 0) return;
  if (dir > 0 ? i >= segs().length - 1 : i <= 0) return;
  commitMove(i, dir > 0 ? i + 2 : i - 1);
}

function tlUp() {
  const d = Tl.drag;
  Tl.drag = null;
  State._undoTag = null;
  if (d && d.mode === 'move') {
    commitMove(d.idx, moveTargetAt(d.x));
    drawTl();
  }
}

function tlMove(e) {
  const rect = Tl.cvs.getBoundingClientRect();
  const x = e.clientX - rect.left;
  if (!Tl.drag) {
    if (e.target === Tl.cvs) {
      Tl.hoverX = x;
      const y = e.clientY - rect.top;
      const hit = hitTest(x);
      const mk = markerHit(x, y);
      Tl.cvs.style.cursor =
        mk ? 'pointer'
        : hit && hit.edge ? 'col-resize'
        : hit && y > 20 ? 'grab'
        : 'crosshair';
    } else {
      Tl.hoverX = null;
    }
    return;
  }
  if (Tl.drag.mode === 'scrub') {
    seekOut(snapOut(xToTime(clamp(x, 0, Tl.w)), e.shiftKey));
    return;
  }
  if (Tl.drag.mode === 'maybemove') {
    if (Math.abs(x - Tl.drag.startX) < 5) return;
    Tl.drag = { mode: 'move', idx: Tl.drag.idx, x };
    Tl.cvs.style.cursor = 'grabbing';
  }
  if (Tl.drag.mode === 'move') {
    Tl.drag.x = clamp(x, 0, Tl.w);
    return;               // ghost + insertion caret drawn by drawTl
  }
  // trim: accumulate the drag in unsnapped space (drag.val) and snap the
  // candidate each move — sticky near markers but still escapable
  Tl.drag.val += (x - Tl.drag.lastX) / Tl.w * State.view.span;
  Tl.drag.lastX = x;
  const s = segs()[Tl.drag.idx];
  const cand = snapSrc(Tl.drag.val, e.shiftKey);
  if (Tl.drag.edge === 'in') s.in = clamp(cand, 0, s.out - MIN_SEG);
  else s.out = clamp(cand, s.in + MIN_SEG, State.duration);
  s.in = Math.round(s.in * 1000) / 1000;
  s.out = Math.round(s.out * 1000) / 1000;
  invalidate();
  seekOut(phOut());
  scheduleSave();
  updateStatus();
  drawTl();
}

function tlWheel(e) {
  e.preventDefault();
  const rect = Tl.cvs.getBoundingClientRect();
  if (e.deltaY) zoomView(Math.exp(e.deltaY * 0.0015), e.clientX - rect.left);
  if (e.deltaX) {
    State.view.start = clamp(
      State.view.start + e.deltaX / Tl.w * State.view.span,
      0, Math.max(0, outDur() - State.view.span));
    invalidate();
  }
  drawTl();
}

/* ---------- keys ---------- */
function bindKeys() {
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') {
      e.preventDefault(); undo(); return;
    }
    if ((e.metaKey || e.ctrlKey) && (e.key === '=' || e.key === '+')) {
      e.preventDefault(); zoomAtPlayhead(1 / 1.6); return;
    }
    if ((e.metaKey || e.ctrlKey) && (e.key === '-' || e.key === '_')) {
      e.preventDefault(); zoomAtPlayhead(1.6); return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key === '0') {
      e.preventDefault(); fitView(); drawTl(); return;
    }
    if (e.metaKey || e.ctrlKey) return;
    switch (e.key) {
      case ' ':
        e.preventDefault();
        togglePlay();
        break;
      case 'ArrowLeft':
        seekOut(phOut() + (e.shiftKey ? -5 : -1 / State.fps)); break;
      case 'ArrowRight':
        seekOut(phOut() + (e.shiftKey ? 5 : 1 / State.fps)); break;
      case 's': case 'S':
        splitAtPlayhead(); break;
      case 'm': case 'M':
        addMarkerAtPlayhead(); break;
      case '[':
        moveSelected(-1); break;
      case ']':
        moveSelected(1); break;
      case 'Backspace': case 'Delete':
        e.preventDefault();
        if (State.selMarker >= 0) deleteSelectedMarker();
        else deleteSelected();
        break;
      case 'Escape':
        State.selected = -1; State.selMarker = -1;
        invalidate(); updateStatus(); drawTl(); break;
      case '+': case '=':
        zoomView(1 / 1.5); drawTl(); break;
      case '-': case '_':
        zoomView(1.5); drawTl(); break;
      case '0':
        fitView(); drawTl(); break;
    }
  });
}

/* ---------- export ---------- */
function bindExport() {
  $('exportBtn').addEventListener('click', async () => {
    try {
      // flush the latest cut AND make this project the active one — the
      // render script exports the store's active project
      await doSave();
      const r = await api('/api/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ style: 'reels' }),
      }).then(r => r.json());
      if (!r.ok) throw new Error(r.error || 'export failed');
      pollExport();
    } catch (e) {
      flashSave('⚠ export failed — ' + e.message);
    }
  });
  $('openBtn').addEventListener('click', () => {
    const file = $('openBtn').dataset.file;
    if (!file) return;
    api('/api/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file }),
    });
  });
  refreshExport();  // show ▶ Open if this reel finished rendering earlier
}

async function refreshExport() {
  try {
    const st = await api('/api/styles').then(r => r.json());
    const me = (st.styles || []).find(s => s.id === 'reels');
    if (!me) return false;
    const job = me.job;
    const btn = $('exportBtn');
    if (job && (job.status === 'running' || job.status === 'queued')) {
      btn.disabled = true;
      btn.textContent = job.status === 'queued'
        ? '⏳ queued…'
        : `⏳ ${job.phase} ${job.progress || 0}%`;
      return true;
    }
    btn.disabled = false;
    btn.textContent = '🎬 Export reel';
    if (job && job.status === 'error') flashSave('⚠ render failed — ' + (job.error || ''));
    // output filename is per-project (reel_<name>.mp4) — take it from the
    // finished job's "✓" line rather than a fixed name
    const file = (job && job.status === 'done' && job.file) || null;
    $('openBtn').hidden = !file;
    if (file) $('openBtn').dataset.file = file;
    return false;
  } catch (e) { return false; }
}

function pollExport() {
  clearInterval(State.exportTimer);
  State.exportTimer = setInterval(async () => {
    const busy = await refreshExport();
    if (!busy) clearInterval(State.exportTimer);
  }, 1500);
  refreshExport();
}

boot();
