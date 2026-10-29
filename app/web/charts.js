/* Small SVG chart kit: line/dot charts with crosshair tooltip, threshold line, corner bands,
   playhead and linked cursors. Thin 2px lines, recessive grid, text in ink tokens. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const tip = () => document.getElementById("tip");

  function niceTicks(lo, hi, n) {
    if (!(hi > lo)) { hi = lo + 1; }
    const span = hi - lo, step0 = span / Math.max(1, n);
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= step0) || 10 * mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }

  function showTip(html, ev) {
    const t = tip();
    t.innerHTML = html;
    t.hidden = false;
    const r = t.getBoundingClientRect();
    let x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
    t.style.left = x + "px"; t.style.top = y + "px";
  }
  function hideTip() { tip().hidden = true; }

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }

  function nearestIndex(pts, x) {
    let lo = 0, hi = pts.length - 1;
    if (hi < 0) return -1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (pts[m][0] < x) lo = m; else hi = m; }
    return Math.abs(pts[lo][0] - x) <= Math.abs(pts[hi][0] - x) ? lo : hi;
  }

  /* opts: {height, title, series:[{name,color,points:[[x,y,meta]],dash,dots,area,step,width}],
            xMin,xMax,yMin,yMax,zero,xFmt,yFmt,xTicks(n),threshold:{y,label},bands:[{x0,x1,label}],
            tooltip(x, hits) -> html, onHover(x|null), onClick(x), padL, cat (x labels for categorical)} */
  function line(host, opts) {
    host.classList.add("chart");
    const state = { cursor: null, playhead: null, xMin: opts.xMin, xMax: opts.xMax };
    let svg, scales;

    function render() {
      const W = Math.max(200, host.clientWidth || 600), H = opts.height || 160;
      const padL = opts.padL || 44, padR = 10, padT = opts.title ? 22 : 8, padB = opts.noXAxis ? 6 : 22;
      const all = opts.series.flatMap(s => s.points.filter(p => p[1] != null && isFinite(p[1])));
      let xMin = state.xMin ?? Math.min(...all.map(p => p[0])), xMax = state.xMax ?? Math.max(...all.map(p => p[0]));
      if (!(xMax > xMin)) { xMax = xMin + 1; }
      const vis = all.filter(p => p[0] >= xMin && p[0] <= xMax);
      let yMin = opts.yMin ?? Math.min(...vis.map(p => p[1])), yMax = opts.yMax ?? Math.max(...vis.map(p => p[1]));
      if (opts.threshold && opts.threshold.y != null) { yMin = Math.min(yMin, opts.threshold.y); yMax = Math.max(yMax, opts.threshold.y); }
      if (opts.zero) { yMin = Math.min(yMin, 0); yMax = Math.max(yMax, 0); }
      if (!isFinite(yMin)) { yMin = 0; yMax = 1; }
      if (yMax - yMin < 1e-9) { yMax = yMin + (Math.abs(yMin) || 1) * 0.1 + 0.01; yMin -= (yMax - yMin) * 0.5; }
      if (opts.yMin == null || opts.yMax == null) { const pad = (yMax - yMin) * 0.08; if (opts.yMin == null) yMin -= pad; if (opts.yMax == null) yMax += pad; }
      const X = x => padL + (W - padL - padR) * (x - xMin) / (xMax - xMin);
      const Y = y => padT + (H - padT - padB) * (1 - (y - yMin) / (yMax - yMin));
      scales = { X, Y, xMin, xMax, W, H, padL, padR, padT, padB };
      host.innerHTML = "";
      svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.title || "chart" }, host);
      const defs = el("defs", {}, svg);
      const cp = el("clipPath", { id: "c" + Math.random().toString(36).slice(2) }, defs);
      el("rect", { x: padL, y: 0, width: W - padL - padR, height: H }, cp);
      if (opts.title) { const t = el("text", { x: padL, y: 13, class: "ttl" }, svg); t.textContent = opts.title; }
      for (const b of opts.bands || []) {
        if (b.x1 < xMin || b.x0 > xMax) continue;
        const x0 = X(Math.max(b.x0, xMin)), x1 = X(Math.min(b.x1, xMax));
        el("rect", { x: x0, y: padT, width: Math.max(0, x1 - x0), height: H - padT - padB, class: "corner-band" }, svg);
        if (!opts.noBandLabels && x1 - x0 >= (b.label || "").length * 6.5 + 6) { const t = el("text", { x: (x0 + x1) / 2, y: padT + 10, "text-anchor": "middle", class: "corner-l" }, svg); t.textContent = b.label; }
      }
      for (const v of niceTicks(yMin, yMax, Math.max(2, Math.round((H - padT - padB) / 34)))) {
        el("line", { x1: padL, x2: W - padR, y1: Y(v), y2: Y(v), class: "gridline" }, svg);
        const t = el("text", { x: padL - 6, y: Y(v) + 3.5, "text-anchor": "end" }, svg);
        t.textContent = opts.yFmt ? opts.yFmt(v) : v;
      }
      if (opts.zero && yMin < 0 && yMax > 0) el("line", { x1: padL, x2: W - padR, y1: Y(0), y2: Y(0), class: "axis" }, svg);
      if (!opts.noXAxis) {
        el("line", { x1: padL, x2: W - padR, y1: H - padB, y2: H - padB, class: "axis" }, svg);
        const ticks = opts.cat ? opts.cat.map((_, i) => i).filter(i => i >= xMin && i <= xMax)
          : niceTicks(xMin, xMax, Math.max(2, Math.round((W - padL) / 90))).filter(v => !opts.xInt || Number.isInteger(v));
        const every = opts.cat ? Math.max(1, Math.ceil(ticks.length / Math.max(1, Math.floor((W - padL) / 80)))) : 1;
        ticks.forEach((v, i) => {
          if (i % every) return;
          const t = el("text", { x: X(v), y: H - 6, "text-anchor": "middle" }, svg);
          t.textContent = opts.cat ? opts.cat[v] : (opts.xFmt ? opts.xFmt(v) : v);
        });
      }
      if (opts.threshold && opts.threshold.y != null) {
        el("line", { x1: padL, x2: W - padR, y1: Y(opts.threshold.y), y2: Y(opts.threshold.y), class: "thresh" }, svg);
        if (opts.threshold.label) { const t = el("text", { x: W - padR - 2, y: Y(opts.threshold.y) - 4, "text-anchor": "end", class: "thresh-l" }, svg); t.textContent = opts.threshold.label; }
      }
      const g = el("g", { "clip-path": `url(#${cp.id})` }, svg);
      for (const s of opts.series) {
        const pts = s.points.filter(p => p[1] != null && isFinite(p[1]));
        if (!pts.length) continue;
        if (s.dotsOnly !== true) {
          let d = "";
          let prev = null;
          for (const p of pts) {
            if (p[0] < xMin - (xMax - xMin) * 0.02 && prev && prev[0] < xMin) { prev = p; continue; }
            const cmd = d ? "L" : "M";
            if (s.step && prev) d += `L${X(p[0]).toFixed(1)},${Y(prev[1]).toFixed(1)}`;
            d += `${cmd}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`;
            prev = p;
            if (p[0] > xMax + (xMax - xMin) * 0.02) break;
          }
          if (s.area) {
            const base = Y(Math.max(yMin, Math.min(yMax, s.areaBase ?? 0)));
            el("path", { d: d + `L${X(pts[pts.length - 1][0])},${base}L${X(pts[0][0])},${base}Z`, class: "area", fill: s.color }, g);
          }
          el("path", { d, class: "line" + (s.dash ? " dash" : ""), stroke: s.color, "stroke-width": s.width || (s.dash ? 1.6 : 2) }, g);
        }
        if (s.dots || s.dotsOnly) {
          for (const p of pts) {
            if (p[0] < xMin || p[0] > xMax) continue;
            el("circle", { cx: X(p[0]), cy: Y(p[1]), r: s.r || 4, class: "dot", fill: s.hollow && s.hollow(p) ? "var(--surface)" : s.color,
              stroke: s.hollow && s.hollow(p) ? s.color : "var(--surface)", "stroke-width": 2 }, g);
          }
        }
      }
      state.cursorLine = el("line", { y1: padT, y2: H - padB, class: "cursor", visibility: "hidden" }, svg);
      state.playLine = el("line", { y1: padT, y2: H - padB, class: "playhead", visibility: "hidden" }, svg);
      const hit = el("rect", { x: padL, y: 0, width: W - padL - padR, height: H, fill: "transparent", style: "cursor:crosshair" }, svg);
      hit.addEventListener("mousemove", ev => {
        const r = svg.getBoundingClientRect();
        const px = (ev.clientX - r.left) * (W / r.width);
        let xv = xMin + (px - padL) / (W - padL - padR) * (xMax - xMin);
        if (opts.cat) xv = Math.round(xv);
        setCursor(xv);
        if (opts.onHover) opts.onHover(xv);
        const hits = opts.series.map(s => { const i = nearestIndex(s.points, xv); return i < 0 ? null : { s, p: s.points[i] }; }).filter(Boolean);
        const html = opts.tooltip ? opts.tooltip(xv, hits) : defaultTip(xv, hits, opts);
        if (html) showTip(html, ev); else hideTip();
      });
      hit.addEventListener("mouseleave", () => { setCursor(null); hideTip(); if (opts.onHover) opts.onHover(null); });
      if (opts.onClick) hit.addEventListener("click", ev => {
        const r = svg.getBoundingClientRect();
        const px = (ev.clientX - r.left) * (W / r.width);
        opts.onClick(xMin + (px - padL) / (W - padL - padR) * (xMax - xMin));
      });
      setCursor(state.cursor); setPlayhead(state.playhead);
    }

    function setCursor(x) {
      state.cursor = x;
      if (!state.cursorLine || !scales) return;
      if (x == null || x < scales.xMin || x > scales.xMax) { state.cursorLine.setAttribute("visibility", "hidden"); return; }
      state.cursorLine.setAttribute("x1", scales.X(x)); state.cursorLine.setAttribute("x2", scales.X(x));
      state.cursorLine.setAttribute("visibility", "visible");
    }
    function setPlayhead(x) {
      state.playhead = x;
      if (!state.playLine || !scales) return;
      if (x == null || x < scales.xMin || x > scales.xMax) { state.playLine.setAttribute("visibility", "hidden"); return; }
      state.playLine.setAttribute("x1", scales.X(x)); state.playLine.setAttribute("x2", scales.X(x));
      state.playLine.setAttribute("visibility", "visible");
    }
    render();
    const ro = new ResizeObserver(() => render());
    ro.observe(host);
    return {
      setCursor, setPlayhead,
      setXRange(a, b) { state.xMin = a; state.xMax = b; render(); },
      rerender: render,
    };
  }

  function defaultTip(x, hits, opts) {
    if (!hits.length) return "";
    const head = opts.cat ? opts.cat[x] : (opts.xFmt ? opts.xFmt(x) : x);
    return `<div class="tt">${head ?? ""}</div>` + hits.map(h =>
      `<div class="tr"><span><i class="sw" style="background:${h.s.color}"></i>${h.s.name}</span><span>${opts.yFmt ? opts.yFmt(h.p[1]) : h.p[1]}</span></div>`).join("");
  }

  function legend(items) {
    return `<div class="legend">${items.map(i => `<span><i class="${i.dash ? "dash" : ""} ${i.dot ? "dot" : ""}" style="background:${i.color}"></i>${i.name}</span>`).join("")}</div>`;
  }

  /* Track outline as an inline SVG string from [[x,z],...] (AC frame: x right, z down). */
  function outline(points, opts = {}) {
    if (!points || points.length < 3) return "";
    const xs = points.map(p => p[0]), zs = points.map(p => p[1]);
    const minx = Math.min(...xs), maxx = Math.max(...xs), minz = Math.min(...zs), maxz = Math.max(...zs);
    const span = Math.max(maxx - minx, maxz - minz) || 1;
    const w = (maxx - minx) / span * 100, h = (maxz - minz) / span * 100;
    const d = points.map((p, i) => `${i ? "L" : "M"}${((p[0] - minx) / span * 100).toFixed(2)},${((p[1] - minz) / span * 100).toFixed(2)}`).join("") + (opts.closed ? "Z" : "");
    const s = points[0];
    return `<svg class="outline ${opts.cls || ""}" viewBox="${-4} ${-4} ${w + 8} ${h + 8}" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
      <path d="${d}"/><circle class="start" cx="${((s[0] - minx) / span * 100).toFixed(2)}" cy="${((s[1] - minz) / span * 100).toFixed(2)}" r="2.6"/></svg>`;
  }

  window.Charts = { line, legend, outline, showTip, hideTip, niceTicks };
})();
