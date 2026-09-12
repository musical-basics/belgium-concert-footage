/* Synced multi-camera playback.
 *
 * A direct port of the editor's sync clock (editor/app.js `syncFollowers`):
 * one master video carries the clock and the audio, the others are muted
 * followers that get hard-corrected past 60ms of drift and nudged with
 * playbackRate below it. That tolerance is what the editor has used all along —
 * it is watchable, not frame-locked.
 *
 * The angles are the full 96-minute concert proxies, seeked to the reel's start
 * rather than cut: they are faststart mp4s on R2, so the browser range-requests
 * its way to the song instead of downloading what precedes it.
 */
export const HARD_DRIFT = 0.06;   // seconds — beyond this, snap instead of nudge
const DEADBAND = 0.008;          // close enough; stop correcting and avoid hunting
// Followers are muted, so rate changes cost nothing audibly and can be far more
// aggressive than the editor's fixed 0.96/1.04. A fixed nudge that weak never
// actually closed the gap here: decode latency holds the followers ~50ms behind
// and the correction only cancels the growth, so it hovers instead of settling.
// Proportional control converges and then backs off to 1.0 on its own.
const GAIN = 3;
const MAX_RATE = 0.12;

export class MultiCam {
  /** @param {HTMLElement} mount  @param {object} cfg  parsed multicam.json */
  constructor(mount, cfg) {
    this.mount = mount;
    this.cfg = cfg;
    this.videos = [];
    this.master = null;
    this.raf = null;
    this.onTick = null;
    this.range = null;
  }

  /** Angles to actually decode. Three concurrent HD decodes is more than a
   *  phone should be asked for, so narrow screens get one pane and a picker. */
  static solo() {
    return window.matchMedia("(max-width: 780px)").matches;
  }

  async open(range, angleId = null) {
    this.range = range;
    const solo = MultiCam.solo();
    const angles = solo
      ? [this.cfg.angles.find((a) => a.id === (angleId ?? this.cfg.angles[0].id))]
      : this.cfg.angles;

    this.mount.innerHTML = "";
    this.videos = [];
    for (const a of angles) {
      const pane = document.createElement("div");
      pane.className = "pane";
      const v = document.createElement("video");
      v.src = `${this.cfg.base}/${a.file}`;
      v.preload = "auto";
      v.playsInline = true;
      // Only the audio-bed camera is audible; the rest would phase against it.
      v.muted = solo ? !a.audio : !a.audio;
      v.dataset.angle = a.id;
      pane.innerHTML = `<span class="plabel">${a.label}${a.audio ? " ♪" : ""}</span>`;
      pane.appendChild(v);
      this.mount.appendChild(pane);
      this.videos.push(v);
      if (a.audio) this.master = v;
    }
    // Solo mode may not include the audio camera; whatever is on screen leads.
    if (!this.master || !this.videos.includes(this.master)) {
      this.master = this.videos[0];
      this.master.muted = false;
    }

    await this.seekAll(range.in);
    return this;
  }

  /** Resolves once every pane has actually landed on the frame and buffered
   *  enough to play. Starting before that is the main source of visible drift. */
  seekAll(t) {
    return Promise.all(
      this.videos.map(
        (v) =>
          new Promise((resolve) => {
            let settled = false;
            const done = () => {
              if (settled) return;
              if (v.readyState < 3) return;         // HAVE_FUTURE_DATA
              settled = true;
              v.removeEventListener("seeked", done);
              v.removeEventListener("canplay", done);
              resolve();
            };
            v.addEventListener("seeked", done);
            v.addEventListener("canplay", done);
            v.currentTime = t;
            // A pane that never buffers must not hang the whole view.
            setTimeout(() => { settled = true; resolve(); }, 15000);
          }),
      ),
    );
  }

  async play() {
    await Promise.allSettled(this.videos.map((v) => v.play()));
    this.loop();
  }

  pause() {
    this.videos.forEach((v) => { v.pause(); v.playbackRate = 1; });
    cancelAnimationFrame(this.raf);
    this.raf = null;
  }

  get playing() {
    return this.master && !this.master.paused;
  }

  loop() {
    cancelAnimationFrame(this.raf);
    const step = () => {
      this.sync();
      const t = this.master ? this.master.currentTime : 0;
      this.onTick?.(t);
      if (this.range && t >= this.range.out) { this.pause(); this.onTick?.(this.range.out, true); return; }
      this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }

  sync() {
    if (!this.master) return;
    const t = this.master.currentTime;
    for (const v of this.videos) {
      if (v === this.master) continue;
      const drift = v.currentTime - t;
      if (Math.abs(drift) > HARD_DRIFT) {
        // Too far to ride back — snap, and accept the visible jump.
        v.currentTime = t;
        v.playbackRate = 1;
      } else if (Math.abs(drift) < DEADBAND) {
        v.playbackRate = 1;
      } else {
        const adj = Math.max(-MAX_RATE, Math.min(MAX_RATE, -drift * GAIN));
        v.playbackRate = 1 + adj;
      }
      if (this.playing && v.paused) v.play().catch(() => {});
    }
  }

  async scrub(t) {
    const was = this.playing;
    this.pause();
    await this.seekAll(t);
    if (was) await this.play();
    else this.onTick?.(t);
  }

  destroy() {
    this.pause();
    // Dropping the src is what actually stops the range requests; leaving the
    // elements attached keeps three downloads alive behind a closed dialog.
    for (const v of this.videos) { v.removeAttribute("src"); v.load(); }
    this.videos = [];
    this.master = null;
    this.mount.innerHTML = "";
  }
}
