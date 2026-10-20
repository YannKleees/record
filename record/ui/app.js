const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// status from the recorder -> [chip text, tone, tooltip]. Obvious states are icon-only.
const STATUS = {
  recording: ["Rec", "rec", "Recording"],
  converting: ["", "busy", "Encoding…"],
  saved: ["", "ok", "Saved"],
  "already saved": ["", "ok", "Already in folder"],
  partial: ["Skipped", "warn", "Skipped before the end, not saved"],
  "too short": ["Too short", "warn", "Under 30 seconds, not saved"],
  failed: ["Failed", "err", "Couldn't encode"],
};
const DONE = ["saved", "already saved"];
const ISSUES = ["partial", "too short", "failed"];
const FILTERS = {
  all: () => true,
  done: (r) => DONE.includes(r.status),
  issues: (r) => ISSUES.includes(r.status),
};
const FORMAT_NAMES = { mp3: "MP3 320", flac: "FLAC" };
const MODES = { "Waiting for music": "armed", Recording: "recording", Paused: "paused", "Finishing conversions": "finishing" };
const SEGMENTS = 32;

let api;
const state = {
  settings: {}, library: [], sources: [], view: null, data: null, rows: new Map(), filter: "all", query: "", mode: "idle",
  recId: null, now: null, nowKey: null, recorded: false, stopRequested: false, savedNow: 0, level: 0, shown: 0,
};
const media = matchMedia("(prefers-color-scheme: dark)");
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");

/* ---------- helpers ---------- */

function duration(sec) {
  if (!sec) return "";
  const s = Math.round(sec), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`;
}

function longDuration(sec) {
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
}

const basename = (path) => path.split(/[\\/]/).filter(Boolean).pop() || path;

function bytes(n) {
  return n >= 1e9 ? [(n / 1e9).toFixed(1), "GB"] : [Math.round(n / 1e6), "MB"];
}

function setBig(el, value, unit) {
  el.replaceChildren(String(value), Object.assign(document.createElement("small"), { textContent: unit }));
}

function toast(text, tone = "info", ms = 4000) {
  const box = $("#toasts"), el = document.createElement("div");
  el.className = "toast";
  el.dataset.tone = tone;
  el.innerHTML = "<i></i><span></span>";
  el.lastChild.textContent = text;
  box.append(el);
  while (box.children.length > 3) box.firstChild.remove();
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 260); }, ms);
}

function sourceName() {
  const id = state.settings.source;
  return id ? state.sources.find((s) => s.id === id)?.name || "the chosen app" : "any app";
}

/* ---------- theme ---------- */

function applyTheme() {
  const t = state.settings.theme === "system" ? (media.matches ? "dark" : "light") : state.settings.theme;
  document.documentElement.dataset.theme = t;
  api.set_dark(t === "dark");
}
media.addEventListener("change", () => state.settings.theme === "system" && applyTheme());

/* ---------- navigation ---------- */

function showView(name) {
  for (const v of ["welcome", "collection", "settings"]) $(`#view-${v}`).hidden = v !== name;
}

function markNav() {
  $$("[data-nav]").forEach((b) => b.classList.toggle("active", b.dataset.nav === state.view));
  $$(".lib-item").forEach((b) => b.classList.toggle("active", b.dataset.id === state.view));
}

async function navigate(target) {
  state.view = target;
  markNav();
  if (target === "settings") {
    showView("settings");
    return refreshSources();
  }
  if (!target) return showView("welcome");
  // paint what we already know at once, then fill in the tracks
  state.query = $("#search").value = "";
  showView("collection");
  paintHeader(state.library.find((e) => e.id === target) || { name: "" }, true);
  const data = await api.open(target);
  if (state.view !== target) return;
  if (data.error) return navigate(state.library[0]?.id || null);
  renderCollection(data);
}

/* ---------- library ---------- */

