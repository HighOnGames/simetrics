/* Simetrics UI: library -> car@track -> lap replay, plus habits & trends.
   Units come from settings (U). All numbers come from the Python engine (lap_analysis.py). */
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const view = $("#view");
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k, d) { try { const v = localStorage.getItem("lls." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("lls." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  const api = async (path, body) => {
    const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  };
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const CAT = () => [css("--lap"), css("--cat2"), css("--gas")];       // first three categorical slots (validated all-pairs)
  // game modules (modules/<id>/module.json via /api/index): names, colours, filters, wording
  const MOD = g => (INDEX && INDEX.modules && INDEX.modules[g]) || { id: g, name: g, short: String(g).toUpperCase(), filters: {}, ui: {} };
  const GAME_COLOR = g => `var(--${g}, var(--lap))`;                   // --<id> comes from /modules.css
  const classColor = c => ((MOD(c.game).ui || {}).class_colors || {})[c.car_class] || "var(--surface-3)";

  const fmtDelta = (s, d = 3) => s == null || !isFinite(s) ? "-" : (s >= 0 ? "+" : "−") + Math.abs(s).toFixed(d);
  const fmtNum = (v, d = 0) => v == null || !isFinite(v) ? "-" : (+v).toFixed(d);
  const DAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDate = (t, time = true) => { if (!t) return "-"; const d = new Date(t * 1000); return `${DAY[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]}` + (time ? `, ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}` : ""); };
  const fmtShort = t => { const d = new Date(t * 1000); return `${d.getDate()} ${MON[d.getMonth()]} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
  const ago = t => { if (!t) return "never"; const s = Date.now() / 1000 - t; if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min ago"; if (s < 86400) return Math.round(s / 3600) + " h ago"; const d = Math.round(s / 86400); return d === 1 ? "yesterday" : d + " days ago"; };
  const gearTxt = g => g == null ? "-" : g === 0 ? "N" : g < 0 ? "R" : String(g);

  // input devices (lap_analysis.DEVICES), in display order: wheel first, then controller, keyboard & mouse
  const DEVICES = ["wheel", "gamepad", "keyboard", "unknown"];
  const DEV_LABEL = { wheel: "Wheel", gamepad: "Controller", keyboard: "Keyboard & mouse", unknown: "Unknown device" };
  const DEV_SOURCE = { game_config: "from the game's input settings", setting: "set in the logger", heuristic: "detected from the lap's data",
                       user: "tagged by hand", none: "not recorded" };
  const devOf = x => DEVICES.includes(x && x.device) ? x.device : "unknown";
  const devChip = (d, src, detail) => `<span class="dev dev-${esc(d)}" title="Input device${detail ? ": " + esc(detail) : ""}${src ? " (" + esc(DEV_SOURCE[src] || src) + ")" : ""}">${esc(DEV_LABEL[d] || d)}</span>`;
  const isWheelish = d => d === "wheel" || d === "unknown";             // steering in real degrees
  // peak steering: degrees on a wheel, % of full lock on a controller or keyboard (a stick has no wheel angle)
  const steerTxt = (m, pct) => pct ? (m.steer_max_pct == null ? "-" : fmtNum(m.steer_max_pct) + "%") : (m.steer_max == null ? "-" : fmtNum(m.steer_max) + "°");

  // Timing validity, observed track limits and driving incidents are separate measurements.
  // In particular, null validity on an older recording must never be shown as "invalid".
  const validityOf = l => l?.validity?.status || (l?.valid === false ? "invalid" : "unverified");
  const validityLabel = l => l?.validity?.label || ({ observed_clear: "Observed clear", rewind_free: "No rewind recorded (track limits unavailable)",
    invalid: "Invalid recorded", off_track: "Off track observed", penalty: "Game penalty observed", unverified: "Validity unverified" }[validityOf(l)] || "Validity unverified");
  const observedClear = l => ["observed_clear", "rewind_free"].includes(validityOf(l));
  const paceEligible = l => typeof l?.pace_eligible === "boolean" ? l.pace_eligible : l?.valid !== false;
  const flaggedLap = l => ["invalid", "off_track", "penalty"].includes(validityOf(l)) || l?.pace_eligible === false;
  const offTrackDetails = l => {
    const v = l?.validity || {};
    if (!v.track_limits_source || v.off_track_events == null) return "Off-track event count and duration unavailable";
    const duration = v.off_track_s == null ? "duration unavailable" : `${fmtNum(v.off_track_s, 2)} s outside observed`;
    return `${v.off_track_events} off-track events observed; ${duration}; ${v.track_limits_coverage || "unknown"} monitoring; source: ${v.track_limits_source}`;
  };
  const validityBadge = l => {
    const v = l?.validity || {}, details = offTrackDetails(l);
    const compact = {observed_clear: "No issue observed", unverified: "Unverified", off_track: "Off track observed",
      invalid: "Game invalid", penalty: "Penalty observed", rewind_free: "No rewind recorded"}[validityOf(l)] || "Unverified";
    const explanation = `${validityLabel(l)}. ${details}. Game-flag monitoring: ${v.game_validity_coverage || "unavailable"}`;
    const events = v.off_track_events == null ? "off track observed" : `${v.off_track_events} off-track event${v.off_track_events === 1 ? "" : "s"}${v.off_track_s == null ? "" : ` · ${fmtNum(v.off_track_s, 2)} s`}`;
    return `<span class="flag${flaggedLap(l) ? " bad" : ""}" title="${esc(explanation)}">${esc(compact)}</span>`
      + (v.off_track === true ? `<span class="flag bad" title="${esc(details)}">${esc(events)}</span>` : "")
      + (v.penalty_observed === true && validityOf(l) !== "penalty" ? '<span class="flag bad">game penalty observed</span>' : "");
  };
  const statusSummary = laps => {
    const counts = {};
    for (const l of laps) counts[validityOf(l)] = (counts[validityOf(l)] || 0) + 1;
    const primary = [["observed_clear", "observed clear"], ["rewind_free", "without rewind (track limits unavailable)"],
      ["invalid", "invalid"], ["off_track", "off track"], ["penalty", "with game penalty"], ["unverified", "validity unverified"]]
      .filter(([key]) => counts[key]).map(([key, label]) => `${counts[key]} ${label}`).join(" · ");
    const observedOutside = laps.filter(l => l.validity?.off_track === true).length;
    const unknownOutside = laps.filter(l => l.validity?.off_track == null).length;
    const outside = unknownOutside === laps.length ? "off-track observations unavailable" : `${observedOutside} laps with off-track observed${unknownOutside ? ` · ${unknownOutside} off-track histories unverified` : ""}`;
    const penalties = laps.filter(l => l.validity?.penalty_observed === true).length;
    return `Primary status: ${primary}. Independent observations: ${outside}${penalties ? ` · ${penalties} laps with game penalty` : ""} (counts can overlap)`;
  };
  const referenceLap = laps => {
    const eligible = laps.filter(paceEligible), pool = eligible.length ? eligible : laps;
    return pool.slice().sort((x, y) => Number(observedClear(y)) - Number(observedClear(x)) || x.time - y.time)[0];
  };
  const comparisonRows = (session, laps) => session?.population?.files
    ? laps.filter(l => session.population.files.includes(l.file)) : laps.filter(paceEligible);

  // display units (settings ⚙). Data stays in km/h / mph channels and metres; only what's shown changes.
  const U = {
    speed: "kmh", distance: "m",
    set(u) { if (u) { this.speed = u.speed; this.distance = u.distance; } },
    get sl() { return this.speed === "mph" ? "mph" : "km/h"; },
    get dl() { return this.distance === "ft" ? "ft" : "m"; },
    fromMph(v) { return v == null ? null : this.speed === "mph" ? v : v * 1.609344; },
    fromKmh(v) { return v == null ? null : this.speed === "mph" ? v / 1.609344 : v; },
    dv(m) { return m == null ? null : this.distance === "ft" ? m * 3.28084 : m; },
    mph(v, d = 0) { return fmtNum(this.fromMph(v), d); },          // number only, input in mph
    kmh(v, d = 0) { return fmtNum(this.fromKmh(v), d); },          // number only, input in km/h
    dist(m, d = 0) { return m == null ? "-" : fmtNum(this.dv(m), d) + " " + this.dl; },
    // lap_analysis texts carry "12 m" / "5 mph": convert them for display
    text(s) {
      return String(s).replace(/(\d+(?:\.\d+)?) (mph|km\/h)/g, (_, n, u) => `${Math.round(u === "mph" ? this.fromMph(+n) : this.fromKmh(+n))} ${this.sl}`)
        .replace(/(\d+(?:\.\d+)?) m\b/g, (_, n) => `${Math.round(this.dv(+n))} ${this.dl}`);
    },
  };
  window.U = U;

  function settingsDialog() {
    const dlg = $("#dlg");
    let sp = U.speed, di = U.distance;
    dlg.innerHTML = `<form method="dialog">
      <div class="dh"><h2>Settings</h2><div class="note">Units change what the app shows and what the AI prompt uses. Your lap files are never changed.</div></div>
      <div class="db">
        <div class="row"><span class="note" style="min-width:86px">Preset</span><div class="seg" id="s-pre">
          <button type="button" data-v="kmh,m">Metric</button><button type="button" data-v="mph,ft">Imperial</button><button type="button" data-v="mph,m">mph + metres</button></div></div>
        <div class="row"><span class="note" style="min-width:86px">Speed</span><div class="seg" id="s-sp"><button type="button" data-v="kmh">km/h</button><button type="button" data-v="mph">mph</button></div></div>
        <div class="row"><span class="note" style="min-width:86px">Distance</span><div class="seg" id="s-di"><button type="button" data-v="m">metres</button><button type="button" data-v="ft">feet</button></div></div>
        <div class="note">Lap data folder: <code>${esc(INDEX ? INDEX.data_dir : "")}</code></div>
      </div>
      <div class="df"><button class="btn" value="cancel">Cancel</button><button class="btn primary" value="save">Save</button></div></form>`;
    const paint = () => {
      $$("#s-sp button", dlg).forEach(b => b.classList.toggle("on", b.dataset.v === sp));
      $$("#s-di button", dlg).forEach(b => b.classList.toggle("on", b.dataset.v === di));
      $$("#s-pre button", dlg).forEach(b => b.classList.toggle("on", b.dataset.v === sp + "," + di));
    };
    $("#s-pre", dlg).onclick = e => { if (e.target.dataset.v) { [sp, di] = e.target.dataset.v.split(","); paint(); } };
    $("#s-sp", dlg).onclick = e => { if (e.target.dataset.v) { sp = e.target.dataset.v; paint(); } };
    $("#s-di", dlg).onclick = e => { if (e.target.dataset.v) { di = e.target.dataset.v; paint(); } };
    paint();
    dlg.onclose = async () => {
      if (dlg.returnValue !== "save") return;
      try { U.set((await api("/api/settings", { units: { speed: sp, distance: di } })).units); } catch (e) { alert("Could not save: " + e.message); }
      if (INDEX) INDEX.units = { speed: U.speed, distance: U.distance };
      route();
    };
    dlg.showModal();
  }

  let INDEX = null;
  // what the AI prompt is about: set by every view (page, id, lap, ref, s, game, label)
  let CTX = { page: "library", label: "All your sessions" };
  const comboCache = {};

  // ---------------------------------------------------------- AI prompt (copy and paste into any agent)
  async function promptDialog() {
    const dlg = $("#dlg");
    const goal = store.get("goal", "mix"), variant = store.get("variant", "agent");
    dlg.innerHTML = `<form method="dialog" class="prompt-dlg">
      <div class="dh"><h2>AI prompt</h2><div class="note">For: <b>${esc(CTX.label)}</b>. Paste it into any AI agent or chat. The app itself gives no advice: it only measures.</div></div>
      <div class="db">
        <div class="row"><span class="note" style="min-width:86px">Your goal</span><div class="seg" id="p-goal">
          <button type="button" data-v="road" title="Better, safer real-world driving: only road-safe suggestions">Road driving</button>
          <button type="button" data-v="mix" title="Both: track-only suggestions are labelled">Both</button>
          <button type="button" data-v="race" title="Lap time in the sim: racing techniques are fine">Lap time</button></div></div>
        <div class="row"><span class="note" style="min-width:86px">Pasting into</span><div class="seg" id="p-var">
          <button type="button" data-v="agent" title="Claude Code, Codex and other agents that can open files on this PC">An agent with file access</button>
          <button type="button" data-v="chat" title="ChatGPT, Claude.ai and other chats: the numbers are included in the prompt">A chat app</button></div></div>
        <textarea id="p-text" readonly spellcheck="false">Writing the prompt…</textarea>
      </div>
      <div class="df"><span class="note" id="p-note" style="margin-right:auto"></span><button class="btn" value="close">Close</button>
        <a class="btn" id="p-json" title="The same numbers as structured JSON, for agents or tools that read files">Download JSON brief</a>
        <button type="button" class="btn primary" id="p-copy">Copy prompt</button></div></form>`;
    const load = async () => {
      const g = store.get("goal", "mix"), v = store.get("variant", "agent");
      $$("#p-goal button", dlg).forEach(b => b.classList.toggle("on", b.dataset.v === g));
      $$("#p-var button", dlg).forEach(b => b.classList.toggle("on", b.dataset.v === v));
      const qs = new URLSearchParams({ page: CTX.page, goal: g, variant: v });
      for (const k of ["id", "lap", "ref", "s", "game", "combos", "since", "label"]) if (CTX[k]) qs.set(k, CTX[k]);
      $("#p-json", dlg).href = "/api/brief?" + qs;
      $("#p-text", dlg).value = "Writing the prompt…";
      try { $("#p-text", dlg).value = (await api("/api/prompt?" + qs)).text; } catch (e) { $("#p-text", dlg).value = "Could not build the prompt: " + e.message; }
      $("#p-note", dlg).textContent = "";
    };
    $("#p-goal", dlg).onclick = e => { if (e.target.dataset.v) { store.set("goal", e.target.dataset.v); load(); } };
    $("#p-var", dlg).onclick = e => { if (e.target.dataset.v) { store.set("variant", e.target.dataset.v); load(); } };
    $("#p-copy", dlg).onclick = async () => {
      const ta = $("#p-text", dlg);
      try { await navigator.clipboard.writeText(ta.value); } catch (e) { ta.select(); document.execCommand("copy"); }
      $("#p-note", dlg).textContent = "Copied ✓ Paste it into your agent.";
      $("#p-copy", dlg).textContent = "Copied ✓";
      setTimeout(() => { if ($("#p-copy", dlg)) $("#p-copy", dlg).textContent = "Copy prompt"; }, 1800);
    };
    dlg.showModal();
    load();
  }
  window.openPrompt = promptDialog;

  // ---------------------------------------------------------- shell: theme, status, prompt button
  function initShell() {
    $("#prompt-btn").onclick = promptDialog;
    $("#settings").onclick = settingsDialog;
    const th = store.get("theme", null);
    if (th) document.documentElement.dataset.theme = th;
    $("#theme").onclick = () => {
      const dark = matchMedia("(prefers-color-scheme: dark)").matches;
      const cur = document.documentElement.dataset.theme || (dark ? "dark" : "light");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next; store.set("theme", next); route();
    };
    let lastState = null;
    const poll = async () => {
      try {
        const s = await api("/api/status");
        const el = $("#status");
        el.hidden = s.state !== "working";
        el.textContent = s.state === "working" ? `Analysing ${s.current} (${s.done + 1}/${s.total})` : "";
        if (lastState === "working" && s.state === "ready") { INDEX = null; if (location.hash === "" || location.hash === "#/" || location.hash.startsWith("#/?")) route(); }
        lastState = s.state;
      } catch (e) { /* server stopped */ }
      setTimeout(poll, lastState === "working" ? 1500 : 5000);
    };
    poll();
  }

  async function getIndex(force) {
    if (!INDEX || force) {
      INDEX = await api("/api/index"); U.set(INDEX.units);
      for (const m of Object.values(INDEX.modules)) {           // a module's optional skin / panel CSS
        if (m.css && !document.querySelector(`link[data-module="${m.id}"]`))
          document.head.insertAdjacentHTML("beforeend", `<link rel="stylesheet" href="${esc(m.css)}" data-module="${esc(m.id)}">`);
      }
    }
    return INDEX;
  }
  async function getCombo(id, force) {
    if (!comboCache[id] || force) comboCache[id] = await api("/api/combo?id=" + encodeURIComponent(id));
    return comboCache[id];
  }

  // ---------------------------------------------------------- router
  async function route() {
    Charts.hideTip();
    const h = location.hash.slice(1) || "/";
    const [path, qs] = h.split("?");
    const q = Object.fromEntries(new URLSearchParams(qs || ""));
    const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
    const nav = parts[0] === "habits" || parts[0] === "modules" ? parts[0] : "library";
    $$(".nav a").forEach(a => a.classList.toggle("on", a.dataset.nav === nav));
    try {
      if (parts[0] === "combo") await comboView(parts[1], q);
      else if (parts[0] === "lap") await lapView(parts[1], parts[2], q);
      else if (parts[0] === "habits") await habitsView(q);
      else if (parts[0] === "modules") await modulesView();
      else await libraryView();
    } catch (e) {
      view.innerHTML = `<div class="empty">Something went wrong: ${esc(e.message)}<br><br><a href="#/">Back to the library</a></div>`;
      console.error(e);
    }
  }
  window.addEventListener("hashchange", route);

  // ---------------------------------------------------------- library
  const F = Object.assign({ game: "all", event: "all", cls: "all", car: "all", drive: "all", period: "all", disc: "all", group: "track", device: "all" }, store.get("filters", {}));

  async function libraryView() {
    if (!INDEX) view.innerHTML = `<div class="loading">Loading your laps…</div>`;
    const idx = await getIndex();
    const all = idx.combos;
    if (!all.length) {
      const hints = Object.values(idx.modules).map(m => m.ui.empty_hint).filter(Boolean);
      view.innerHTML = `<div class="empty"><h2>No laps yet</h2><p>${hints.length ? `Drive with ${esc(hints.join(" or "))} running.`
        : `No game module is installed: see <a href="#/modules">Modules</a>.`}<br>Laps are saved in <code>${esc(idx.data_dir)}</code>.</p></div>`;
      return;
    }
    CTX = { page: "library", game: F.game === "all" ? null : F.game, label: F.game === "all" ? "All your sessions" : "Your " + idx.games[F.game] + " sessions" };
    const counts = g => all.filter(c => g === "all" || c.game === g).reduce((n, c) => n + c.laps, 0);
    const games = ["all", ...Object.keys(idx.games).filter(g => all.some(c => c.game === g))];
    if (!games.includes(F.game)) F.game = "all";
    const inGame = all.filter(c => F.game === "all" || c.game === F.game);
    const now = Date.now() / 1000;
    const periodOk = c => F.period === "all" || c.last >= now - (+F.period) * 86400;
    const opts = key => [...new Set(inGame.map(c => c[key]).filter(Boolean))].sort();
    const events = opts("event"), classes = [...new Set(inGame.map(c => c.car_class).filter(Boolean))]
      .sort((a, b) => (inGame.find(c => c.car_class === a).class_sort > inGame.find(c => c.car_class === b).class_sort ? 1 : -1));
    const cars = [...new Map(inGame.map(c => [c.car_key, c.car_name])).entries()].sort((a, b) => a[1].localeCompare(b[1]));
    const drives = opts("drivetrain"), discs = opts("discipline");
    // route types: offered by modules with filters.disciplines (label and tip from their manifests)
    const discMods = [...new Set(inGame.map(c => c.game))].map(MOD).filter(m => (m.filters.disciplines || []).length);
    const discLabels = [...new Set(discMods.map(m => m.filters.discipline_label || "Event type"))];
    const discLabel = discLabels.length === 1 ? discLabels[0] : "Event type";
    const discTips = discMods.map(m => m.filters.discipline_tip).filter(Boolean);
    for (const [k, list] of [["event", events], ["cls", classes], ["drive", drives], ["disc", discs]]) if (F[k] !== "all" && !list.includes(F[k])) F[k] = "all";
    if (F.car !== "all" && !cars.some(c => c[0] === F.car)) F.car = "all";
    const devs = c => (c.devices && c.devices.length ? c.devices : [{ device: "unknown", laps: c.laps, best: c.best }]);
    const devsIn = DEVICES.filter(dv => inGame.some(c => devs(c).some(x => x.device === dv)));
    if (F.device !== "all" && !devsIn.includes(F.device)) F.device = "all";
    const devLaps = dv => inGame.reduce((n, c) => n + devs(c).filter(x => x.device === dv).reduce((m, x) => m + x.laps, 0), 0);
    const shown = inGame.filter(c => (F.event === "all" || c.event === F.event) && (F.cls === "all" || c.car_class === F.cls) &&
      (F.car === "all" || c.car_key === F.car) && (F.drive === "all" || c.drivetrain === F.drive) &&
      (F.disc === "all" || c.discipline === F.disc) && periodOk(c) && (F.device === "all" || devs(c).some(x => x.device === F.device)));
    const moreActive = F.drive !== "all" || F.period !== "all" || F.disc !== "all";
    const eventCount = e => inGame.filter(c => c.event === e).length;

    view.innerHTML = `
      <div class="page-head"><div><h1>Your laps</h1><div class="sub">${all.reduce((n, c) => n + c.laps, 0)} laps on ${all.length} car/track combinations. Pick a track or a car.</div></div>
        <div class="spacer"></div><div class="seg" id="group"><button data-v="track">Tracks</button><button data-v="car">Cars</button></div></div>
      <div class="filters">
        <div class="game-tabs">${games.map(g => `<button class="game-tab ${F.game === g ? "on" : ""}" data-game="${g}">
          <span class="gl ${g}">${g === "all" ? "All" : esc(MOD(g).short)}</span><span>${g === "all" ? "All games" : esc(idx.games[g])}<small>${counts(g)} laps</small></span></button>`).join("")}</div>
        <div class="line">
          ${devsIn.length > 1 ? `<div class="chips" id="devices" title="Input device the laps were driven with"><button class="chip ${F.device === "all" ? "on" : ""}" data-v="all">All devices</button>${devsIn.map(dv => `<button class="chip ${F.device === dv ? "on" : ""}" data-v="${dv}">${DEV_LABEL[dv]}<span class="n">${devLaps(dv)}</span></button>`).join("")}</div>` : ""}
          ${events.length > 1 ? `<div class="chips" id="events"><button class="chip ${F.event === "all" ? "on" : ""}" data-v="all">All events</button>${events.map(e => `<button class="chip ${F.event === e ? "on" : ""}" data-v="${esc(e)}">${esc(e)}<span class="n">${eventCount(e)}</span></button>`).join("")}</div>` : ""}
          ${classes.length > 1 ? `<label class="f">Class <select id="f-cls"><option value="all">All</option>${classes.map(c => `<option ${F.cls === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>` : ""}
          ${cars.length > 1 ? `<label class="f">Car <select id="f-car"><option value="all">All cars</option>${cars.map(([k, n]) => `<option value="${esc(k)}" ${F.car === k ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>` : ""}
          <button class="btn small" id="more" aria-expanded="${moreActive}">${moreActive ? "Fewer filters" : "More filters"}</button>
          ${(F.event !== "all" || F.cls !== "all" || F.car !== "all" || F.device !== "all" || moreActive) ? `<button class="link" id="clear">Clear filters</button>` : ""}
        </div>
        <div class="line" id="more-line" ${moreActive ? "" : "hidden"}>
          ${drives.length > 1 ? `<label class="f">Drivetrain <select id="f-drive"><option value="all">Any</option>${drives.map(d => `<option ${F.drive === d ? "selected" : ""}>${d}</option>`).join("")}</select></label>` : ""}
          <label class="f">Driven <select id="f-period"><option value="all">Any time</option><option value="7" ${F.period === "7" ? "selected" : ""}>Last 7 days</option><option value="30" ${F.period === "30" ? "selected" : ""}>Last 30 days</option></select></label>
          ${discs.length ? `<label class="f">${esc(discLabel)} <select id="f-disc"><option value="all">Any</option>${discs.map(d => `<option ${F.disc === d ? "selected" : ""}>${d}</option>`).join("")}</select></label>` : ""}
          ${!discs.length ? discTips.map(t => `<span class="note">${esc(t)}</span>`).join("") : ""}
        </div>
      </div>
      <div id="cards"></div>`;

    $$("#group button").forEach(b => b.classList.toggle("on", b.dataset.v === F.group));
    const save = () => { store.set("filters", F); libraryView(); };
    $("#group").onclick = e => { if (e.target.dataset.v) { F.group = e.target.dataset.v; save(); } };
    $$(".game-tab").forEach(b => b.onclick = () => { F.game = b.dataset.game; save(); });
    if ($("#events")) $("#events").onclick = e => { const v = e.target.closest("button")?.dataset.v; if (v) { F.event = v; save(); } };
    if ($("#devices")) $("#devices").onclick = e => { const v = e.target.closest("button")?.dataset.v; if (v) { F.device = v; save(); } };
    for (const [id, k] of [["#f-cls", "cls"], ["#f-car", "car"], ["#f-drive", "drive"], ["#f-period", "period"], ["#f-disc", "disc"]])
      if ($(id)) $(id).onchange = e => { F[k] = e.target.value; save(); };
    $("#more").onclick = () => { const l = $("#more-line"); l.hidden = !l.hidden; $("#more").textContent = l.hidden ? "More filters" : "Fewer filters"; };
    if ($("#clear")) $("#clear").onclick = () => { Object.assign(F, { event: "all", cls: "all", car: "all", drive: "all", period: "all", disc: "all", device: "all" }); save(); };

    const narrowed = shown.length !== inGame.length;
    CTX = { page: "library", game: F.game === "all" ? null : F.game,
            combos: narrowed ? shown.map(c => c.id).join(",") : null,
            label: (F.game === "all" ? "All your sessions" : "Your " + idx.games[F.game] + " sessions") + (narrowed ? ", filtered (" + shown.length + " car/tracks)" : "") };
    const host = $("#cards");
    if (!shown.length) { host.outerHTML = `<div class="empty">Nothing matches these filters.</div>`; return; }
    // one section per input device (wheel, controller, keyboard & mouse); a car/track driven on two devices is in both,
    // with that device's laps and best time
    const sections = DEVICES.filter(dv => (F.device === "all" || F.device === dv) && shown.some(c => devs(c).some(x => x.device === dv)));
    host.innerHTML = sections.map(dv => {
      const groups = new Map();
      for (const c0 of shown) {
        const x = devs(c0).find(x => x.device === dv);
        if (!x) continue;
        const c = Object.assign({}, c0, { laps: x.laps, best: x.best, dev: devs(c0).length > 1 ? dv : "" });
        const k = F.group === "car" ? c.car_key : c.track_key;
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(c);
      }
      const list = [...groups.values()].sort((a, b) => Math.max(...b.map(c => c.last)) - Math.max(...a.map(c => c.last)));
      const laps = list.flat().reduce((n, c) => n + c.laps, 0);
      return `<section class="dev-group"><h2 class="dev-head">${DEV_LABEL[dv]} <small>${laps} lap${laps === 1 ? "" : "s"} · ${list.flat().length} car/track${list.flat().length === 1 ? "" : "s"}</small></h2>
        <div class="cards">${list.map(cs => F.group === "car" ? carCard(cs) : trackCard(cs)).join("")}</div></section>`;
    }).join("");
    $$(".tcard", host).forEach(card => {
      card.onclick = e => {
        if (e.target.closest(".edit, .subs a")) return;
        location.hash = "#/combo/" + encodeURIComponent(card.dataset.first) + (card.dataset.dev ? "?dev=" + card.dataset.dev : "");
      };
      card.onkeydown = e => { if (e.key === "Enter") card.click(); };
    });
    $$(".edit", host).forEach(b => b.onclick = e => {
      e.stopPropagation();
      const c = shown.find(x => x.id === b.dataset.id), k = b.dataset.kind === "cars" ? "car_key" : "track_key";
      editDialog(b.dataset.kind, c, all.filter(x => x[k] === c[k]));     // every combo on this track / with this car, filtered or not
    });
  }

  function statsLine(cs) {
    const laps = cs.reduce((n, c) => n + c.laps, 0);
    const best = cs.length === 1 && cs[0].best ? fmtTime(cs[0].best) : null;
    return `<div class="stats"><span><b>${laps}</b> lap${laps === 1 ? "" : "s"}</span>${best ? `<span>recorded reference <b>${best}</b></span>` : ""}<span>${ago(Math.max(...cs.map(c => c.last)))}</span></div>`;
  }

  function trackCard(cs) {
    const c = cs.slice().sort((a, b) => b.last - a.last)[0];
    const outline = Charts.outline(c.outline, { closed: c.closed, cls: c.track_img ? "over" : "" });
    const pic = c.track_img ? `<img src="${esc(c.track_img)}" alt="" loading="lazy">${outline}` : outline || `<div class="class-tile"><span class="hint">Analysing…</span></div>`;
    const subs = cs.length > 1 ? `<div class="subs">${cs.map(x => `<a href="#/combo/${encodeURIComponent(x.id)}${x.dev ? "?dev=" + x.dev : ""}">${esc(x.car_name)} · ${x.best ? fmtTime(x.best) : "-"}</a>`).join("")}</div>`
      : `<div class="meta">${esc(c.car_name)}</div>`;
    return `<div class="tcard" tabindex="0" data-first="${esc(c.id)}" data-dev="${esc(c.dev || "")}">
      <div class="pic">${pic}<div class="badges"><span class="badge ${c.game}">${esc(MOD(c.game).short)}</span><span class="badge">${esc(c.event)}${c.discipline ? " · " + esc(c.discipline) : ""}</span></div>
        <button class="btn small edit" data-kind="tracks" data-id="${esc(c.id)}" title="Rename, set type or add a picture">✎</button></div>
      <div class="body"><div class="title">${esc(c.track_name)}</div>${subs}${statsLine(cs)}</div></div>`;
  }

  function carCard(cs) {
    const c = cs.slice().sort((a, b) => b.last - a.last)[0];
    const pic = c.car_img ? `<img src="${esc(c.car_img)}" alt="" loading="lazy">`
      : `<div class="class-tile"><span class="cl" style="background:${classColor(c)}">${esc(c.car_class || "?")}${c.pi ? `<small style="font-size:22px;margin-left:8px;opacity:.85">${esc(c.pi)}</small>` : ""}</span>
         <span class="hint">✎ to add a picture of this car</span></div>`;
    const subs = cs.length > 1 ? `<div class="subs">${cs.map(x => `<a href="#/combo/${encodeURIComponent(x.id)}${x.dev ? "?dev=" + x.dev : ""}">${esc(x.track_name)} · ${x.best ? fmtTime(x.best) : "-"}</a>`).join("")}</div>`
      : `<div class="meta">${esc(c.track_name)}</div>`;
    return `<div class="tcard" tabindex="0" data-first="${esc(c.id)}" data-dev="${esc(c.dev || "")}">
      <div class="pic">${pic}<div class="badges"><span class="badge ${c.game}">${esc(MOD(c.game).short)}</span>${c.car_class ? `<span class="badge">${esc(c.car_class)}${c.pi ? " " + esc(c.pi) : ""}</span>` : ""}${c.drivetrain ? `<span class="badge">${c.drivetrain}</span>` : ""}</div>
        <button class="btn small edit" data-kind="cars" data-id="${esc(c.id)}" title="Rename or change the picture">✎</button></div>
      <div class="body"><div class="title">${esc(c.car_name)}</div>${c.car_detail ? `<div class="meta">${esc(c.car_detail)}</div>` : ""}${subs}${statsLine(cs)}</div></div>`;
  }

  // ---------------------------------------------------------- edit dialog (names, route type, pictures, skin)
  function editDialog(kind, c, group) {
    const dlg = $("#dlg");
    const isCar = kind === "cars";
    const key = isCar ? c.car_key : c.track_key;
    const name = isCar ? c.car_name : c.track_name;
    const curImg = isCar ? c.car_img : c.track_img;
    const discs = MOD(c.game).filters.disciplines || [];
    let newImage = null;
    dlg.innerHTML = `<form method="dialog">
      <div class="dh"><h2>${isCar ? "Car" : "Track / route"}</h2><div class="note">${esc(c.game_name)} · changes only affect how the app shows it</div></div>
      <div class="db">
        <label>Name<input type="text" id="e-name" value="${esc(name)}"></label>
        ${!isCar && discs.length ? `<label>Event type<select id="e-disc"><option value="">Not set</option>${discs.map(d => `<option ${c.discipline === d ? "selected" : ""}>${d}</option>`).join("")}</select></label>` : ""}
        ${isCar && c.skins && c.skins.length > 1 ? `<label>Skin shown<div class="skins">${c.skins.map(s => `<button type="button" data-skin="${esc(s)}" class="${s === c.skin ? "on" : ""}"><img src="/img/${encodeURIComponent(c.game)}/car/${encodeURIComponent(c.car)}/${encodeURIComponent(s)}" loading="lazy" alt=""><span>${esc(s)}</span></button>`).join("")}</div></label>` : ""}
        <div><div class="note" style="margin-bottom:6px">Picture ${esc(MOD(c.game).ui.picture_hint || "(a screenshot works: it is shrunk to about 30 KB)")}</div>
          <div class="drop" id="e-drop" tabindex="0">${curImg ? `<img src="${esc(curImg)}" alt="">` : ""}<span>Drop an image here, paste one (Ctrl+V) or <u>choose a file</u></span><input type="file" accept="image/*" hidden></div>
          ${curImg && (isCar ? INDEX && c.car_img && c.car_img.startsWith("/img/user") : c.track_img && c.track_img.startsWith("/img/user")) ? `<button type="button" class="link" id="e-noimg" style="margin-top:6px">Remove my picture</button>` : ""}
        </div>
      </div>
      <div class="df"><button type="button" class="btn quiet-danger" id="e-del" style="margin-right:auto" title="Delete the laps and everything the app stores for them">Delete…</button>
        <button class="btn" value="cancel">Cancel</button><button class="btn primary" id="e-save" value="save">Save</button></div></form>`;
    $("#e-del", dlg).onclick = () => deleteDialog(kind, isCar ? c.car_name : c.track_name, group || [c], () => {
      dlg.close("deleted"); INDEX = null; Object.keys(comboCache).forEach(k => delete comboCache[k]); location.hash = "#/"; route();
    });
    const drop = $("#e-drop", dlg), file = $("input[type=file]", dlg);
    const take = f => {
      if (!f || !f.type.startsWith("image/")) return;
      const img = new Image();
      img.onload = () => {
        const w = Math.min(640, img.naturalWidth), h = Math.round(img.naturalHeight * w / img.naturalWidth);
        const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
        cv.getContext("2d").drawImage(img, 0, 0, w, h);
        newImage = cv.toDataURL("image/jpeg", 0.82);
        drop.querySelector("img")?.remove();
        drop.insertAdjacentHTML("afterbegin", `<img src="${newImage}" alt="">`);
        URL.revokeObjectURL(img.src);
      };
      img.src = URL.createObjectURL(f);
    };
    drop.onclick = () => file.click();
    file.onchange = () => take(file.files[0]);
    drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
    drop.ondragleave = () => drop.classList.remove("over");
    drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); take(e.dataTransfer.files[0]); };
    const onPaste = e => { const it = [...(e.clipboardData?.items || [])].find(i => i.type.startsWith("image/")); if (it) take(it.getAsFile()); };
    document.addEventListener("paste", onPaste);
    let skin = c.skin;
    $$(".skins button", dlg).forEach(b => b.onclick = () => { skin = b.dataset.skin; $$(".skins button", dlg).forEach(x => x.classList.toggle("on", x === b)); });
    let removeImg = false;
    if ($("#e-noimg", dlg)) $("#e-noimg", dlg).onclick = () => { removeImg = true; drop.querySelector("img")?.remove(); newImage = null; };
    dlg.onclose = async () => {
      document.removeEventListener("paste", onPaste);
      if (dlg.returnValue !== "save") return;
      const fields = {};
      const nm = $("#e-name", dlg).value.trim();
      fields.name = nm && nm !== name ? nm : (nm ? undefined : "");
      if ($("#e-disc", dlg)) fields.discipline = $("#e-disc", dlg).value;
      if (isCar && skin !== c.skin) fields.skin = skin;
      if (removeImg) fields.image = "";
      Object.keys(fields).forEach(k => fields[k] === undefined && delete fields[k]);
      try {
        if (Object.keys(fields).length) await api("/api/library", { kind, key, fields });
        if (newImage) await api("/api/image", { kind, key, data: newImage });
      } catch (e) { alert("Could not save: " + e.message); }
      INDEX = null; Object.keys(comboCache).forEach(k => delete comboCache[k]);
      route();
    };
    dlg.showModal();
  }

  // ---------------------------------------------------------- delete (its own dialog on top of the edit dialog)
  // Built against fast clicking: the trigger sits bottom-left, "Yes" sits bottom-right and is the quiet button,
  // "No" is highlighted and focused (Enter / Esc keep the laps), and "Yes" only wakes up after 2 seconds.
  function deleteDialog(kind, name, combos, done) {
    const d = document.createElement("dialog");
    d.className = "confirm-del";
    const isCar = kind === "cars";
    const label = x => isCar ? x.track_name : x.car_name;
    const fmtMB = b => b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB";
    d.innerHTML = `<form method="dialog">
      <div class="dh"><h2>Permanently delete ${esc(name)}?</h2></div>
      <div class="db">
        <div class="warn-box"><b>Are you sure you want to permanently delete these laps?</b>
          This can't be undone. The files are deleted straight away, they don't go to the Recycle Bin,
          and Simetrics can't get them back.</div>
        ${combos.length > 1 ? `<div class="note">${isCar ? "Tracks driven with this car" : "Cars driven on this track"}: untick any you want to keep.</div>` : ""}
        <div class="del-list">${combos.map(x => `<label class="del-item">${combos.length > 1 ? `<input type="checkbox" checked value="${esc(x.id)}">` : ""}
          <span>${esc(label(x))}</span><small>${x.laps} lap${x.laps === 1 ? "" : "s"} · ${x.sessions} session${x.sessions === 1 ? "" : "s"} · ${ago(x.last)}</small></label>`).join("")}</div>
        <div class="note" id="d-sum">Checking what would be deleted…</div>
      </div>
      <div class="df"><button class="btn primary" value="no" autofocus>No, keep them</button><span style="flex:1"></span>
        <button type="button" class="btn quiet-danger" id="d-yes" disabled>Yes, delete permanently</button></div></form>`;
    document.body.appendChild(d);
    d.onclose = () => d.remove();
    const picked = () => combos.length > 1 ? $$(".del-item input:checked", d).map(i => i.value) : [combos[0].id];
    const yes = $("#d-yes", d);
    let armed = false, plan = null, seq = 0;
    const sync = () => { yes.disabled = !armed || !plan || !picked().length; };
    const check = async () => {
      const ids = picked(), my = ++seq;
      plan = null; sync();
      if (!ids.length) { $("#d-sum", d).textContent = "Nothing selected."; return; }
      $("#d-sum", d).textContent = "Checking what would be deleted…";
      try {
        const p = await api("/api/delete", { kind, ids });
        if (my !== seq) return;                 // a newer tick/untick is being checked
        plan = p;
        $("#d-sum", d).innerHTML = `Deletes <b>${plan.laps} lap${plan.laps === 1 ? "" : "s"}</b>: ${plan.files} file${plan.files === 1 ? "" : "s"} (${fmtMB(plan.bytes)}) with their
          reports, saved analysis and corner map${plan.library_entries.length ? `, plus this ${isCar ? "car" : "track"}'s name and picture in the app` : ""}.`;
      } catch (e) { if (my === seq) $("#d-sum", d).textContent = "Could not check: " + e.message; }
      sync();
    };
    $$(".del-item input", d).forEach(i => i.onchange = check);
    setTimeout(() => { armed = true; sync(); }, 2000);
    yes.onclick = async () => {
      yes.disabled = true; yes.textContent = "Deleting…";
      try {
        const r = await api("/api/delete", { kind, ids: picked(), confirm: "delete" });
        if (r.errors.length) alert("Some files could not be deleted (is a logger still writing to them?):\n" + r.errors.slice(0, 8).join("\n"));
        d.close(); done();
      } catch (e) { alert("Could not delete: " + e.message); d.close(); }
    };
    d.showModal();
    $("button[value=no]", d).focus();
    check();
  }

  // ---------------------------------------------------------- modules: one per game (modules/<id>/)
  async function modulesView() {
    view.innerHTML = `<div class="loading">Reading modules…</div>`;
    const m = await api("/api/modules");
    CTX = { page: "library", label: "All your sessions" };
    const rows = Object.fromEntries((m.manage.modules || []).map(r => [r.id, r]));
    const li = xs => xs.map(x => `<li>${esc(x)}</li>`).join("");
    const backupList = r => (r && r.backups.length) ? `<h3>Saved versions</h3><div class="mod-bk">${r.backups.map(b => `<div>
        <span>v${esc(b.version)} <small class="muted">saved ${fmtDate(b.saved)}</small></span>
        <button class="btn" data-act="rollback" data-id="${esc(r.id)}" data-backup="${esc(b.name)}">${r.installed ? "Roll back to this" : "Restore"}</button></div>`).join("")}</div>` : "";
    const card = x => {
      const r = rows[x.id];
      return `<div class="card mod">
        <div class="mod-h"><span class="gl ${esc(x.id)}">${esc(x.short)}</span><div><h2>${esc(x.name)}</h2>
          <div class="note">version ${esc(x.version)} · needs core API ${x.core_api} · <span class="ok">compatible ✓</span>${r && r.update ? ` · <span class="upd">${esc(r.available)} available</span>` : ""}</div></div></div>
        <p class="note">${esc(x.description)}</p>
        <div class="tags"><span class="tag">${x.laps} lap${x.laps === 1 ? "" : "s"} in ${esc(x.data_folder)}</span>
          ${x.logger.kind ? `<span class="tag">logger: ${esc(x.logger.kind)}</span>` : ""}</div>
        ${(x.logger.steps || []).length ? `<h3>Set up the logger</h3><ol class="note">${li(x.logger.steps)}</ol>` : ""}
        ${backupList(r)}
        <div class="note mod-path">Folder: <code>${esc(x.folder)}</code>${x.hooks.length ? `<br>Adapter hooks: ${esc(x.hooks.join(", "))}` : ""}</div>
        <div class="mod-actions"><button class="btn quiet-danger" data-act="remove" data-id="${esc(x.id)}">Remove…</button><span style="flex:1"></span>
          ${r && r.update ? `<button class="btn primary" data-act="update" data-id="${esc(x.id)}" data-version="${esc(r.available)}">Update to ${esc(r.available)}</button>` : ""}</div>
      </div>`;
    };
    const avail = (m.manage.modules || []).filter(r => !r.installed && !m.failed.some(f => f.id === r.id) && (r.in_registry || r.backups.length));
    const availCard = r => `<div class="card mod">
        <div class="mod-h"><span class="gl ${esc(r.id)}">${esc(r.short)}</span><div><h2>${esc(r.name)}</h2>
          <div class="note">${r.available ? "version " + esc(r.available) : r.in_registry ? `<span class="lose">needs a newer version of Simetrics</span>` : "removed (not in the registry)"}${r.needs_newer_core && r.available ? " · a newer version needs a newer Simetrics" : ""}</div></div></div>
        <p class="note">${esc(r.description)}</p>
        ${backupList(r)}
        ${r.available ? `<div class="mod-actions"><span style="flex:1"></span><button class="btn primary" data-act="install" data-id="${esc(r.id)}" data-version="${esc(r.available)}">Install</button></div>` : ""}
      </div>`;
    const reg = m.manage.registry;
    view.innerHTML = `
      <div class="page-head"><div><h1>Modules</h1><div class="sub">Each game is a module. The library, filters and settings only show installed games.</div></div></div>
      <div class="card mod-reg">
        <div class="row"><div style="flex:1"><h2>Updates</h2>
          <div class="note">${m.registry_source ? `Module registry: <code>${esc(m.registry_source)}</code>${reg && reg.source === m.registry_source ? ` · checked ${ago(reg.fetched)}` : " · not checked yet"}`
            : "No module registry is set."}</div></div>
          ${m.registry_source ? `<button class="btn" id="mod-check">Check for updates</button>` : ""}</div>
        <div class="note" id="mod-msg"></div>
        <details><summary class="note">Use a different registry</summary>
          <div class="row" style="margin-top:8px"><input id="mod-src" style="flex:1" placeholder="leave empty for the official registry" value="${esc(m.registry_setting)}">
            <button class="btn" id="mod-src-save">Save</button></div>
          <div class="note">An https:// address or a registry.json file on this PC. Modules run code on this PC: only use a registry you trust.
            Simetrics only contacts the registry when you press Check for updates.</div></details>
      </div>
      <div class="mods">${m.installed.map(card).join("") || `<div class="empty">No game module is installed.</div>`}</div>
      ${avail.length ? `<h2 class="group-h">Available</h2><div class="mods">${avail.map(availCard).join("")}</div>` : ""}
      ${m.failed.length ? `<h2 class="group-h">Not loaded</h2><div class="mods">${m.failed.map(x => `<div class="card mod bad">
          <div class="mod-h"><span class="gl">!</span><div><h2>${esc(x.name)}</h2>
          <div class="note">${x.version ? "version " + esc(x.version) + " · " : ""}${x.core_api ? "needs core API " + x.core_api : ""}</div></div></div>
          <p class="lose">${esc(x.error)}</p>${backupList(rows[x.id])}<div class="note mod-path">Folder: <code>${esc(x.folder)}</code></div></div>`).join("")}</div>` : ""}
      ${m.orphan_data.length ? `<div class="card" style="margin-top:16px"><h2>Laps without a module</h2>
          <p class="note">These lap folders stay on disk, untouched. Install the matching module to see them again.</p>
          <ul class="note">${m.orphan_data.map(o => `<li><code>${esc(o.data_folder)}</code>: ${o.laps} lap${o.laps === 1 ? "" : "s"}</li>`).join("")}</ul></div>` : ""}
      <div class="note" style="margin-top:18px">This Simetrics has core API <b>${m.core_api}</b>. Modules live in <code>${esc(m.modules_dir)}</code>.
        Removed and replaced versions are kept (the last ${m.manage.keep_backups} of each) and can be restored here. Laps in <code>data/</code> are never touched.</div>`;
    const msg = $("#mod-msg");
    if ($("#mod-check")) $("#mod-check").onclick = async e => {
      e.target.disabled = true; msg.textContent = "Reading the registry…";
      try { await api("/api/module_check", {}); modulesView(); }
      catch (err) { msg.innerHTML = `<span class="lose">${esc(err.message)}</span>`; e.target.disabled = false; }
    };
    $("#mod-src-save").onclick = async () => {
      try { await api("/api/module_registry", { source: $("#mod-src").value }); modulesView(); }
      catch (err) { msg.innerHTML = `<span class="lose">${esc(err.message)}</span>`; }
    };
    $$("[data-act]", view).forEach(b => b.onclick = () => moduleDialog(b.dataset.act, b.dataset.id,
      { version: b.dataset.version, backup: b.dataset.backup }, (rows[b.dataset.id] || {}).name || b.dataset.id));
  }

  // install / update / roll back / remove: the server says what would happen first, then Simetrics restarts
  function moduleDialog(action, id, extra, name) {
    const d = document.createElement("dialog");
    const remove = action === "remove";
    const title = { install: `Install ${name}?`, update: `Update ${name}?`, rollback: `Roll back ${name}?`, remove: `Remove ${name}?` }[action];
    const verb = { install: "Install", update: "Update", rollback: "Roll back", remove: "Yes, remove it" }[action];
    if (remove) d.className = "confirm-del";
    d.innerHTML = `<form method="dialog"><div class="dh"><h2>${esc(title)}</h2></div>
      <div class="db" id="md-body"><div class="note">Checking…</div></div>
      <div class="df">${remove ? `<button class="btn primary" value="no" autofocus>No, keep it</button>` : `<button class="btn" value="no">Cancel</button>`}
        <span style="flex:1"></span><button type="button" class="btn ${remove ? "quiet-danger" : "primary"}" id="md-yes" disabled>${verb}</button></div></form>`;
    document.body.appendChild(d);
    d.onclose = () => d.remove();
    const body = $("#md-body", d), yes = $("#md-yes", d);
    const kb = b => b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB";
    const req = { action, id, version: extra.version, backup: extra.backup };
    let armed = !remove, plan = null;
    const sync = () => { yes.disabled = !armed || !plan || plan.busy.length > 0; };
    if (remove) setTimeout(() => { armed = true; sync(); }, 2000);
    (async () => {
      try { plan = await api("/api/module", req); }
      catch (e) { body.innerHTML = `<div class="lose">${esc(e.message)}</div>`; return; }
      const what = {
        install: `Downloads version <b>${esc(plan.to)}</b> (${kb(plan.size)}). It must match the registry's SHA-256 and load cleanly before it goes in.`,
        update: `Replaces version <b>${esc(plan.from)}</b> with <b>${esc(plan.to)}</b> (${kb(plan.size)} download). The download must match the registry's SHA-256 and load cleanly, or nothing changes.
          Version ${esc(plan.from)} is kept, so you can roll back here.`,
        rollback: `Puts back the saved version <b>${esc(plan.to)}</b>${plan.from ? ` in place of <b>${esc(plan.from)}</b>, which is kept so you can switch back` : ""}.`,
        remove: `<div class="warn-box"><b>${esc(name)} will disappear from the library, trends and AI prompts.</b>
          Its folder isn't deleted: it moves to the saved versions, and you can restore it from this page.</div>
          <div class="note">Your laps in <code>${esc(plan.data_folder)}</code> stay on disk and come back when the module does.</div>`,
      }[action];
      body.innerHTML = `<div class="note">${what}</div>
        ${plan.warnings.map(w => `<div class="note">⚠ ${esc(w)}</div>`).join("")}
        ${plan.busy.length ? `<div class="lose">Close ${esc(plan.busy.join(", "))} first: it's using this module.</div>` : ""}
        ${remove ? "" : `<div class="note">${action === "install" ? "" : "Your settings inside the module (such as the AC app's lap folder) are kept. "}Simetrics restarts by itself afterwards.</div>`}`;
      sync();
    })();
    yes.onclick = async () => {
      yes.disabled = true; yes.textContent = "Working…";
      try {
        await api("/api/module", Object.assign({ confirm: action }, req));
        body.innerHTML = `<div class="note"><b>Done.</b> Restarting Simetrics to load the change…</div>`;
        $(".df", d).innerHTML = "";
        await waitForRestart(body);
      } catch (e) {
        body.insertAdjacentHTML("beforeend", `<div class="lose">${esc(e.message)}</div><div class="note">Nothing was changed.</div>`);
        yes.textContent = verb;
      }
    };
    d.showModal();
    $("button[value=no]", d).focus();
  }

  async function waitForRestart(body) {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    await sleep(1800);
    for (let i = 0; i < 90; i++) {
      try { const r = await fetch("/api/status", { cache: "no-store" }); if (r.ok) { location.hash = "#/modules"; location.reload(); return; } }
      catch (e) { /* still restarting */ }
      await sleep(700);
    }
    body.innerHTML = `<div class="lose">Simetrics didn't come back by itself. Start it again with Start Simetrics.bat.</div>`;
  }

  // ---------------------------------------------------------- combo (car @ track)
  async function comboView(id, q) {
    view.innerHTML = `<div class="loading">Analysing laps… (the first time can take a few seconds)</div>`;
    const { combo: c, analysis: a, tip_phrases: P } = await getCombo(id);
    await getIndex();
    // input device filter for the session list (only shown when this car/track was driven on more than one device)
    const devsHere = DEVICES.filter(dv => a.sessions.some(s => devOf(s) === dv));
    const selDev = devsHere.length > 1 && devsHere.includes(q.dev) ? q.dev : "all";
    const sessions = a.sessions.filter(s => selDev === "all" || devOf(s) === selDev);
    const selS = q.s && sessions.find(s => s.session === q.s) ? q.s : sessions[sessions.length - 1].session;
    const S = sessions.find(s => s.session === selS);
    const lapsS = a.laps.filter(l => l.session === selS);
    const bestLap = a.laps.find(l => l.file === a.ref);
    const cleanN = a.laps.filter(l => l.clean).length;
    CTX = { page: "combo", id, s: selS, label: `${c.car_name} @ ${c.track_name}, session ${fmtDate(S.start)}` };

    view.innerHTML = `
      <div class="hero">
        <div class="pics">
          <div>${c.track_img ? `<img src="${esc(c.track_img)}" alt="">` : Charts.outline(a.outline, { closed: a.closed })}</div>
          <div>${c.car_img ? `<img src="${esc(c.car_img)}" alt="">` : `<div class="class-tile"><span class="cl" style="background:${classColor(c)};font-size:34px">${esc(c.car_class || "?")}</span></div>`}</div>
        </div>
        <div>
          <div class="crumbs"><a href="#/">Library</a> › ${esc(c.game_name)}</div>
          <h1>${esc(c.car_name)} <span class="muted" style="font-weight:500">@</span> ${esc(c.track_name)}</h1>
          <div class="tags"><span class="tag" style="border-color:${GAME_COLOR(c.game)}">${esc(c.game_name)}</span><span class="tag">${esc(c.event)}${c.discipline ? " · " + esc(c.discipline) : ""}</span>
            ${c.car_class ? `<span class="tag">${esc(c.car_class)}${c.pi ? " " + esc(c.pi) : ""}</span>` : ""}${c.drivetrain ? `<span class="tag">${c.drivetrain}</span>` : ""}
            <span class="tag">${U.dist(a.length_m)} ${a.closed ? "lap" : "run"}</span><span class="tag">${a.corners.length} corners</span>
            ${devsHere.map(dv => devChip(dv)).join("")}</div>
          <div class="row" style="margin-top:12px"><a class="btn" href="#/lap/${encodeURIComponent(id)}/${encodeURIComponent(referenceLap(lapsS)?.file || a.ref)}">▶ Replay this session's reference lap</a>
            <a class="btn" href="#/habits?combos=${encodeURIComponent(id)}">Trends for this car/track</a>
            <button class="btn prompt" onclick="openPrompt()"><span aria-hidden="true">✦</span> AI prompt for this session</button></div>
        </div>
      </div>
      <div class="tiles">
        <div class="tile"><div class="k">Reference lap</div><div class="v">${fmtTime(a.ref_time ?? a.best)}</div><div class="s">${esc(a.reference_role || "Fastest recorded comparison lap")}<br>${bestLap ? "lap " + bestLap.number + ", " + fmtDate(sessions.find(s => s.session === bestLap.session)?.start) : ""}</div></div>
        <div class="tile"><div class="k">Typical lap <span class="info" title="${esc(a.typical_definition || "Median of rolling laps without detected major incidents or continuity failures. Unverified track validity is identified separately.")}">i</span></div><div class="v">${fmtTime(a.typical)}</div><div class="s">${a.typical && a.ref_time ? fmtDelta(a.typical - a.ref_time, 2) + " s vs reference · " : ""}${cleanN} without detected major incidents</div></div>
        <div class="tile"><div class="k">Theoretical best <span class="info" title="Your fastest time through every corner section, added together. Illustrative only: the sections come from different laps with different entry and exit speeds, so it isn't a lap you could necessarily drive.">i</span></div><div class="v">${fmtTime(a.theoretical)}</div><div class="s">${a.theoretical && a.ref_time ? fmtDelta(a.theoretical - a.ref_time, 2) + " s vs reference · illustrative" : ""}</div></div>
        <div class="tile"><div class="k">Laps recorded</div><div class="v">${a.laps.length}</div><div class="s">${esc(statusSummary(a.laps))}<br>${a.sessions.length} session${a.sessions.length === 1 ? "" : "s"}</div></div>
      </div>
      <div class="note" style="margin:-6px 0 14px" id="cmap">Corner map <b>v${a.corner_set.version}</b>: distances and T numbers come from
        lap ${esc(a.corner_set.frame_lap)} (${fmtShort(sessions.find(s => s.session === a.corner_set.frame_session)?.start || 0)}) and stay fixed when you drive a faster lap.
        ${a.corner_set.frame_file !== a.ref ? `<button class="link" id="remap" title="Make a new corner map from the reference lap. T numbers may change; old copied prompts keep their version.">Re-map to reference lap</button>` : ""}</div>
      <div class="card" style="margin-bottom:16px"><h2>Lap times over time</h2><div class="sub">Every recorded lap in order, shaded by session. Filled dots = observed clear or without rewind; hollow dots = validity unverified; red hollow dots = flagged or excluded from pace comparisons. Hover for the status. Dashed lines show each session's typical lap.</div>
        <div id="prog"></div></div>
      <div class="row" style="margin:18px 0 10px"><h2>Session</h2>
        ${devsHere.length > 1 ? `<div class="chips" id="cdev"><button class="chip ${selDev === "all" ? "on" : ""}" data-v="all">All devices</button>${devsHere.map(dv => `<button class="chip ${selDev === dv ? "on" : ""}" data-v="${dv}">${DEV_LABEL[dv]}<span class="n">${a.sessions.filter(s => devOf(s) === dv).length}</span></button>`).join("")}</div>` : ""}
        <select id="sess">${sessions.slice().reverse().map(s => `<option value="${esc(s.session)}" ${s.session === selS ? "selected" : ""}>${fmtDate(s.start)} · ${s.laps} laps${s.best ? " · reference " + fmtTime(s.best) : ""} · ${DEV_LABEL[devOf(s)]}</option>`).join("")}</select>
        ${devChip(devOf(S))}<span class="muted" title="${esc(S.best_role || "Session reference; track validity is shown separately")}">reference ${fmtTime(S.best)} · typical ${fmtTime(S.typical)} · theoretical ${fmtTime(S.theoretical)}</span></div>
      <div class="grid-map">
        <div class="card"><h2>Where the time goes</h2><div class="sub">This session's comparison laps vs the reference (${bestLap ? "lap " + bestLap.number + ", " + fmtTime(a.ref_time) : ""}), per ${U.dist(20)}. Red = losing time, green = gaining. Click a corner.</div><div id="lossmap"></div></div>
        <div class="card"><h2>Corners</h2><div class="sub">${esc(S.population?.description || "This session's comparison laps")}. Gap = median section time minus the fastest section time in that population.</div><div class="scroll-x" id="ctable"></div></div>
      </div>
      <div class="card" style="margin-top:16px"><h2>Laps</h2><div class="sub">Click a lap to replay it against the reference. Statuses distinguish observed track limits, game invalidation and unverified recordings. Newest session first.</div><div id="sessions"></div></div>
      <div class="grid2" style="margin-top:16px" id="kc"></div>`;

    if ($("#remap")) $("#remap").onclick = async () => {
      if (!confirm("Make a new corner map from the reference lap? T numbers and distances may change.")) return;
      $("#cmap").textContent = "Re-mapping and re-analysing…";
      await api("/api/remap", { id });
      delete comboCache[id]; INDEX = null; route();
    };
    const devQ = selDev !== "all" ? "&dev=" + selDev : "";
    $("#sess").onchange = e => { location.hash = `#/combo/${encodeURIComponent(id)}?s=${encodeURIComponent(e.target.value)}${devQ}`; };
    if ($("#cdev")) $("#cdev").onclick = e => { const v = e.target.closest("button")?.dataset.v; if (v) location.hash = `#/combo/${encodeURIComponent(id)}${v === "all" ? "" : "?dev=" + v}`; };
    progressChart($("#prog"), a);
    lossMap($("#lossmap"), c, a, S, k => highlightCorner(k));
    cornerTable($("#ctable"), a, S, lapsS, P);
    sessionsList($("#sessions"), id, a, sessions);
    keyCornerCharts($("#kc"), a);
  }

  function highlightCorner(k) {
    $$("#ctable tr[data-k]").forEach(tr => tr.classList.toggle("sel", +tr.dataset.k === k));
    $(`#ctable tr[data-k="${k}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function progressChart(host, a) {
    const laps = a.laps.slice();
    const bands = [], typ = [];
    let i = 0;
    for (const s of a.sessions) {
      const n = laps.filter(l => l.session === s.session).length;
      bands.push({ x0: i + 0.5, x1: i + n + 0.5, label: fmtShort(s.start).split(" ").slice(0, 2).join(" ") });
      if (s.typical) typ.push([i + 1, s.typical], [i + n, s.typical], [i + n + 0.5, null]);
      i += n;
    }
    const pts = laps.map((l, k) => [k + 1, l.time, l]);
    const times = laps.filter(paceEligible).map(l => l.time).sort((x, y) => x - y);
    const cap = times.length ? times[Math.floor(times.length * 0.9)] * 1.08 : null;
    Charts.line(host, {
      height: 220, bands, padL: 58, xInt: true, xMin: 0.5, xMax: laps.length + 0.5,
      series: [
        { name: "observed clear / without rewind", color: css("--lap"), points: pts.filter(p => observedClear(p[2]) && !flaggedLap(p[2])).map(p => [p[0], Math.min(p[1], cap || p[1]), p[2]]), dotsOnly: true, r: 4 },
        { name: "validity unverified", color: css("--lap"), points: pts.filter(p => !observedClear(p[2]) && !flaggedLap(p[2])).map(p => [p[0], Math.min(p[1], cap || p[1]), p[2]]), dotsOnly: true, r: 4, hollow: () => true },
        { name: "flagged / excluded", color: css("--brake"), points: pts.filter(p => flaggedLap(p[2])).map(p => [p[0], Math.min(p[1], cap || p[1]), p[2]]), dotsOnly: true, r: 4, hollow: () => true },
        { name: "session typical", color: css("--ref"), points: typ, dash: true },
      ].filter(s => s.points.length),
      yFmt: v => fmtTime(v).replace(/\.\d+$/, m => m.slice(0, 2)), xFmt: v => "lap " + v,
      yMax: cap || undefined,
      tooltip: (x, hits) => {
        const l = laps[Math.round(x) - 1]; if (!l) return "";
        return `<div class="tt">Lap ${l.number} · ${fmtDate(a.sessions.find(s => s.session === l.session)?.start)}</div>
          <div class="tr"><span>time</span><span>${fmtTime(l.time)}</span></div>
          <div class="tr"><span>vs reference</span><span>${fmtDelta(l.time - a.ref_time, 2)}</span></div>
          <div class="tr"><span>status</span><span>${esc(validityLabel(l))}</span></div>
          ${l.pace_eligible === false ? '<div class="tr"><span>pace comparison</span><span>excluded</span></div>' : ""}
          ${l.quality?.warnings?.length ? `<div class="tr"><span>data warnings</span><span>${esc(l.quality.warnings.join("; "))}</span></div>` : ""}
          ${l.incidents.length ? `<div class="tr"><span>incidents</span><span>${l.incidents.map(i => i.kind + " " + i.corner).join(", ")}</span></div>` : ""}`;
      },
      onClick: x => { const l = laps[Math.round(x) - 1]; if (l) location.hash = `#/lap/${encodeURIComponent(a.id)}/${encodeURIComponent(l.file)}`; },
    });
  }

  function mapSvg(c, a, colorAt, onCorner) {
    const pts = a.path;
    if (!pts.length) return `<div class="empty">No position data for a map.</div>`;
    const xs = pts.map(p => p[0]), zs = pts.map(p => p[1]);
    const minx = Math.min(...xs), maxx = Math.max(...xs), minz = Math.min(...zs), maxz = Math.max(...zs);
    const pad = Math.max(maxx - minx, maxz - minz) * 0.07;
    const vb = [minx - pad, minz - pad, maxx - minx + 2 * pad, maxz - minz + 2 * pad];
    const sw = Math.max(vb[2], vb[3]) / 120;
    let bg = "";
    if (c.map && c.map.ini && c.map.ini.WIDTH) {
      const m = c.map.ini, sf = m.SCALE_FACTOR || 1;
      bg = `<image href="${esc(c.map.url)}" x="${-m.X_OFFSET * sf}" y="${-m.Z_OFFSET * sf}" width="${m.WIDTH * sf}" height="${m.HEIGHT * sf}" opacity="0.3" preserveAspectRatio="none"/>`;
    }
    let segs = "";
    for (let i = 0; i < pts.length - 1; i++) {
      const col = colorAt(i);
      segs += `<line x1="${pts[i][0]}" y1="${pts[i][1]}" x2="${pts[i + 1][0]}" y2="${pts[i + 1][1]}" stroke="${col}" />`;
    }
    const corners = a.corners.map((cn, k) => cn.x == null ? "" : `<g class="cn" data-k="${k}" style="cursor:pointer"><circle cx="${cn.x}" cy="${cn.z - sw * 3.2}" r="${sw * 2.4}" fill="var(--surface)" stroke="var(--axis)" stroke-width="${sw * 0.25}"/>
      <text x="${cn.x}" y="${cn.z - sw * 3.2}" font-size="${sw * 2.2}" text-anchor="middle" dominant-baseline="central" fill="var(--ink)" font-weight="650">${esc(cn.name)}</text></g>`).join("");
    return `<svg viewBox="${vb.join(" ")}" style="width:100%;max-height:520px;display:block" role="img" aria-label="Track map coloured by time lost">
      ${bg}<polyline points="${pts.map(p => p.join(",")).join(" ")}" fill="none" stroke="var(--surface-3)" stroke-width="${sw * 3}" stroke-linejoin="round" stroke-linecap="round"/>
      <g stroke-width="${sw * 1.5}" stroke-linecap="round">${segs}</g>
      <rect x="${pts[0][0] - sw}" y="${pts[0][1] - sw}" width="${sw * 2}" height="${sw * 2}" fill="var(--ink)"/>${corners}</svg>`;
  }

  function lossColor(r) {
    if (r == null) return "var(--even)";
    if (r > 0.4) return `color-mix(in srgb, var(--lose) ${Math.round(Math.min(100, 45 + r * 18))}%, var(--even))`;
    if (r < -0.4) return `color-mix(in srgb, var(--gain) ${Math.round(Math.min(100, 45 - r * 18))}%, var(--even))`;
    return "var(--even)";
  }

  function lossMap(host, c, a, S, onCorner) {
    const per = 20 / a.path_step_m;          // path points per 20 m window
    host.innerHTML = mapSvg(c, a, i => lossColor(S.loss_map[Math.floor(i / per)]));
    $$(".cn", host).forEach(g => g.onclick = () => onCorner(+g.dataset.k));
    host.insertAdjacentHTML("beforeend", Charts.legend([{ name: "losing (darker = more)", color: "var(--lose)" }, { name: "level", color: "var(--even)" }, { name: "gaining", color: "var(--gain)" }]));
  }

  function cornerTable(host, a, S, lapsS, P) {
    const maxLoss = Math.max(0.05, ...S.corners.map(x => x.loss || 0));
    const slide = S.corners.some(x => x.beta_peak != null);
    const pct = !isWheelish(devOf(S));                 // controller / keyboard session: steering in % of full lock
    const refDev = a.ref_device || "unknown";
    // the best lap's steering is only shown next to laps on the same kind of device
    const refSame = pct ? refDev === devOf(S) : isWheelish(refDev);
    const rows = a.corners.map((cn, k) => {
      const sc = S.corners[k];
      const counts = {};
      for (const l of comparisonRows(S, lapsS)) for (const [tid, scope] of (l.tips[k] || [])) counts[tid] = counts[tid] || { n: 0, scope }, counts[tid].n++;
      const nValid = comparisonRows(S, lapsS).length || 1;
      const common = Object.entries(counts).filter(([, v]) => v.n >= Math.max(2, nValid * 0.4)).sort((x, y) => y[1].n - x[1].n).slice(0, 3);
      const tips = common.map(([t, v]) => `<span class="tipline">${esc(P[t] || t)} <span class="muted">${v.n}/${nValid} laps</span></span>`).join("<br>");
      const ref = cn.ref;
      return `<tr data-k="${k}"><td><b>${esc(cn.name)}</b><div class="muted">${U.dist(cn.apex_d)}</div></td>
        <td class="num ${sc.loss > 0.15 ? "lose" : ""}">${fmtNum(sc.loss, 2)}<span class="lossbar" style="width:${Math.round(40 * (sc.loss || 0) / maxLoss)}px"></span></td>
        <td class="num">${U.mph(sc.min_mph)}<span class="muted"> ±${U.mph(sc.min_mph_sd)}</span><div class="muted">reference ${U.kmh(ref.min_speed)}</div></td>
        <td class="num">${sc.brake_at == null ? "no brake" : U.dist(sc.brake_at)}<div class="muted">${sc.brake_sd != null ? "±" + U.dist(sc.brake_sd) : ""}</div></td>
        <td class="num" ${devOf(S) === "keyboard" ? 'title="Keys are on/off: no brake pressure to measure"' : ""}>${sc.brake_peak == null ? "-" : fmtNum(sc.brake_peak * 100) + "%"}</td>
        <td class="num">${gearTxt(sc.gear)}</td>
        <td class="num">${steerTxt(sc, pct)}<div class="muted">${refSame ? "reference " + steerTxt(ref, pct) : "reference: " + esc(DEV_LABEL[refDev].toLowerCase())}</div></td>
        ${slide ? `<td class="num">${fmtNum(sc.beta_peak)}°</td>` : ""}
        <td>${tips || '<span class="muted">-</span>'}</td></tr>`;
    }).join("");
    host.innerHTML = `<table class="t"><thead><tr><th>Corner</th><th class="num">Gap s</th><th class="num" title="Lowest speed between the corner's boundaries (not a geometric apex)">Min speed ${U.sl}</th><th class="num">Brake at</th><th class="num" title="Highest brake pedal input (0-100%), not brake pressure">Peak brake input</th><th class="num">Gear</th><th class="num" title="${pct ? "Peak steering input, % of full lock (a controller or keyboard has no wheel angle)" : "Peak steering-wheel angle"}">Steering${pct ? " % lock" : ""}</th>${slide ? '<th class="num">Slide</th>' : ""}<th class="wide" title="Measured differences vs the reference lap that show up on at least 40% of this session's comparison laps">Common differences vs reference</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function sessionsList(host, id, a, sessions = a.sessions) {
    host.innerHTML = sessions.slice().reverse().map((s, si) => {
      const laps = a.laps.filter(l => l.session === s.session);
      const sessionRef = referenceLap(laps);
      const h = s.habits, sd = devOf(s);
      const both = sd === "gamepad" ? "Both triggers" : sd === "keyboard" ? "Both keys" : "Both pedals";
      const na = (s.habit_na || {}).clutch_coast_s || "not recorded";
      return `<details class="sess" ${si === 0 ? "open" : ""}><summary><span class="when">${fmtDate(s.start)}</span>${devChip(sd)}
        <span><b>${s.laps}</b> recorded · ${esc(statusSummary(laps))}</span><span title="${esc(s.best_role || "Session reference; track validity is shown separately")}">reference <b>${fmtTime(s.best)}</b></span><span title="${esc(s.typical_definition || "Median of rolling laps without detected major incidents or continuity failures; see track validity separately.")}">typical <b>${fmtTime(s.typical)}</b></span>
        <span>theoretical <b>${fmtTime(s.theoretical)}</b></span><span>coasting <b>${fmtNum(h.coast_pct)}%</b></span><span>full throttle <b>${fmtNum(h.full_pct)}%</b></span></summary>
        <div class="scroll-x"><table class="t"><thead><tr><th>Lap</th><th class="num">Time</th><th class="num">vs reference</th><th>Device</th><th></th><th class="num">Coasting</th><th class="num">Full throttle</th><th class="num">Shifts</th>
          <th class="num" title="Brake and throttle pressed together">${both}</th><th class="num" title="Clutch held down while rolling">Clutch down</th><th class="num">Handbrake</th></tr></thead><tbody>
        ${laps.map(l => `<tr class="click" data-file="${esc(l.file)}"><td><b>${l.number}</b></td><td class="num">${fmtTime(l.time)}</td>
          <td class="num ${l.time - a.ref_time > 0 ? "" : "gain"}">${fmtDelta(l.time - a.ref_time, 2)}</td>
          <td>${devChip(devOf(l), l.device_source, l.device_detail)}</td>
          <td>${l.file === a.ref ? '<span class="flag best">reference</span>' : l.file === sessionRef?.file ? '<span class="flag best">session reference</span>' : ""}${validityBadge(l)}${l.pace_eligible === false ? '<span class="flag bad">excluded from pace</span>' : ""}${l.quality?.warnings?.length ? `<span class="flag bad" title="${esc(l.quality.warnings.join("; "))}">data warning</span>` : ""}${l.standing ? '<span class="flag">standing start</span>' : ""}${l.incidents.map(i => `<span class="flag bad">${i.kind} ${esc(i.corner)}</span>`).join("")}</td>
          <td class="num">${fmtNum(l.totals.coast_pct)}%</td><td class="num">${fmtNum(l.totals.full_pct)}%</td><td class="num">${fmtNum(l.totals.shifts)}</td>
          <td class="num">${fmtNum(l.habits.two_feet_s, 2)} s</td><td class="num" ${l.habits.clutch_coast_s == null ? `title="${esc(na)}"` : ""}>${l.habits.clutch_coast_s == null ? "-" : fmtNum(l.habits.clutch_coast_s, 1) + " s"}</td><td class="num">${fmtNum(l.habits.handbrake_pulls)}</td></tr>`).join("")}
        </tbody></table></div></details>`;
    }).join("");
    $$("tr.click", host).forEach(tr => tr.onclick = () => { location.hash = `#/lap/${encodeURIComponent(id)}/${encodeURIComponent(tr.dataset.file)}`; });
  }

  function keyCornerCharts(host, a) {
    if (a.sessions.length < 2) { host.innerHTML = ""; return; }
    const avg = a.corners.map((_, k) => a.sessions.reduce((n, s) => n + (s.corners[k].loss || 0), 0) / a.sessions.length);
    const top = avg.map((v, k) => [v, k]).sort((x, y) => y[0] - x[0]).slice(0, 3).map(x => x[1]).sort((x, y) => x - y);
    const cat = a.sessions.map(s => fmtShort(s.start));
    const cols = CAT();
    const mk = (title, sub, key, unit, dec) => {
      const d = document.createElement("div"); d.className = "card";
      d.innerHTML = `<h2>${title}</h2><div class="sub">${sub}</div>${Charts.legend(top.map((k, i) => ({ name: a.corners[k].name, color: cols[i], dot: 1 })))}<div></div>`;
      host.appendChild(d);
      Charts.line(d.lastElementChild, {
        height: 170, cat, series: top.map((k, i) => ({ name: a.corners[k].name, color: cols[i], dots: true, points: a.sessions.map((s, si) => [si, key === "brake_sd" ? U.dv(s.corners[k][key]) : s.corners[k][key]]) })),
        yFmt: v => fmtNum(v, dec) + unit, xMin: -0.3, xMax: a.sessions.length - 0.7,
      });
    };
    host.innerHTML = "";
    if (a.sessions.some(s => isWheelish(devOf(s))))
      mk("Steering lock in your key corners", "The three corners where you lose the most time. Median peak steering angle per wheel session (controller and keyboard sessions have no wheel angle).", "steer_max", "°", 0);
    else
      mk("Steering input in your key corners", "The three corners where you lose the most time. Median peak steering input per session, % of full lock.", "steer_max_pct", "%", 0);
    mk("Brake point consistency", `How much your brake point moves from lap to lap (± ${U.dl}, standard deviation).`, "brake_sd", " " + U.dl, 1);
  }

  // ---------------------------------------------------------- lap view: replay + traces
  async function lapView(id, file, q) {
    view.innerHTML = `<div class="loading">Loading lap…</div>`;
    await getIndex();                          // display units (a lap URL opened directly has not loaded them yet)
    const [{ combo: c, analysis: a }, d] = await Promise.all([getCombo(id), api(`/api/lap?id=${encodeURIComponent(id)}&lap=${encodeURIComponent(file)}${q.ref ? "&ref=" + encodeURIComponent(q.ref) : ""}`)]);
    const L = d.lap, R = d.ref;
    const order = a.laps.map(l => l.file);
    const pos = order.indexOf(file);
    const lapRow = a.laps.find(l => l.file === file);
    const dt = L.time - R.time;
    const refOpts = a.laps.slice().sort((x, y) => x.time - y.time).map(l => `<option value="${esc(l.file)}" ${l.file === R.file ? "selected" : ""}>${l.file === a.ref ? "★ " : ""}Lap ${l.number} · ${fmtTime(l.time)} · ${fmtShort(a.sessions.find(s => s.session === l.session).start)} · ${esc(validityLabel(l))}${l.pace_eligible === false ? " · excluded from pace" : ""}</option>`).join("");
    const hasBeta = L.grid.beta && L.grid.beta.some(v => v != null && v !== 0);
    const lDev = devOf(L), rDev = devOf(R);
    // steering in degrees only when both laps were on a wheel; otherwise in % of full lock, and the reference's
    // steering is only shown when it was driven on the same kind of device
    const stPct = !(isWheelish(lDev) && isWheelish(rDev)) && L.grid.steer_pct && R.grid.steer_pct;
    const stSame = lDev === rDev || (isWheelish(lDev) && isWheelish(rDev));
    CTX = { page: "lap", id, lap: file, ref: q.ref || null, label: `Lap ${L.number} (${fmtTime(L.time)}), ${c.car_name} @ ${c.track_name}` };

    view.innerHTML = `
      <div class="crumbs"><a href="#/">Library</a> › <a href="#/combo/${encodeURIComponent(id)}?s=${encodeURIComponent(L.session)}">${esc(c.car_name)} @ ${esc(c.track_name)}</a></div>
      <div class="lap-head">
        <div class="times"><h1>Lap ${L.number}</h1><span class="big">${fmtTime(L.time)}</span>
          <span class="${dt > 0 ? "lose" : "gain"}" style="font-size:18px;font-weight:650">${fmtDelta(dt)} s</span>
          ${devChip(lDev, L.device_source, L.device_detail)}
          <span class="muted">${fmtDate(a.sessions.find(s => s.session === L.session)?.start)} · ${esc(validityLabel(lapRow || L))}${lapRow?.pace_eligible === false ? " · excluded from pace" : ""}${lapRow && lapRow.incidents.length ? " · " + lapRow.incidents.map(i => i.kind + " at " + i.corner).join(", ") : ""}</span></div>
        <div class="spacer"></div>
        <button class="btn small prompt" onclick="openPrompt()"><span aria-hidden="true">✦</span> AI prompt for this lap</button>
        <label class="f muted">Reference <select id="refsel">${refOpts}</select></label>
        <a class="btn small" ${pos > 0 ? `href="#/lap/${encodeURIComponent(id)}/${encodeURIComponent(order[pos - 1])}${q.ref ? "?ref=" + encodeURIComponent(q.ref) : ""}"` : "aria-disabled=true style='opacity:.4;pointer-events:none'"}>‹ Previous lap</a>
        <a class="btn small" ${pos < order.length - 1 ? `href="#/lap/${encodeURIComponent(id)}/${encodeURIComponent(order[pos + 1])}${q.ref ? "?ref=" + encodeURIComponent(q.ref) : ""}"` : "aria-disabled=true style='opacity:.4;pointer-events:none'"}>Next lap ›</a>
      </div>
      ${lapRow?.quality?.warnings?.length ? `<div class="note">Data warnings: ${esc(lapRow.quality.warnings.join("; "))}</div>` : ""}
      <div class="card replay" id="replay"></div>
      <div class="card traces" style="margin-top:16px">
        <div class="row"><h2>Traces</h2><span class="muted">vs ${esc(R.file === a.ref ? a.reference_role || "reference lap" : "lap " + R.label.split(" L")[1])} (${fmtTime(R.time)}) · hover to read values, click to move the replay there</span><div class="spacer"></div>
          <div class="corner-jump" id="zoom"><button data-z="all" class="on">Whole lap</button>${d.corners.map((cn, k) => `<button data-z="${k}">${esc(cn.name)}</button>`).join("")}</div></div>
        ${Charts.legend([{ name: "this lap", color: "var(--lap)" }, { name: "reference", color: "var(--ref)", dash: 1 }])}
        ${lDev !== rDev ? `<div class="note">The reference was driven on: ${esc(DEV_LABEL[rDev].toLowerCase())}. Brake points, speeds and throttle points compare; pedal pressure and steering aren't compared between devices.</div>` : ""}
        <div id="tr"></div>
      </div>
      <div class="stack" style="margin-top:16px">
        <div class="card"><h2>Corner by corner</h2><div class="sub">This lap vs the reference: measured differences only. For an interpretation, use ✦ AI prompt for this lap.</div><div class="scroll-x" id="lc"></div></div>
        <div class="card"><h2>Pedals and gears on this lap</h2><div class="sub">Measured over the whole lap.</div><div id="lh"></div></div>
      </div>`;

    $("#refsel").onchange = e => { location.hash = `#/lap/${encodeURIComponent(id)}/${encodeURIComponent(file)}?ref=${encodeURIComponent(e.target.value)}`; };

    // traces
    const g = L.grid, rg = R.grid, n = Math.min(g.d.length, rg.d.length);
    const zip = (gg, k) => gg.d.map((x, i) => [x, gg[k] ? gg[k][i] : null]);
    const delta = []; for (let i = 0; i < n; i++) delta.push([g.d[i], g.t[i] - rg.t[i]]);
    const bands = d.corners.map(cn => ({ x0: cn.a_d, x1: cn.b_d, label: cn.name }));
    const panels = [
      { title: `Speed (${U.sl})`, h: 190, series: [["ref", { d: rg.d, v: rg.speed_mph.map(x => U.fromMph(x)) }, "v", "--ref", true], ["lap", { d: g.d, v: g.speed_mph.map(x => U.fromMph(x)) }, "v", "--lap"]], fmt: v => fmtNum(v) },
      { title: "Time vs reference (s) · up = losing", h: 120, custom: [{ name: "delta", color: css("--lap"), points: delta, area: true }], zero: true, fmt: v => fmtDelta(v, 2) },
      { title: "Throttle (%)", h: 95, series: [["ref", rg, "gas", "--ref", true], ["lap", g, "gas", "--gas"]], ymin: 0, ymax: 1, fmt: v => Math.round(v * 100) },
      { title: "Brake (%)", h: 95, series: [["ref", rg, "brake", "--ref", true], ["lap", g, "brake", "--brake"]], ymin: 0, ymax: 1, fmt: v => Math.round(v * 100) },
      stPct ? { title: "Steering (% of full lock) · + = right", h: 120, series: [...(stSame ? [["ref", rg, "steer_pct", "--ref", true]] : []), ["lap", g, "steer_pct", "--lap"]], zero: true, ymin: -100, ymax: 100, fmt: v => fmtNum(v) }
        : { title: "Steering (°) · + = right", h: 120, series: [["ref", rg, "steer", "--ref", true], ["lap", g, "steer", "--lap"]], zero: true, fmt: v => fmtNum(v) },
    ];
    if (g.clutch && g.clutch.some(v => v != null && v < 0.9)) panels.push({ title: "Clutch pedal (% down)", h: 80, series: [["ref", { d: rg.d, c: rg.clutch.map(v => 1 - v) }, "c", "--ref", true], ["lap", { d: g.d, c: g.clutch.map(v => 1 - v) }, "c", "--clutch"]], ymin: 0, ymax: 1, fmt: v => Math.round(v * 100) });
    if (hasBeta) panels.push({ title: "Slide angle (°) · + = sliding right", h: 100, series: [["ref", rg, "beta", "--ref", true], ["lap", g, "beta", "--lap"]], zero: true, fmt: v => fmtNum(v) });
    panels.push({ title: "Gear", h: 80, series: [["ref", rg, "gear", "--ref", true], ["lap", g, "gear", "--lap"]], step: true, fmt: v => gearTxt(Math.round(v)) });

    const trHost = $("#tr");
    let replay = null;
    const charts = panels.map((p, pi) => {
      const div = document.createElement("div"); trHost.appendChild(div);
      const series = p.custom || p.series.map(([name, gg, k, col, dash]) => ({ name, color: css(col), points: zip(gg, k), dash, step: p.step }));
      return Charts.line(div, {
        height: p.h, title: p.title, series, bands, zero: p.zero, yMin: p.ymin, yMax: p.ymax, yFmt: p.fmt, xFmt: v => U.dist(v),
        noBandLabels: pi > 0, noXAxis: pi < panels.length - 1, padL: 48,
        onHover: x => charts.forEach((c2, j) => j !== pi && c2.setCursor(x)),
        onClick: x => replay && replay.seekDistance(x),
        tooltip: (x, hits) => `<div class="tt">${U.dist(x)}${(() => { const cn = d.corners.find(cn => x >= cn.a_d && x <= cn.b_d); return cn ? " · " + cn.name : ""; })()}</div>` +
          hits.map(h => `<div class="tr"><span><i class="sw" style="background:${h.s.color}"></i>${h.s.name}</span><span>${p.fmt(h.p[1])}</span></div>`).join(""),
      });
    });
    $("#zoom").onclick = e => {
      const z = e.target.dataset.z; if (z == null) return;
      $$("#zoom button").forEach(b => b.classList.toggle("on", b === e.target));
      if (z === "all") charts.forEach(ch => ch.setXRange(undefined, undefined));
      else { const cn = d.corners[+z]; charts.forEach(ch => ch.setXRange(Math.max(0, cn.a_d - 60), cn.b_d + 30)); replay && replay.seekDistance(Math.max(0, cn.a_d - 40)); }
    };

    replay = new Replay($("#replay"), { lap: L, ref: R, corners: d.corners, map: c.map }, {
      onTick: dist => charts.forEach(ch => ch.setPlayhead(dist)),
    });

    // corner table
    const slide = d.corners.some(cn => cn.lap.beta_peak != null);
    const m2 = v => v == null ? "-" : U.dist(v);
    $("#lc").innerHTML = `<table class="t"><thead><tr><th>Corner</th><th class="num">Time</th><th class="num" title="Lowest speed between the corner's boundaries (not a geometric apex)">Min speed ${U.sl}<div class="muted" style="text-transform:none">lap / ref</div></th><th class="num">Brake at</th><th class="num" title="Highest brake pedal input (0-100%), not brake pressure">Peak brake input</th><th class="num">Full throttle</th><th class="num">Steering</th>${slide ? '<th class="num">Slide</th>' : ""}<th class="wide">Differences</th></tr></thead><tbody>
      ${d.corners.map((cn, k) => `<tr class="click" data-k="${k}"><td><b>${esc(cn.name)}</b></td><td class="num ${cn.dt > 0.05 ? "lose" : cn.dt < -0.05 ? "gain" : ""}">${fmtDelta(cn.dt, 2)}</td>
        <td class="num">${U.kmh(cn.lap.min_speed)} / ${U.kmh(cn.ref.min_speed)}</td>
        <td class="num">${cn.lap.brake_at == null ? "no brake" : m2(cn.lap.brake_at)}<div class="muted">${cn.ref.brake_at == null ? "ref no brake" : "ref " + m2(cn.ref.brake_at)}</div></td>
        <td class="num">${cn.lap.brake_at == null || cn.lap.brake_peak == null ? "-" : fmtNum(cn.lap.brake_peak * 100) + "%"}</td>
        <td class="num">${m2(cn.lap.full_gas_at)}<div class="muted">ref ${m2(cn.ref.full_gas_at)}</div></td>
        <td class="num">${steerTxt(cn.lap, stPct)}<div class="muted">${stSame ? "ref " + steerTxt(cn.ref, stPct) : "ref: " + esc(DEV_LABEL[rDev].toLowerCase())}</div></td>
        ${slide ? `<td class="num">${fmtNum(cn.lap.beta_peak)}° <div class="muted">ref ${fmtNum(cn.ref.beta_peak)}°</div></td>` : ""}
        <td>${(cn.tips.race || []).length ? `<ul class="tips">${cn.tips.race.map(t => `<li>${esc(U.text(t))}</li>`).join("")}</ul>` : '<span class="muted">-</span>'}</td></tr>`).join("")}
      </tbody></table>`;
    $$("#lc tr.click").forEach(tr => tr.onclick = () => $(`#zoom button[data-z="${tr.dataset.k}"]`).click());

    // pedal / gear measurements (same definitions as the Trends page)
    const vals = Object.assign({}, L.habits, lapRow ? lapRow.totals : {});
    const hc = k => (d.habit_checks || []).find(h => h.key === k);
    const lbl = m => m.key === "two_feet_s" && !isWheelish(lDev) ? "Brake + throttle overlap (" + (lDev === "gamepad" ? "triggers" : "keys") + ")" : m.label;
    $("#lh").innerHTML = `<table class="t"><tbody><tr><td>Input device</td><td class="num">${devChip(lDev, L.device_source, L.device_detail)}</td></tr>${d.metrics.filter(m => m.group !== "Pace" && m.key !== "incidents" && m.key !== "brake_peak" && m.key !== "brake_sd").map(m => {
      const v = vals[m.key];
      return `<tr title="${esc(m.def)}"><td>${esc(lbl(m))}</td><td class="num">${v == null ? `<span class="muted">${esc(hc(m.key) ? hc(m.key).na : "not recorded")}</span>` : fmtMetric(m, v)}</td></tr>`;
    }).join("")}${lapRow && lapRow.incidents.length ? `<tr><td>Spin candidates and big slowdowns</td><td class="num">${lapRow.incidents.map(i => `${i.kind === "spin" ? "spin candidate" : i.kind} ${esc(i.corner)} (${U.dist(i.d)})`).join(", ")}</td></tr>` : ""}
    </tbody></table><div class="note" style="margin-top:8px">Hover a row for how it's measured.</div>`;
  }

  // metrics measured in metres follow the distance unit
  const isDist = m => m.unit === "m" || m.unit === "m/lap";
  const mScale = m => isDist(m) ? (U.distance === "ft" ? 3.28084 : 1) : m.scale;
  const mUnit = m => isDist(m) ? m.unit.replace("m", U.dl) : m.unit;
  const fmtMetric = (m, v) => v == null ? "-" : (v * mScale(m)).toFixed(m.dec) + (m.unit.startsWith("%") ? "%" + m.unit.slice(1) : " " + mUnit(m));

  // neutral change marker: the data says what moved, not whether that's good
  function change(now, before, m) {
    if (now == null || before == null) return "";
    const dec = m ? m.dec : 2, sc = m ? mScale(m) : 1;
    const d = +(now * sc).toFixed(dec) - +(before * sc).toFixed(dec);
    if (Math.abs(d) < Math.pow(10, -dec) / 2) return `<span class="muted">= same</span>`;
    return `<span class="chg">${d > 0 ? "▲" : "▼"} ${Math.abs(d).toFixed(m ? m.dec : 2)}${m && m.unit.startsWith("%") ? " pts" : ""}</span>`;
  }

  // ---------------------------------------------------------- trends (data only)
  async function habitsView(q) {
    const idx = await getIndex();
    const H = Object.assign({ game: "", combos: "", since: "all" }, store.get("habits", {}), q);
    const since = H.since === "all" ? 0 : Date.now() / 1000 - (+H.since) * 86400;
    view.innerHTML = `<div class="loading">Crunching trends…</div>`;
    const o = await api(`/api/overview?game=${H.game || ""}&combos=${encodeURIComponent(H.combos || "")}&since=${since}`);
    const combos = Object.fromEntries(idx.combos.map(c => [c.id, c]));
    const cname = id => combos[id] ? `${combos[id].car_name} @ ${combos[id].track_name}` : id;
    const games = Object.keys(idx.games).filter(g => idx.combos.some(c => c.game === g));
    const pts = o.points;
    const cat = pts.map(p => fmtShort(p.start));
    CTX = { page: "habits", game: H.game || null, combos: H.combos || null, since: since ? Math.floor(since) : null,
            label: "Trends" + (H.game ? " in " + idx.games[H.game] : " across your games") + (H.combos ? ", " + cname(H.combos) : "") + (H.since !== "all" ? ", last " + H.since + " days" : "") };
    const P = o.tip_phrases;

    view.innerHTML = `
      <div class="page-head"><div><h1>Trends</h1><div class="sub">What you do with the pedals, brakes and steering, session by session, measured the same way in every game. The app doesn't judge it: use ✦ Copy AI prompt for an interpretation.</div></div></div>
      <div class="filters"><div class="line">
        <div class="seg" id="h-game"><button data-v="">All games</button>${games.map(g => `<button data-v="${g}">${esc(idx.games[g])}</button>`).join("")}</div>
        <label class="f">Car / track <select id="h-combo"><option value="">All</option>${idx.combos.filter(c => !H.game || c.game === H.game).map(c => `<option value="${esc(c.id)}" ${H.combos === c.id ? "selected" : ""}>${esc(c.car_name)} @ ${esc(c.track_name)}</option>`).join("")}</select></label>
        <div class="seg" id="h-since"><button data-v="all">All time</button><button data-v="30">30 days</button><button data-v="7">7 days</button></div>
      </div></div>
      <div class="grid-60">
        <div class="card"><h2>Biggest time gaps</h2><div class="sub">Latest session of your three most recently driven car/tracks: the corners furthest from your own best time through them, and the differences measured there on most laps.</div><div class="gaps" id="gaps"></div></div>
        <div class="card"><h2>Changes since the previous session</h2><div class="sub">Same car and track, latest session vs the one before. Biggest relative changes first.</div><div class="scroll-x" id="chg"></div></div>
      </div>
      <div id="metrics"></div>
      <div id="kcs"></div>`;
    $$("#h-game button").forEach(b => b.classList.toggle("on", b.dataset.v === (H.game || "")));
    $$("#h-since button").forEach(b => b.classList.toggle("on", b.dataset.v === H.since));
    const go = patch => { Object.assign(H, patch); store.set("habits", { game: H.game, combos: H.combos, since: H.since }); location.hash = "#/habits"; if (location.hash === "#/habits") habitsView({}); };
    $("#h-game").onclick = e => { if (e.target.dataset.v != null) go({ game: e.target.dataset.v, combos: "" }); };
    $("#h-since").onclick = e => { if (e.target.dataset.v) go({ since: e.target.dataset.v }); };
    $("#h-combo").onchange = e => go({ combos: e.target.value });

    const mdef = Object.fromEntries(o.metrics.map(m => [m.key, m]));
    $("#gaps").innerHTML = o.gaps.length ? o.gaps.map(g => `<div class="gap">
      <div class="row"><a href="#/combo/${encodeURIComponent(g.combo)}?s=${encodeURIComponent(g.session)}" class="gt">${esc(cname(g.combo))}</a><span class="spacer"></span>
        <span class="muted">${g.laps} lap${g.laps === 1 ? "" : "s"}${g.only ? "" : ` · ${fmtNum(g.total_gap, 2)} s total gap to your best corners`}</span></div>
      ${g.corners.length ? `<table class="t"><tbody>${g.corners.map(c => `<tr><td style="width:46px"><b>${esc(c.corner)}</b></td><td class="num" style="width:70px">${fmtNum(c.loss, 2)} s</td>
        <td>${c.diffs.length ? c.diffs.map(([t, n]) => `${esc(P[t] || t)} <span class="muted">${n}/${g.laps}</span>`).join(" · ") : '<span class="muted">no single difference on most laps</span>'}</td></tr>`).join("")}</tbody></table>` : `<div class="note">${g.only ? "Only one lap on this car and track so far: nothing to compare it with yet." : "Every corner within 0.05 s of your best."}</div>`}
    </div>`).join("") : `<div class="note">No sessions in this filter.</div>`;
    $("#chg").innerHTML = o.changes.length ? `<table class="t"><thead><tr><th>Measurement</th><th>Car / track</th><th class="num">Before</th><th class="num">Now</th><th class="num">Change</th></tr></thead><tbody>${o.changes.map(x => {
      const m = mdef[x.metric];
      if (!m) return `<tr><td>Typical lap</td><td>${esc(cname(x.combo))}</td><td class="num">${fmtTime(x.before)}</td><td class="num">${fmtTime(x.now)}</td>
        <td class="num ${x.now < x.before ? "gain" : "lose"}">${fmtDelta(x.now - x.before, 2)} s</td></tr>`;
      return `<tr title="${esc(m.def)}"><td>${esc(m.label)}</td><td>${esc(cname(x.combo))}</td><td class="num">${fmtMetric(m, x.before)}</td><td class="num">${fmtMetric(m, x.now)}</td><td class="num">${change(x.now, x.before, m)}</td></tr>`;
    }).join("")}</tbody></table>` : `<div class="note">Drive a second session on the same car and track to see changes.</div>`;

    // one small chart per measurement, x = sessions in time order, one series per game
    const host = $("#metrics");
    const groups = [...new Set(o.metrics.map(m => m.group))];
    for (const gname of groups) {
      const withData = o.metrics.filter(m => m.group === gname && pts.some(p => p.values[m.key] != null));
      if (!withData.length) continue;
      host.insertAdjacentHTML("beforeend", `<div class="group-h">${esc(gname)}</div><div class="mcards"></div>`);
      const wrap = host.lastElementChild;
      for (const m of withData) {
        const series = games.filter(g => pts.some(p => p.game === g && p.values[m.key] != null)).map(g => ({
          name: idx.games[g], color: css("--" + g) || css("--lap"), dots: true,
          points: pts.map((p, i) => p.game === g && p.values[m.key] != null ? [i, p.values[m.key] * mScale(m), p] : null).filter(Boolean),
        }));
        const last = [...pts].reverse().find(p => p.values[m.key] != null);
        const prev = last && [...pts].reverse().find(p => p !== last && p.combo === last.combo && p.values[m.key] != null && p.start < last.start);
        const card = document.createElement("div"); card.className = "card mcard";
        card.innerHTML = `<div class="head"><div><h3>${esc(m.label)}</h3><div class="note">${esc(mUnit(m))}</div></div>
          <div class="now"><div class="v">${last ? fmtMetric(m, last.values[m.key]) : "-"}</div><div class="d">${last && prev ? change(last.values[m.key], prev.values[m.key], m) + " vs previous" : "latest session"}</div></div></div>
          ${series.length > 1 ? Charts.legend(series.map(s => ({ name: s.name, color: s.color, dot: 1 }))) : ""}<div class="mchart"></div>
          <div class="note def">${esc(U.text(m.def))}</div>`;
        wrap.appendChild(card);
        Charts.line(card.querySelector(".mchart"), {
          height: 150, cat, series, xMin: -0.4, xMax: Math.max(0.4, pts.length - 0.6), padL: 46,
          yMin: m.unit.startsWith("%") || m.key.endsWith("_s") || m.key === "incidents" || m.key === "handbrake_pulls" || m.key === "slide_m" ? 0 : undefined,
          yFmt: v => (+v).toFixed(m.dec),
          tooltip: (x, hits) => {
            const p = pts[Math.round(x)]; if (!p || p.values[m.key] == null) return "";
            return `<div class="tt">${esc(cname(p.combo))}</div><div class="tr"><span>${fmtDate(p.start)}</span><span>${p.laps} laps</span></div>
              <div class="tr"><span><i class="sw" style="background:${GAME_COLOR(p.game)}"></i>${esc(m.label)}</span><span>${fmtMetric(m, p.values[m.key])}</span></div>`;
          },
          onClick: x => { const p = pts[Math.round(x)]; if (p) location.hash = `#/combo/${encodeURIComponent(p.combo)}?s=${encodeURIComponent(p.session)}`; },
        });
      }
    }
    if (!pts.length) host.innerHTML = `<div class="empty" style="margin-top:16px">No sessions in this filter.</div>`;

    // key corners per car/track
    const kh = $("#kcs");
    const byCombo = {};
    for (const k of o.key_corners) (byCombo[k.combo] = byCombo[k.combo] || []).push(k);
    const multi = Object.entries(byCombo).filter(([, ks]) => ks[0].series.length > 1);
    if (multi.length) {
      kh.insertAdjacentHTML("beforeend", `<div class="group-h">Key corners: steering angle per session</div><div class="mcards"></div>`);
      const wrap = kh.lastElementChild;
      const cols = CAT();
      for (const [cid, ks] of multi) {
        const card = document.createElement("div"); card.className = "card";
        card.innerHTML = `<h3>${esc(cname(cid))}</h3><div class="sub">Median peak steering-wheel angle in the 3 corners with the biggest time gap. Dashed = the reference lap.</div>
          ${Charts.legend(ks.map((k, i) => ({ name: k.corner, color: cols[i], dot: 1 })))}<div></div>`;
        wrap.appendChild(card);
        const kcat = ks[0].series.map(s => fmtShort(s.start));
        Charts.line(card.lastElementChild, {
          height: 160, cat: kcat, xMin: -0.3, xMax: kcat.length - 0.7, yFmt: v => Math.round(v) + "°",
          series: ks.flatMap((k, i) => [{ name: k.corner, color: cols[i], dots: true, points: k.series.map((s, j) => [j, s.steer_max]) },
            ...(k.ref_steer ? [{ name: k.corner + " reference", color: cols[i], dash: true, width: 1, points: [[-0.3, k.ref_steer], [kcat.length - 0.7, k.ref_steer]] }] : [])]),
        });
      }
    }
  }

  initShell();
  route();
})();
