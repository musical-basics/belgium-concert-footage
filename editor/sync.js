/* 5D 2 ↔ Back Camera sync viewer.
 *
 * The back-camera proxy is the master clock; the 5D 2 proxy chases it through
 * the audio-matched clip map in sync.json (src = 5D 2 time, ref = back-camera
 * time, delta = ref - src). Where no clip covers the playhead the 5D 2 pane
 * shows "NOT CAPTURED" black, exactly like the gaps in a real edit timeline.
 */
'use strict';

const $ = (s) => document.querySelector(s);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const fmtTC = (t) => {
  t = Math.max(0, t);
  const h = String(Math.floor(t / 3600)).padStart(2, '0');
  const m = String(Math.floor(t / 60) % 60).padStart(2, '0');
  const s = String(Math.floor(t % 60)).padStart(2, '0');
  const ms = String(Math.round(t % 1 * 1000)).padStart(3, '0');
  return `${h}:${m}:${s}.${ms}`;
};

const S = {
  dur: 5764.7,
  clips: [],
  perfs: [],
  view: { start: 0, span: 5764.7 },
  wave: null,            // Uint8Array peaks
  wavePps: 100,
  playing: false,
  activeClip: -1,
  canvas: null, ctx: null, w: 0, h: 0, dpr: 1,
};

const vback = $('#vback');
const v5d2 = $('#v5d2');

/* ------------------------------------------------------------ data loading */
async function boot() {
  const sync = await fetch('/editor/sync.json').then(r => r.json());
  S.clips = sync.clips;
  S.dur = sync.ref_duration;
  S.view = { start: 0, span: S.dur };
  $('#status').textContent =
    `${S.clips.length} clips · ${Math.round(sync.clips.reduce((a, c) => a + c.dur, 0))}s matched`;

  fetch('/api/markers').then(r => r.json()).then(m => {
    S.perfs = m.performances || [];
    draw();
  }).catch(() => {});

  const meta = await fetch('/api/waveform').then(r => r.json()).catch(() => ({ ready: false }));
  if (meta.ready) {
    S.wavePps = meta.peaks_per_second || 100;
    const buf = await fetch('/waveform.u8').then(r => r.arrayBuffer());
    S.wave = new Uint8Array(buf);
  }
  initCanvas();
  wire();
  draw();
  tick();
}

/* ------------------------------------------------------------- clip lookup */
function clipAt(refT) {
  for (let i = 0; i < S.clips.length; i++) {
    const c = S.clips[i];
    if (refT >= c.ref_in && refT < c.ref_out) return i;
  }
  return -1;
}

/* --------------------------------------------------------------- playback */
function seekRef(t, force) {
  t = clamp(t, 0, S.dur - 0.05);
  vback.currentTime = t;
  chase(t, true);
  draw();
}

function chase(refT, force) {
  const i = clipAt(refT);
  if (i !== S.activeClip) { S.activeClip = i; updateClipInfo(); }
  const nc = $('#notCaptured');
  if (i < 0) {
    nc.hidden = false;
    if (!v5d2.paused) v5d2.pause();
    return;
  }
  nc.hidden = true;
  const c = S.clips[i];
  const target = refT - c.delta;
  if (force || Math.abs(v5d2.currentTime - target) > 0.12) v5d2.currentTime = target;
  if (S.playing && v5d2.paused) v5d2.play().catch(() => {});
  if (!S.playing && !v5d2.paused) v5d2.pause();
}

function updateClipInfo() {
  const el = $('#clipInfo');
  if (S.activeClip < 0) { el.innerHTML = 'gap — back camera only'; return; }
  const c = S.clips[S.activeClip];
  el.innerHTML = `<b>clip ${c.i}</b> · 5D2 ${fmtTC(c.src_in)}→${fmtTC(c.src_out)} ` +
    `· Δ ${c.delta.toFixed(3)}s · env ${c.env_score.toFixed(2)} · PHAT ${c.phat_locks}/3` +
    (c.flags || []).map(f => `<span class="flagchip">${f}</span>`).join('');
}

function togglePlay() {
  if (S.playing) {
    S.playing = false;
    vback.pause(); v5d2.pause();
    $('#playBtn').textContent = '▶︎ Play';
  } else {
    S.playing = true;
    vback.play().catch(() => {});
    $('#playBtn').textContent = '‖ Pause';
  }
}

function applyAudioSel() {
  const mode = $('#audioSel').value;
  vback.muted = (mode === '5d2');
  v5d2.muted = (mode === 'back');
}

function tick() {
  const t = vback.currentTime;
  $('#timecode').textContent = fmtTC(t);
  if (S.playing) chase(t, false);
  drawPlayheadOnly();
  requestAnimationFrame(tick);
}

/* ---------------------------------------------------------------- timeline */
const LANE = { ruler: 22, clips: [30, 72], ref: [86, 158] };