function renderLibrary() {
  $("#library").replaceChildren(...state.library.map((e) => {
    const btn = document.createElement("button");
    btn.className = "lib-item";
    btn.dataset.id = e.id;
    btn.title = e.folder;
    btn.classList.toggle("active", e.id === state.view);
    btn.classList.toggle("recording", e.id === state.recId && state.mode !== "idle");
    const tile = Object.assign(document.createElement("span"), { className: "lib-cover", textContent: (e.name || "?").trim()[0] });
    const text = Object.assign(document.createElement("span"), { className: "lib-text" });
    text.append(Object.assign(document.createElement("span"), { className: "lib-name", textContent: e.name }),
                Object.assign(document.createElement("span"), { className: "lib-sub", textContent: `${e.saved} tracks` }));
    btn.append(tile, text);
    btn.addEventListener("click", () => navigate(e.id));
    return btn;
  }));
}

async function refreshLibrary() {
  state.library = await api.library();
  renderLibrary();
}

/* ---------- collection view ---------- */

function paintHeader(entry, loading = false) {
  const view = $("#view-collection"), root = document.documentElement.style;
  $("#col-name").textContent = entry.name;
  $("#col-name").classList.toggle("long", (entry.name || "").length > 14);  // long names get a smaller heading so they fit
  if (entry.cover) root.setProperty("--cover", `url("${entry.cover}")`);
  else root.removeProperty("--cover");
  $("#sleeve").classList.toggle("has-cover", !!entry.cover);
  if (entry.folder) {
    $("#folder-name").textContent = basename(entry.folder);
    $("#folder-btn").title = `Open ${entry.folder}`;
  }
  if (loading) {
    $("#col-meta").textContent = "";
    $("#list").classList.add("skeleton");
    $("#list").replaceChildren(...Array.from({ length: 8 }, () => {
      const el = makeRowEl();
      $(".title", el).textContent = $(".artist", el).textContent = "·";
      return el;
    }));
    view.classList.remove("is-empty");
  }
  view.classList.toggle("is-live", state.recId === entry.id);
  renderRecButton();
}

function renderCollection(data) {
  state.data = data;
  const { entry, tracks } = data;
  paintHeader(entry);
  state.rows.clear();
  $("#list").classList.remove("skeleton");
  $("#list").replaceChildren();
  for (const t of tracks) addRow(t);
  for (const x of data.extras) if (x.status !== "saved") addRow({ ...x });
  if (state.recId === entry.id && state.nowKey != null) state.rows.get(state.nowKey)?.el.classList.add("current");
  renderList();
}

function makeRowEl() {
  const el = document.createElement("div");
  el.className = "row";
  el.setAttribute("role", "listitem");
  el.innerHTML = '<span class="pos"></span><div class="song"><div class="title"></div><div class="artist"></div></div>' +
                 '<span class="len"></span><span class="chip"><i></i><span></span></span>';
  return el;
}

function addRow(r) {
  r.el = makeRowEl();
  const cid = state.view;
  r.el.addEventListener("dblclick", () => DONE.includes(r.status) && api.reveal(cid, r.key));
  state.rows.set(r.key, r);
  $("#list").append(r.el);
  paintRow(r);
  return r;
}

function paintRow(r) {
  const [text, tone, tip] = STATUS[r.status] || [r.status, "", r.status];
  const el = r.el, chip = $(".chip", el);
  $(".pos", el).textContent = r.num ? String(r.num).padStart(2, "0") : "·";
  $(".title", el).textContent = r.title;
  $(".artist", el).textContent = r.artist;
  $(".len", el).textContent = duration(r.duration);
  chip.dataset.tone = tone;
  chip.classList.toggle("icon-only", !text);
  chip.lastChild.textContent = text;
  chip.title = r.detail && !DONE.includes(r.status) ? `${tip}: ${r.detail}` : tip;
  el.dataset.status = r.status;
  el.title = DONE.includes(r.status) ? "Double-click to show the file" : "";
}

