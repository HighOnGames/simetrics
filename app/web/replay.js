/* Lap replay: your car and the reference "ghost" driving the lap on the track map in real time.
   Your line paints green / red as you gain or lose time against the reference (same 20 m windows
   and 0.4 ms/m thresholds as lap_analysis.svg_map). Both laps are on the same 2 m distance grid. */
(function () {
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  function bsearch(arr, v) {
    let lo = 0, hi = arr.length - 1;
    if (v <= arr[0]) return 0;
    if (v >= arr[hi]) return hi - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (arr[m] <= v) lo = m; else hi = m; }
    return lo;
  }

  class Replay {
    constructor(root, data, opts = {}) {
      this.root = root;
      this.lap = data.lap.grid;
      this.ref = data.ref.grid;
      this.lapTime = data.lap.time;
      this.refTime = data.ref.time;
      this.corners = data.corners || [];
      this.map = data.map || null;
      this.opts = opts;
      this.T = 0; this.speed = 1; this.playing = false; this.follow = false; this.showGhost = true;
      this.n = Math.min(this.lap.d.length, this.ref.d.length);
      this.hasXY = this.lap.x && this.lap.x.length;
      this._rates();
      this._build();
      if (this.map) {
        this.img = new Image();
        this.img.onload = () => { this._static = null; this.draw(); };
        this.img.src = this.map.url;
      }
      this.ro = new ResizeObserver(() => this._resize());
      this.ro.observe(this.stage);
      this._resize();
      this.seekTime(0);
      this._loop = this._loop.bind(this);
    }

    _rates() {
      const w = 10, L = this.lap, R = this.ref, n = this.n;
      this.delta = new Float32Array(n);
      for (let i = 0; i < n; i++) this.delta[i] = L.t[i] - R.t[i];
      this.rate = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        const a = Math.max(0, i - w / 2), b = Math.min(n - 1, i + w / 2);
        const dd = (L.d[b] - L.d[a]) || 1;
        this.rate[i] = (this.delta[b] - this.delta[a]) / dd * 1000;   // ms lost per metre
      }
    }

    _build() {
      const r = this.root;
      r.innerHTML = `
        <div class="stage">
          <canvas></canvas>
          <div class="overlay">
            <div class="seg" data-k="cam"><button data-v="fit" class="on">Whole track</button><button data-v="follow">Follow car</button></div>
            <div class="seg"><button data-k="ghost" class="on" aria-pressed="true">Ghost on</button></div>
          </div>
          ${Charts.legend([{ name: "you", color: "var(--lap)", dot: 1 }, { name: "reference ghost", color: "var(--ref)", dot: 1 },
                           { name: "gaining", color: "var(--gain)" }, { name: "losing", color: "var(--lose)" }])}
        </div>
        <div class="hud">
          <div><div class="delta" data-k="delta">+0.000</div></div>
          <div class="pair">
            <div class="m"><div class="k">Speed</div><div class="v" data-k="spd">0 <small>${U.sl}</small></div><div class="r" data-k="rspd">ref 0</div></div>
            <div class="m"><div class="k">Gear</div><div class="v" data-k="gear">-</div><div class="r" data-k="rgear">ref -</div></div>
          </div>
          <div class="pedals">
            <div class="pedal"><div class="bar"><i data-k="clutch" style="background:var(--clutch)"></i></div>clutch</div>
            <div class="pedal"><div class="bar"><i data-k="brake" style="background:var(--brake)"></i><b data-k="rbrake"></b></div>brake</div>
            <div class="pedal"><div class="bar"><i data-k="gas" style="background:var(--gas)"></i><b data-k="rgas"></b></div>throttle</div>
            <div class="pedal"><svg class="wheel" viewBox="-40 -40 80 80" data-k="wheel"><circle r="32"/><path d="M-32 0H32M0 0V32"/><path class="mark" d="M0 -36V-26"/></svg><span data-k="steer">0°</span></div>
          </div>
          <div class="note">White tick on the pedal bars = the reference lap at the same spot. Clutch bar full = pedal down.</div>
          <div class="pair">
            <div class="m"><div class="k">Distance</div><div class="v" data-k="dist">0 <small>${U.dl}</small></div></div>
            <div class="m"><div class="k">Corner</div><div class="v" data-k="corner">-</div></div>
          </div>
        </div>
        <div class="controls">
          <button class="play" data-k="play" aria-label="Play">▶</button>
          <span class="clock" data-k="clock">0:00.000 / 0:00.000</span>
          <input type="range" min="0" max="1000" value="0" data-k="scrub" aria-label="Position in lap">
          <select data-k="speed" aria-label="Playback speed">
            <option value="0.25">0.25×</option><option value="0.5">0.5×</option><option value="1" selected>1×</option>
            <option value="2">2×</option><option value="4">4×</option><option value="8">8×</option>
          </select>
          <div class="corner-jump" data-k="jump">${this.corners.map((c, i) => `<button data-i="${i}" title="Jump to ${c.name}">${c.name}</button>`).join("")}</div>
        </div>`;
      this.stage = r.querySelector(".stage");
      this.canvas = r.querySelector("canvas");
      this.ctx = this.canvas.getContext("2d");
      this.$ = k => r.querySelector(`[data-k="${k}"]`);
      this.$("play").onclick = () => this.toggle();
      this.$("speed").onchange = e => { this.speed = +e.target.value; };
      this.$("scrub").oninput = e => { this.seekTime(e.target.value / 1000 * this.lapTime); };
      r.querySelectorAll('[data-k="cam"] button').forEach(b => b.onclick = () => {
        r.querySelectorAll('[data-k="cam"] button').forEach(x => x.classList.toggle("on", x === b));
        this.follow = b.dataset.v === "follow"; this.draw();
      });
      this.$("ghost").onclick = e => { this.showGhost = !this.showGhost; e.target.textContent = "Ghost " + (this.showGhost ? "on" : "off"); e.target.classList.toggle("on", this.showGhost); e.target.setAttribute("aria-pressed", this.showGhost); this.draw(); };
      this.$("jump").onclick = e => {
        const i = e.target.dataset.i; if (i == null) return;
        this.seekDistance(Math.max(0, this.corners[i].a_d - 40));
        if (this.opts.onCorner) this.opts.onCorner(+i);
      };
      this.keyHandler = e => {
        if (!document.body.contains(this.root)) { document.removeEventListener("keydown", this.keyHandler); return; }
        if (e.target.matches("input, select, textarea")) return;
        if (e.code === "Space") { e.preventDefault(); this.toggle(); }
        if (e.code === "ArrowRight") this.seekTime(this.T + 2);
        if (e.code === "ArrowLeft") this.seekTime(this.T - 2);
      };
      document.addEventListener("keydown", this.keyHandler);
    }

    _resize() {
      const dpr = window.devicePixelRatio || 1;
      const w = this.stage.clientWidth, h = this.stage.clientHeight;
      this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
      this.dpr = dpr; this.W = w; this.H = h;
      this._static = null;
      if (this.hasXY) {
        const xs = this.ref.x, zs = this.ref.z;
        let a = Infinity, b = -Infinity, c = Infinity, d = -Infinity;
        for (let i = 0; i < xs.length; i++) { a = Math.min(a, xs[i]); b = Math.max(b, xs[i]); c = Math.min(c, zs[i]); d = Math.max(d, zs[i]); }
        this.bounds = { minx: a, maxx: b, minz: c, maxz: d };
      }
      this.draw();
    }

    // ---------------------------------------------------------- playback
    toggle() { this.playing ? this.pause() : this.play(); }
    play() {
      if (this.T >= this.lapTime - 0.01) this.T = 0;
      this.playing = true; this.$("play").textContent = "❚❚"; this.$("play").setAttribute("aria-label", "Pause");
      this.last = performance.now(); requestAnimationFrame(this._loop);
    }
    pause() { this.playing = false; this.$("play").textContent = "▶"; this.$("play").setAttribute("aria-label", "Play"); }
    _loop(now) {
      if (!this.playing || !document.body.contains(this.root)) return;
      const dt = Math.min(0.1, (now - this.last) / 1000); this.last = now;
      this.T += dt * this.speed;
      if (this.T >= this.lapTime) { this.T = this.lapTime; this.pause(); }
      this.update();
      if (this.playing) requestAnimationFrame(this._loop);
    }
    seekTime(T) { this.T = Math.max(0, Math.min(this.lapTime, T)); this.update(); }
    seekDistance(d) {
      const i = bsearch(this.lap.d, d);
      this.seekTime(this.lap.t[i]);
    }

    stateAt(g, T) {
      const i = bsearch(g.t, T);
      const t0 = g.t[i], t1 = g.t[i + 1];
      const f = t1 > t0 ? Math.max(0, Math.min(1, (T - t0) / (t1 - t0))) : 0;
      const lerp = k => g[k] ? g[k][i] + (g[k][i + 1] - g[k][i]) * f : null;
      return { i, f, d: lerp("d"), x: lerp("x"), z: lerp("z"), mph: lerp("speed_mph"), gas: lerp("gas"), brake: lerp("brake"),
               clutch: g.clutch ? g.clutch[i] : null, steer: lerp("steer"), gear: g.gear ? g.gear[i] : null };
    }

    update() {
      const L = this.stateAt(this.lap, this.T);
      const G = this.stateAt(this.ref, Math.min(this.T, this.refTime));
      // reference at the same distance as you (for the delta and the pedal ticks)
      const i = Math.min(L.i, this.n - 2), f = L.f;
      const refT = this.ref.t[i] + (this.ref.t[i + 1] - this.ref.t[i]) * f;
      const delta = this.T - refT;
      const at = k => this.ref[k] ? this.ref[k][i] + (this.ref[k][i + 1] - this.ref[k][i]) * f : 0;
      this.cur = { L, G, delta };
      const dEl = this.$("delta");
      dEl.innerHTML = `${delta >= 0 ? "+" : "−"}${Math.abs(delta).toFixed(3)}<small>${delta >= 0.0005 ? "behind" : delta <= -0.0005 ? "ahead of" : "level with"} the reference</small>`;
      dEl.style.color = delta > 0.02 ? "var(--bad-ink)" : delta < -0.02 ? "var(--good-ink)" : "var(--ink)";
      this.$("spd").innerHTML = `${Math.round(U.fromMph(L.mph))} <small>${U.sl}</small>`;
      this.$("rspd").textContent = `ref ${Math.round(U.fromMph(at("speed_mph")))} ${U.sl} here`;
      this.$("gear").textContent = L.gear == null ? "-" : L.gear === 0 ? "N" : L.gear < 0 ? "R" : L.gear;
      const rg = this.ref.gear ? this.ref.gear[i] : null;
      this.$("rgear").textContent = "ref " + (rg == null ? "-" : rg === 0 ? "N" : rg < 0 ? "R" : rg);
      const pct = v => Math.round(Math.max(0, Math.min(1, v || 0)) * 100) + "%";
      this.$("gas").style.height = pct(L.gas); this.$("brake").style.height = pct(L.brake);
      this.$("clutch").style.height = L.clutch == null || isNaN(L.clutch) ? "0%" : pct(1 - L.clutch);
      this.$("rgas").style.bottom = pct(at("gas")); this.$("rbrake").style.bottom = pct(at("brake"));
      const st = L.steer || 0;
      this.$("wheel").style.transform = `rotate(${st}deg)`;
      this.$("steer").textContent = `${Math.round(st)}°`;
      this.$("dist").innerHTML = `${Math.round(U.dv(L.d))} <small>${U.dl}</small>`;
      const cn = this.corners.find(c => L.d >= c.a_d && L.d <= c.b_d);
      this.$("corner").textContent = cn ? cn.name : "-";
      this.root.querySelectorAll('[data-k="jump"] button').forEach((b, k) => b.classList.toggle("on", cn && this.corners[k] === cn));
      this.$("clock").textContent = `${fmtTime(this.T)} / ${fmtTime(this.lapTime)}`;
      this.$("scrub").value = Math.round(this.T / this.lapTime * 1000);
      this.draw();
      if (this.opts.onTick) this.opts.onTick(L.d, this.T);
    }

    // ---------------------------------------------------------- drawing
    _view() {
      const pad = 36, b = this.bounds;
      const spanx = b.maxx - b.minx || 1, spanz = b.maxz - b.minz || 1;
      let s = Math.min((this.W - 2 * pad) / spanx, (this.H - 2 * pad) / spanz);
      let cx = (b.minx + b.maxx) / 2, cz = (b.minz + b.maxz) / 2;
      if (this.follow && this.cur) { s *= 3.2; cx = this.cur.L.x; cz = this.cur.L.z; }
      return { s, ox: this.W / 2 - cx * s, oz: this.H / 2 - cz * s };
    }

    draw() {
      const ctx = this.ctx;
      if (!ctx) return;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.W, this.H);
      if (!this.hasXY) {
        ctx.fillStyle = css("--muted"); ctx.font = "14px system-ui"; ctx.fillText("This lap has no position data for a map.", 20, 40);
        return;
      }
      const v = this._view();
      const P = (x, z) => [v.ox + x * v.s, v.oz + z * v.s];
      if (this.img && this.img.complete && this.img.naturalWidth && this.map.ini) {
        // AC map.png: pixel = (world + OFFSET) / SCALE_FACTOR
        const m = this.map.ini, sf = m.SCALE_FACTOR || 1;
        const [x0, y0] = P(-m.X_OFFSET * sf, -m.Z_OFFSET * sf);
        ctx.globalAlpha = 0.18;
        ctx.drawImage(this.img, x0, y0, this.img.naturalWidth * sf * v.s, this.img.naturalHeight * sf * v.s);
        ctx.globalAlpha = 1;
      }
      const R = this.ref, L = this.lap;
      // track ribbon from the reference lap
      ctx.lineJoin = "round"; ctx.lineCap = "round";
      ctx.beginPath();
      for (let i = 0; i < R.x.length; i++) { const [x, y] = P(R.x[i], R.z[i]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
      ctx.strokeStyle = css("--surface-3"); ctx.lineWidth = this.follow ? 22 : 12; ctx.stroke();
      ctx.strokeStyle = css("--axis"); ctx.lineWidth = 1; ctx.stroke();
      // your line, painted up to where you are
      const upto = this.cur ? this.cur.L.i : 0;
      const lose = css("--lose"), gain = css("--gain"), even = css("--even");
      ctx.lineWidth = this.follow ? 6 : 4;
      for (let i = 0; i < Math.min(upto, this.n - 1); i++) {
        const r = this.rate[i];
        const col = r > 0.4 ? lose : r < -0.4 ? gain : even;
        ctx.globalAlpha = r > 0.4 || r < -0.4 ? Math.min(1, 0.55 + Math.abs(r) / 6) : 0.9;
        ctx.strokeStyle = col;
        const [x0, y0] = P(L.x[i], L.z[i]), [x1, y1] = P(L.x[i + 1], L.z[i + 1]);
        ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      // start / finish
      { const [x, y] = P(R.x[0], R.z[0]); ctx.fillStyle = css("--ink"); ctx.fillRect(x - 5, y - 5, 10, 10); ctx.fillStyle = css("--bg"); ctx.fillRect(x - 5, y - 5, 5, 5); ctx.fillRect(x, y, 5, 5); }
      // corner labels
      ctx.font = "600 12px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      for (const c of this.corners) {
        const k = Math.min(R.x.length - 1, Math.round(c.apex_d / 2));
        const [x, y] = P(R.x[k], R.z[k]);
        ctx.fillStyle = css("--surface"); ctx.beginPath(); ctx.arc(x, y - 18, 11, 0, 7); ctx.fill();
        ctx.strokeStyle = css("--axis"); ctx.lineWidth = 1; ctx.stroke();
        ctx.fillStyle = css("--ink-2"); ctx.fillText(c.name, x, y - 18);
      }
      if (!this.cur) return;
      // ghost
      if (this.showGhost) {
        const G = this.cur.G; const [x, y] = P(G.x, G.z);
        ctx.beginPath(); ctx.arc(x, y, 7, 0, 7); ctx.fillStyle = css("--surface"); ctx.fill();
        ctx.lineWidth = 2.5; ctx.strokeStyle = css("--ref"); ctx.stroke();
      }
      // you, with heading
      const Lc = this.cur.L; const [x, y] = P(Lc.x, Lc.z);
      const j = Math.min(L.x.length - 1, Lc.i + 3);
      const hx = L.x[j] - L.x[Math.max(0, Lc.i - 1)], hz = L.z[j] - L.z[Math.max(0, Lc.i - 1)];
      const a = Math.atan2(hz, hx);
      ctx.save(); ctx.translate(x, y); ctx.rotate(a);
      ctx.beginPath(); ctx.moveTo(13, 0); ctx.lineTo(-7, 7); ctx.lineTo(-4, 0); ctx.lineTo(-7, -7); ctx.closePath();
      ctx.fillStyle = css("--lap"); ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = css("--surface"); ctx.stroke();
      ctx.restore();
    }
  }

  function fmtTime(s) {
    if (s == null || !isFinite(s)) return "-";
    const m = Math.floor(s / 60), r = s - m * 60;
    return `${m}:${r.toFixed(3).padStart(6, "0")}`;
  }

  window.Replay = Replay;
  window.fmtTime = fmtTime;
})();