function initCanvas() {
  S.canvas = $('#syncTl');
  S.ctx = S.canvas.getContext('2d');
  S.off = document.createElement('canvas');
  const resize = () => {
    S.dpr = window.devicePixelRatio || 1;
    S.w = S.canvas.clientWidth;
    S.h = S.canvas.clientHeight;
    S.canvas.width = S.w * S.dpr;
    S.canvas.height = S.h * S.dpr;
    S.off.width = S.canvas.width;
    S.off.height = S.canvas.height;
    draw();
  };
  window.addEventListener('resize', resize);
  resize();
}

const timeToX = (t) => (t - S.view.start) / S.view.span * S.w;
const xToTime = (x) => S.view.start + x / S.w * S.view.span;

function tickStep(span) {
  const target = span / (S.w / 90);
  const steps = [.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800];
  return steps.find(s => s >= target) || 3600;
}

function draw() {
  if (!S.ctx) return;
  const ctx = S.off.getContext('2d');
  ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
  ctx.clearRect(0, 0, S.w, S.h);
  ctx.fillStyle = '#0c0e13';
  ctx.fillRect(0, 0, S.w, S.h);

  // ruler
  ctx.font = '10px -apple-system, sans-serif';
  const step = tickStep(S.view.span);
  const t0 = Math.floor(S.view.start / step) * step;
  for (let t = t0; t <= S.view.start + S.view.span; t += step) {
    const x = timeToX(t);
    ctx.strokeStyle = '#232838';
    ctx.beginPath(); ctx.moveTo(x, LANE.ruler - 6); ctx.lineTo(x, S.h); ctx.stroke();
    ctx.fillStyle = '#6b7387';
    ctx.fillText(fmtTC(t).slice(0, 8), x + 3, 12);
  }

  // performance markers (from the main editor) as context under the ruler
  ctx.font = '9px -apple-system, sans-serif';
  for (const p of S.perfs) {
    const x1 = timeToX(p.in), x2 = timeToX(p.out);
    if (x2 < 0 || x1 > S.w) continue;
    ctx.fillStyle = 'rgba(99,110,140,.18)';
    ctx.fillRect(x1, LANE.ruler - 5, x2 - x1, 5);
    if (x2 - x1 > 40) {
      ctx.fillStyle = '#565f75';
      ctx.fillText(p.title, Math.max(2, x1 + 2), LANE.ruler - 8);
    }
  }

  // 5D2 clip lane: gaps = black hatch, clips = FCP-blue blocks
  const [cy0, cy1] = LANE.clips;
  ctx.fillStyle = '#05060a';
  ctx.fillRect(0, cy0, S.w, cy1 - cy0);
  for (const c of S.clips) {
    const x1 = timeToX(c.ref_in), x2 = timeToX(c.ref_out);
    if (x2 < 0 || x1 > S.w) continue;
    const black = (c.flags || []).includes('black-video');
    ctx.fillStyle = black ? '#1d2f45' : '#2f5d8a';
    ctx.fillRect(x1, cy0, Math.max(x2 - x1, 1.5), cy1 - cy0);
    ctx.strokeStyle = '#7fb2e6';
    ctx.strokeRect(x1 + .5, cy0 + .5, Math.max(x2 - x1, 1.5) - 1, cy1 - cy0 - 1);
    if (x2 - x1 > 34) {
      ctx.fillStyle = '#cfe3f7';
      ctx.font = '600 10px -apple-system, sans-serif';
      ctx.fillText(`5D2 #${c.i}${black ? ' (black)' : ''}`, x1 + 4, cy0 + 13);
      ctx.fillStyle = '#89a9c9';
      ctx.font = '9px -apple-system, sans-serif';
      ctx.fillText(`${fmtTC(c.src_in).slice(3, 8)}→`, x1 + 4, cy0 + 25);
    }
  }
  ctx.fillStyle = '#4a5164';
  ctx.font = '9px -apple-system, sans-serif';
  ctx.fillText('5D 2 (audio-matched clips)', 4, cy0 - 3);

  // reference lane with waveform
  const [ry0, ry1] = LANE.ref;
  ctx.fillStyle = '#10151f';
  ctx.fillRect(0, ry0, S.w, ry1 - ry0);
  if (S.wave) {
    ctx.fillStyle = '#3f7d5a';
    const mid = (ry0 + ry1) / 2, half = (ry1 - ry0) / 2 - 2;
    for (let x = 0; x < S.w; x++) {
      const ta = xToTime(x), tb = xToTime(x + 1);
      let ia = Math.floor(ta * S.wavePps), ib = Math.min(Math.floor(tb * S.wavePps), S.wave.length);
      let pk = 0;
      if (ib <= ia) ib = ia + 1;
      const stride = Math.max(1, Math.floor((ib - ia) / 40));
      for (let i = ia; i < ib; i += stride) pk = Math.max(pk, S.wave[i] || 0);
      const hgt = Math.max(1, pk / 255 * half);
      ctx.fillRect(x, mid - hgt, 1, hgt * 2);
    }
  }
  ctx.strokeStyle = '#2a3040';
  ctx.strokeRect(.5, ry0 + .5, S.w - 1, ry1 - ry0 - 1);
  ctx.fillStyle = '#4a5164';
  ctx.fillText('Back Camera (reference — uncut)', 4, ry0 - 3);

  S.ctx.setTransform(1, 0, 0, 1, 0, 0);
  S.ctx.clearRect(0, 0, S.canvas.width, S.canvas.height);
  S.ctx.drawImage(S.off, 0, 0);
  drawPlayhead(S.ctx);
}