function renderList() {
  const rows = [...state.rows.values()];
  const q = state.query;
  let visible = 0;
  for (const r of rows) {
    r.el.hidden = !FILTERS[state.filter](r) || (q && !`${r.title} ${r.artist}`.toLowerCase().includes(q));
    visible += !r.el.hidden;
  }
  $("#view-collection").classList.toggle("is-empty", rows.length === 0);
  let note = $("#list .list-note");
  if (!visible && rows.length) {
    note ||= Object.assign(document.createElement("p"), { className: "list-note" });
    note.textContent = q ? "No match" : { done: "Nothing saved yet", issues: "Nothing skipped" }[state.filter];
    $("#list").append(note);
  } else note?.remove();
  for (const [name, test] of Object.entries(FILTERS)) $(`[data-count="${name}"]`).textContent = rows.filter(test).length;
  renderStats();
}

function renderStats() {
  if (!state.data) return;
  const saved = [...state.rows.values()].filter((r) => r.status === "saved");
  $("#stat-saved").textContent = saved.length;
  const live = state.recId === state.view && state.mode !== "idle";
  $("#stat-session").textContent = live ? `+${state.savedNow} this session` : "in this folder";
  const total = saved.reduce((sum, r) => sum + (r.duration || 0), 0);
  const mins = Math.round(total / 60);
  if (mins >= 60) setBig($("#stat-length"), `${Math.floor(mins / 60)}:${String(mins % 60).padStart(2, "0")}`, "h");
  else setBig($("#stat-length"), mins, "min");
  $("#col-meta").textContent = saved.length ? `${saved.length} tracks · ${longDuration(total)}` : "Empty";
  const { used, free } = state.data.disk;
  setBig($("#stat-size"), ...bytes(used));
  $("#stat-free").textContent = `${bytes(free).join(" ")} free`;
}

function renderRecButton() {
  const btn = $("#rec-btn"), label = $("#rec-label"), here = state.recId === state.view, busy = state.mode !== "idle";
  btn.classList.toggle("live", busy && here);
  btn.disabled = busy;
  btn.title = busy && !here ? "Already recording into another collection" : "Record what plays next";
  label.textContent = busy && here ? "Recording" : "Record";
  label.classList.toggle("live", busy && here);
  label.classList.toggle("off", busy && !here);
}

/* ---------- recording ---------- */

function setMode(mode) {
  state.mode = mode;
  document.body.dataset.mode = mode;
  renderRecButton();
  renderPlayer();
  renderLibrary();
  renderStats();
  $("#view-collection").classList.toggle("is-live", state.recId === state.view);
}

function renderPlayer() {
  const { mode, recorded, stopRequested, now } = state;
  const name = state.library.find((e) => e.id === state.recId)?.name || "";
  const [title, sub] = {
    armed: [recorded ? "Waiting for the next track" : `Play something in ${sourceName()}`, name],
    recording: [now?.title, [now?.artist, now?.app].filter(Boolean).join(" · ")],
    paused: [now?.title, "Paused"],
    finishing: ["Saving the last track…", name],
  }[mode] || ["", ""];
  $("#now-title").textContent = title || "";
  $("#now-sub").textContent = sub || "";
  $("#stop-label").textContent = mode === "recording" ? (stopRequested ? "Stopping…" : "Stop after track") : "Stop";
  $("#stop-btn").disabled = mode === "finishing" || stopRequested;
  $("#discard-btn").hidden = mode === "finishing";
}

async function startRecording() {
  if (state.mode !== "idle") return;
  const cid = state.view;
  // set everything up before asking Python: the recorder can report its first track before start() returns
  Object.assign(state, { recId: cid, recorded: false, stopRequested: false, nowKey: null, now: null, savedNow: 0 });
  for (const r of [...state.rows.values()]) {
    if (r.status !== "saved") { r.el.remove(); state.rows.delete(r.key); }
  }
  $("#time").textContent = "";
  $("#progress").style.width = "0";
  renderList();
  setMode("armed");
  const res = await api.start(cid);
  if (res.error) {
    state.recId = null;
    setMode("idle");
    toast(res.error, "err", 7000);
  }
}

const handlers = {
  state(e) {
    if (MODES[e.text]) setMode(MODES[e.text]);
  },
  now(e) {
    Object.assign(state, { recorded: true, nowKey: e.key, now: { title: e.title, artist: e.artist, app: e.app } });
    if (state.view === e.cid) {
      for (const r of state.rows.values()) r.el.classList.toggle("current", r.key === e.key);
      state.rows.get(e.key)?.el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    renderPlayer();
  },
  tick(e) {
    $("#time").textContent = `${duration(e.seconds) || "0:00"} / ${duration(e.duration) || "–:––"}`;
    $("#progress").style.width = e.duration ? `${Math.min(e.seconds / e.duration, 1) * 100}%` : "0";
    state.level = Math.max(state.level, e.level);
  },
  status(e) {
    if (e.status === "saved") {
      state.savedNow++;
      const lib = state.library.find((x) => x.id === e.cid);
      if (lib) { lib.saved++; renderLibrary(); }
    }
    if (e.status === "failed") toast(`Couldn't save ${e.title}`, "err", 7000);
    if (state.view !== e.cid) return;
    const r = state.rows.get(e.key) || addRow({ key: e.key });
    Object.assign(r, { title: e.title, artist: e.artist, duration: e.duration || r.duration, status: e.status, detail: e.detail });
    if (e.status === "saved") r.num = parseInt(e.detail, 10);
    paintRow(r);
    if (e.status === "saved") {
      r.el.classList.remove("flash");
      void r.el.offsetWidth;
      r.el.classList.add("flash");
    }
    renderList();
  },
  error(e) {
    toast(e.text, "err", 8000);
  },
  warning(e) {
    if (e.reason === "crossfade")
      showBanner("Crossfade is on", "Recording stopped. Turn off crossfade and automix in your player, then record again.", e.reason);
  },
  async done(e) {
    Object.assign(state, { stopRequested: false, nowKey: null });
    setMode("idle");
    state.recId = null;
    await refreshLibrary();
    if (state.view === e.cid) navigate(e.cid);
  },
};
window.onEvent = (e) => handlers[e.kind]?.(e);

function showBanner(title, text, reason = "") {
  $("#banner-title").textContent = title;
  $("#banner-sub").textContent = text;
  $("#banner").dataset.reason = reason;
  $("#banner").hidden = false;
}

function buildMeter() {
  const meter = $("#meter");
  for (let i = 0; i < SEGMENTS; i++) {
    const seg = document.createElement("i");
    if (i >= SEGMENTS * 0.75) seg.classList.add("hot");
    if (i >= SEGMENTS * 0.91) seg.classList.add("peak");
    meter.append(seg);
  }
  const segs = [...meter.children];
  let lastLit = -1;
  (function frame() {
    state.shown = Math.max(state.level, state.shown * 0.965);  // slow fall, like a real VU meter
    state.level = 0;
    const lit = state.mode === "recording" ? Math.round(state.shown * SEGMENTS) : 0;
    if (lit !== lastLit) segs.forEach((s, i) => s.classList.toggle("on", i < lit));
    lastLit = lit;
    requestAnimationFrame(frame);
  })();
}

/* ---------- new collection ---------- */

function openAdd() {
  const dialog = $("#add-dialog");
  if (!dialog.open) dialog.showModal();
  $("#add-name").value = "";
  $("#add-msg").textContent = "";
  updateAddFolder();
  $("#add-name").focus();
}

function updateAddFolder() {
  const name = $("#add-name").value.trim().replace(/[<>:"/\\|?*]/g, "");
  $("#add-folder").textContent = `${state.settings.base}\\${name || "…"}`;
}

function added(res) {
  if (!res) return;
  if (res.error) return ($("#add-msg").textContent = res.error);
  state.library = res.library;
  renderLibrary();
  $("#add-dialog").close();
  navigate(res.id);
}

/* ---------- settings ---------- */

function fillSettings() {
  const s = state.settings;
  $("#base").textContent = s.base;
  $$('#format input').forEach((r) => (r.checked = r.value === s.format));
  $$('#theme input').forEach((r) => (r.checked = r.value === s.theme));
  const select = $("#source");
  select.replaceChildren(new Option("Any app (whatever Windows shows as playing)", ""),
                         ...state.sources.map((src) => new Option(src.name, src.id)));
  select.value = s.source;
}

async function refreshSources() {
  state.sources = await api.sources();
  fillSettings();
}

async function updateSettings(changes) {
  const res = await api.update(changes);
  if (res.error) {
    fillSettings();
    return toast(res.error, "warn");
  }
  state.settings = res.settings;
  fillSettings();
}

/* ---------- system status: silent when fine, specifics only when not ---------- */

function applyChecks(c) {
  const problems = [
    !c.ffmpeg && ["ffmpeg missing", "Install with: winget install Gyan.FFmpeg"],
    !c.device && ["No speakers", "No audio output device found"],
  ].filter(Boolean);
  $("#checks").replaceChildren(...problems.map(([text, tip]) => {
    const li = document.createElement("li");
    li.className = "bad";
    li.innerHTML = '<span class="led"></span>';
    li.append(text);
    li.title = tip;
    return li;
  }));
}

/* ---------- wiring ---------- */

function confirmClick(button, title, action) {
  // destructive buttons need a second click within 3 seconds
  const original = button.title;
  let timer;
  button.addEventListener("click", () => {
    if (!button.classList.contains("confirm")) {
      button.classList.add("confirm");
      button.title = title;
      timer = setTimeout(() => { button.classList.remove("confirm"); button.title = original; }, 3000);
      return;
    }
    clearTimeout(timer);
    button.classList.remove("confirm");
    button.title = original;
    action();
  });
}

function wire() {
  $$("[data-nav]").forEach((b) => b.addEventListener("click", () => navigate(b.dataset.nav)));
  $("#add-btn").addEventListener("click", openAdd);
  $$('[data-action="add"]').forEach((b) => b.addEventListener("click", openAdd));

  // collection header
  $("#rec-btn").addEventListener("click", startRecording);
  $("#folder-btn").addEventListener("click", () => api.open_folder(state.view));
  $("#folder-change").addEventListener("click", async () => {
    const res = await api.change_folder(state.view);
    if (!res) return;
    state.library = res.library;
    renderLibrary();
    renderCollection(res.view);
  });
  confirmClick($("#remove-btn"), "Click again to remove (files are kept)", async () => {
    const res = await api.remove(state.view);
    if (res.error) return toast(res.error, "warn");
    state.library = res.library;
    renderLibrary();
    navigate(state.library[0]?.id || null);
  });
  $$("#tabs [role=tab]").forEach((tab) => tab.addEventListener("click", () => {
    $$("#tabs [role=tab]").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    state.filter = tab.dataset.filter;
    renderList();
  }));

  $("#banner-close").addEventListener("click", () => ($("#banner").hidden = true));

  // player
  $(".player-now").addEventListener("click", () => state.recId && state.view !== state.recId && navigate(state.recId));
  $$("[data-control]").forEach((b) => b.addEventListener("click", () => api.control(b.dataset.control)));
  $("#stop-btn").addEventListener("click", () => {
    api.stop(false);
    if (state.mode === "recording") state.stopRequested = true;
    renderPlayer();
  });
  confirmClick($("#discard-btn"), "Click again to discard this track", () => api.stop(true));

  // new collection dialog
  $("#add-name").addEventListener("input", () => { $("#add-msg").textContent = ""; updateAddFolder(); });
  $("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    added(await api.add($("#add-name").value));
  });
  $("#add-existing").addEventListener("click", async () => added(await api.add_folder()));
  $("#add-close").addEventListener("click", () => $("#add-dialog").close());
  $("#add-dialog").addEventListener("click", (e) => e.target === e.currentTarget && e.currentTarget.close());

  $("#search").addEventListener("input", (e) => {
    state.query = e.target.value.trim().toLowerCase();
    renderList();
  });
  // Ctrl+F searches. No single-key shortcut for Record: stray typing must never start a recording
  document.addEventListener("keydown", (e) => {
    const collectionOpen = !$("#view-collection").hidden && !$("#add-dialog").open;
    if (e.ctrlKey && e.key.toLowerCase() === "f" && collectionOpen) {
      e.preventDefault();
      $("#search").focus();
    } else if (e.key === "Escape" && e.target.id === "search") {
      e.target.value = state.query = "";
      renderList();
      e.target.blur();
    }
  });

  // settings
  // the new theme grows as a circle from where you clicked
  $("#theme").addEventListener("pointerdown", (e) => {
    document.documentElement.style.setProperty("--vt-x", `${e.clientX}px`);
    document.documentElement.style.setProperty("--vt-y", `${e.clientY}px`);
  });
  $$('#theme input').forEach((r) => r.addEventListener("change", () => {
    state.settings.theme = r.value;
    if (document.startViewTransition && !reduceMotion.matches) document.startViewTransition(applyTheme);
    else applyTheme();
    updateSettings({ theme: r.value });
  }));
  $$('#format input').forEach((r) => r.addEventListener("change", () => updateSettings({ format: r.value })));
  $("#source").addEventListener("focus", refreshSources);
  $("#source").addEventListener("change", (e) => updateSettings({ source: e.target.value }));
  $("#browse").addEventListener("click", async () => {
    const res = await api.choose_base();
    if (res?.settings) { state.settings = res.settings; fillSettings(); }
  });
}

// the record button leans toward the cursor when it comes close
function magnetic(button, area) {
  const reset = () => { button.style.setProperty("--mx", "0px"); button.style.setProperty("--my", "0px"); };
  area.addEventListener("pointermove", (e) => {
    if (button.disabled || reduceMotion.matches) return reset();
    const r = button.getBoundingClientRect();
    const dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
    const pull = Math.max(0, 1 - Math.hypot(dx, dy) / 140) * 0.35;
    button.style.setProperty("--mx", `${dx * pull}px`);
    button.style.setProperty("--my", `${dy * pull}px`);
  });
  area.addEventListener("pointerleave", reset);
}

function buildFormats(formats) {
  $("#format").replaceChildren(...formats.map((f) => {
    const label = document.createElement("label");
    label.title = f.label;
    label.innerHTML = `<input type="radio" name="format" value="${f.id}"><b></b>`;
    label.querySelector("b").textContent = FORMAT_NAMES[f.id] || f.label;
    return label;
  }));
}

async function init() {
  if (api) return;
  api = window.pywebview.api;
  const data = await api.init();
  state.settings = data.settings;
  state.library = data.library;
  applyTheme();
  buildFormats(data.formats);
  buildMeter();
  fillSettings();
  applyChecks(data.checks);
  wire();
  magnetic($("#rec-btn"), $(".hero"));
  magnetic($(".welcome .rec"), $("#view-welcome"));
  renderLibrary();
  const start = state.settings.selected;
  navigate(state.library.some((e) => e.id === start) ? start : state.library[0]?.id || null);
  refreshSources();
  setInterval(async () => applyChecks(await api.checks()), 4000);
}

if (window.pywebview?.api) init();
else window.addEventListener("pywebviewready", init);