function drawPlayhead(ctx) {
  const x = timeToX(vback.currentTime) * S.dpr;
  ctx.save();
  ctx.strokeStyle = '#e5484d';
  ctx.lineWidth = Math.max(1, S.dpr);
  ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, S.canvas.height); ctx.stroke();
  ctx.restore();
}

function drawPlayheadOnly() {
  if (!S.ctx) return;
  S.ctx.setTransform(1, 0, 0, 1, 0, 0);
  S.ctx.clearRect(0, 0, S.canvas.width, S.canvas.height);
  S.ctx.drawImage(S.off, 0, 0);
  drawPlayhead(S.ctx);
}

/* ------------------------------------------------------------------ wiring */
function zoomAround(focusT, factor) {
  const span = clamp(S.view.span * factor, 8, S.dur);
  const frac = (focusT - S.view.start) / S.view.span;
  S.view.span = span;
  S.view.start = clamp(focusT - frac * span, 0, S.dur - span);
  draw();
}

function wire() {
  $('#playBtn').addEventListener('click', togglePlay);
  $('#audioSel').addEventListener('change', applyAudioSel);
  applyAudioSel();
  document.querySelectorAll('[data-jump]').forEach(b =>
    b.addEventListener('click', () => seekRef(vback.currentTime + Number(b.dataset.jump))));

  const jumpClip = (dir) => {
    const t = vback.currentTime;
    let best = null;
    if (dir > 0) {
      for (const c of S.clips) if (c.ref_in > t + 0.05) { best = c; break; }
    } else {
      for (const c of S.clips) if (c.ref_in < t - 0.5) best = c; else break;
    }
    if (best) { seekRef(best.ref_in + 0.02); focusClip(best); }
  };
  const focusClip = (c) => {
    if (c.ref_in < S.view.start || c.ref_out > S.view.start + S.view.span) {
      S.view.span = Math.max(c.dur * 4, 60);
      S.view.start = clamp(c.ref_in - S.view.span * .25, 0, S.dur - S.view.span);
    }
    draw();
  };
  $('#prevClip').addEventListener('click', () => jumpClip(-1));
  $('#nextClip').addEventListener('click', () => jumpClip(1));
  $('#zoomFit').addEventListener('click', () => { S.view = { start: 0, span: S.dur }; draw(); });
  $('#zoomIn').addEventListener('click', () => zoomAround(vback.currentTime, 0.6));
  $('#zoomOut').addEventListener('click', () => zoomAround(vback.currentTime, 1 / 0.6));

  const cv = S.canvas;
  let drag = null;
  cv.addEventListener('pointerdown', (e) => {
    const r = cv.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    // click inside a clip block jumps to that clip
    if (y >= LANE.clips[0] && y <= LANE.clips[1]) {
      const t = xToTime(x);
      const i = clipAt(t);
      if (i >= 0) { seekRef(Math.max(S.clips[i].ref_in + 0.02, t)); return; }
    }
    drag = true;
    cv.setPointerCapture(e.pointerId);
    seekRef(xToTime(x));
  });
  cv.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const r = cv.getBoundingClientRect();
    seekRef(xToTime(e.clientX - r.left));
  });
  cv.addEventListener('pointerup', () => { drag = null; });
  cv.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = cv.getBoundingClientRect();
    const focusT = xToTime(e.clientX - r.left);
    if (e.deltaY) zoomAround(focusT, e.deltaY > 0 ? 1.18 : 1 / 1.18);
  }, { passive: false });

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    else if (e.key === 'ArrowLeft') seekRef(vback.currentTime - (e.shiftKey ? 5 : 1));
    else if (e.key === 'ArrowRight') seekRef(vback.currentTime + (e.shiftKey ? 5 : 1));
    else if (e.key === '[') $('#prevClip').click();
    else if (e.key === ']') $('#nextClip').click();
    else if (e.key === '+' || e.key === '=') zoomAround(vback.currentTime, 0.6);
    else if (e.key === '-') zoomAround(vback.currentTime, 1 / 0.6);
    else if (e.key === '0') $('#zoomFit').click();
  });

  vback.addEventListener('seeked', () => chase(vback.currentTime, true));
  vback.addEventListener('play', () => { if (!S.playing) { S.playing = true; $('#playBtn').textContent = '‖ Pause'; } });
  vback.addEventListener('pause', () => { if (S.playing) { S.playing = false; v5d2.pause(); $('#playBtn').textContent = '▶︎ Play'; } });
}

boot();
