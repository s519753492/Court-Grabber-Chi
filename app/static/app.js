"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const FIRST_HOUR = 6, LAST_HOUR = 22, COURTS = [1, 2, 3, 4, 5, 6];
const LIVE = ["running", "awaiting_payment", "paying"];

let me = null;
let bookings = [];
let pollTimer = null;
let registering = false;

// ------------------------------------------------------------------ helpers

async function api(method, path, body) {
  const res = await fetch(path, {
    method, credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = "Please check the form: " + msg.map(d => d.loc.slice(-1)[0]).join(", ");
    const err = new Error(msg || `Something went wrong (${res.status}).`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function hourLabel(h) { return `${h % 12 || 12}:00 ${h < 12 ? "AM" : "PM"}`; }
function shortHour(h) { return `${h % 12 || 12}${h < 12 ? "am" : "pm"}`; }
function parseDate(iso) { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); }
function fmtDate(iso) { return parseDate(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }); }
function isoToday() { const d = new Date(); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
function addDays(iso, n) { const d = parseDate(iso); d.setDate(d.getDate() + n); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}

function setMsg(el, text, ok) { el.textContent = text; el.className = "msg " + (ok ? "ok" : "bad"); }

function hourOptions(sel, selected) {
  sel.innerHTML = "";
  for (let h = FIRST_HOUR; h <= LAST_HOUR; h++) {
    const o = new Option(hourLabel(h), h);
    if (h === selected) o.selected = true;
    sel.add(o);
  }
}

// ------------------------------------------------------------------ views

function show(view) {
  $$(".view").forEach(v => (v.hidden = v.id !== view));
  $("#tabs").hidden = view === "auth";
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  if (view === "settings") fillSettings();
  window.scrollTo(0, 0);
}

$$("#tabs button").forEach(b => b.addEventListener("click", () => show(b.dataset.view)));

// ------------------------------------------------------------------ auth

function setAuthMode(reg) {
  registering = reg;
  $("#authTitle").textContent = reg ? "Create your account" : "Sign in";
  $("#authBtn").textContent = reg ? "Create account" : "Sign in";
  $("#authSwitch").textContent = reg ? "Already have an account? Sign in" : "New here? Create an account";
  $$(".reg").forEach(el => (el.hidden = !reg));
  $("#authForm [name=password]").autocomplete = reg ? "new-password" : "current-password";
  $("#authErr").textContent = "";
}
$("#authSwitch").addEventListener("click", () => setAuthMode(!registering));

$("#authForm").addEventListener("submit", async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = { email: f.get("email"), password: f.get("password") };
  if (registering) Object.assign(body, { name: f.get("name"), invite: f.get("invite") });
  $("#authBtn").disabled = true;
  try {
    me = await api("POST", registering ? "/api/register" : "/api/login", body);
    e.target.reset();
    await enterApp(registering);
  } catch (err) {
    $("#authErr").textContent = err.message;
  } finally {
    $("#authBtn").disabled = false;
  }
});

async function enterApp(isNew) {
  show(isNew || !me.has_park_login ? "settings" : "home");
  await refresh();
  if (isNew) toast("Welcome! Add your Park District login and turn on notifications.", 5000);
}

$("#logout").addEventListener("click", async () => {
  await api("POST", "/api/logout").catch(() => {});
  me = null; bookings = [];
  setAuthMode(false); show("auth");
});

$("#deleteAcct").addEventListener("click", async () => {
  if (!confirm("Delete your account, your saved Park District login and all your requests? This can't be undone.")) return;
  try {
    await api("DELETE", "/api/me");
    me = null; setAuthMode(false); show("auth");
    toast("Account deleted.");
  } catch (err) { toast(err.message); }
});

// ------------------------------------------------------------------ bookings list

const STATUS = {
  scheduled: ["Waiting", ""],
  running: ["Booking now", "live"],
  awaiting_payment: ["Pay now", "live"],
  paying: ["Paying", "live"],
  booked: ["Booked", "good"],
  test_ok: ["Test OK", "good"],
  no_court: ["No court", "bad"],
  expired: ["Released", "bad"],
  released: ["Released", "bad"],
  failed: ["Problem", "bad"],
};

function describe(b) {
  const times = b.starts.map(shortHour).join(" / ");
  let courtsTxt = "courts " + b.courts.join(", ");
  if (b.courts.length === COURTS.length) {
    // favorites first, then the rest in order = "any"
    let fav = 0;
    while (fav < b.courts.length && b.courts.slice(fav).join() !== COURTS.filter(c => !b.courts.slice(0, fav).includes(c)).join()) fav++;
    courtsTxt = fav === 0 ? "any court" : `court${fav > 1 ? "s" : ""} ${b.courts.slice(0, fav).join(", ")}, then any`;
  }
  return `${times} · ${b.hours}h · ${courtsTxt}`;
}

function scheduledDetail(b) {
  if (b.is_test) return "Test starting...";
  const opens = new Date(b.opens_at);
  if (opens <= new Date()) return "Already open: trying right away...";
  return `Books automatically ${opens.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })} at 7:00 AM.`;
}

function render() {
  const list = $("#list");
  const visible = bookings;
  $("#empty").hidden = visible.length > 0;
  list.innerHTML = visible.map(b => {
    const [label, cls] = STATUS[b.status] || [b.status, ""];
    const when = b.court
      ? `${fmtDate(b.play_date)} · Court ${b.court} · ${hourLabel(b.start_hour)}`
      : fmtDate(b.play_date);
    const detail = b.status === "scheduled" ? scheduledDetail(b) : b.detail;
    return `<li class="item">
      <div class="main">
        <div class="when">${esc(when)}${b.is_test ? ' <span class="badge">Test</span>' : ""}
          <span class="badge ${cls}">${esc(label)}</span></div>
        <div class="what">${esc(describe(b))}${b.total ? " · " + esc(b.total) : ""}</div>
        ${detail ? `<div class="detail">${esc(detail)}</div>` : ""}
        ${b.has_shot ? `<a class="detail" href="/api/bookings/${b.id}/screenshot" target="_blank" rel="noopener">See what the bot saw</a>` : ""}
      </div>
      ${["running", "paying", "awaiting_payment"].includes(b.status) ? "" :
        `<button class="x" data-del="${b.id}" aria-label="Remove">×</button>`}
    </li>`;
  }).join("");

  renderPayCards();
  renderSetupNotice();
}

$("#list").addEventListener("click", async e => {
  const id = e.target.dataset.del;
  if (!id) return;
  const b = bookings.find(x => x.id == id);
  if (b && b.status === "scheduled" && !confirm(`Stop trying to book ${fmtDate(b.play_date)}?`)) return;
  try { await api("DELETE", `/api/bookings/${id}`); await refresh(); }
  catch (err) { toast(err.message); }
});

function renderSetupNotice() {
  const tips = [];
  if (me && !me.has_park_login) tips.push("<b>Add your Park District login</b> in Settings so the app can book for you.");
  if (!("Notification" in window) || Notification.permission !== "granted")
    tips.push("<b>Turn on notifications</b> in Settings so you don't miss the 9-minute pay window.");
  $("#setupNotice").innerHTML = tips.length ? `<div class="notice">${tips.join("<br>")}</div>` : "";
}

// ------------------------------------------------------------------ pay cards

function renderPayCards() {
  const wrap = $("#payCards");
  const held = bookings.filter(b => b.status === "awaiting_payment" || b.status === "paying");
  // keep any code the user is typing
  const typed = {};
  $$("input[data-cvv]", wrap).forEach(i => (typed[i.dataset.cvv] = i.value));
  wrap.innerHTML = held.map(b => `
    <section class="paycard" id="pay-${b.id}">
      <h2>Court held for you</h2>
      <div class="big">${esc(fmtDate(b.play_date))} · Court ${b.court} · ${hourLabel(b.start_hour)} (${b.hours}h)</div>
      <p>${esc(b.total || "")} · pay within <span class="timer" data-exp="${b.hold_expires_at}"></span></p>
      ${b.status === "paying" ? "<p><b>Paying...</b></p>" : `
      <p class="hint">${esc(b.detail || "")}</p>
      <form class="row" data-pay="${b.id}">
        <input data-cvv="${b.id}" inputmode="numeric" autocomplete="cc-csc" pattern="[0-9]{3,4}"
               maxlength="4" placeholder="CVV" aria-label="Card security code" required>
        <button class="btn primary">Pay with saved card</button>
      </form>
      <p class="hint">The 3–4 digit code on your saved card. Used once to pay, never stored.</p>
      <button class="link" data-release="${b.id}">I don't want it, release the court</button>`}
    </section>`).join("");
  $$("input[data-cvv]", wrap).forEach(i => (i.value = typed[i.dataset.cvv] || ""));
  tickTimers();
  if (location.hash === "#pay" && held.length) {
    const i = $("input[data-cvv]", wrap);
    if (i) i.focus();
    history.replaceState(null, "", "/");
  }
}

function tickTimers() {
  $$(".timer").forEach(t => {
    const left = Math.max(0, Math.round(Number(t.dataset.exp) - Date.now() / 1000));
    t.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  });
}
setInterval(tickTimers, 1000);

$("#payCards").addEventListener("submit", async e => {
  e.preventDefault();
  const id = e.target.dataset.pay;
  const input = $("input[data-cvv]", e.target);
  const btn = $("button", e.target);
  btn.disabled = true;
  try {
    await api("POST", `/api/bookings/${id}/pay`, { cvv: input.value.trim() });
    input.value = "";
    toast("Paying... this takes a few seconds.");
    await refresh();
  } catch (err) {
    toast(err.message);
    btn.disabled = false;
  }
});

$("#payCards").addEventListener("click", async e => {
  const id = e.target.dataset.release;
  if (!id || !confirm("Release this court? Someone else will be able to book it.")) return;
  try { await api("DELETE", `/api/bookings/${id}`); toast("Releasing the court..."); await refresh(); }
  catch (err) { toast(err.message); }
});

// ------------------------------------------------------------------ add sheet

const sheet = $("#sheet");
const addForm = $("#addForm");

function chip(label, value, pressed = false) {
  return `<button type="button" class="chip" data-v="${value}" aria-pressed="${pressed}">${label}</button>`;
}

function buildFallbackChips() {
  const start = Number($("#startSel").value);
  const keep = new Set($$("#fallbackChips .chip[aria-pressed=true]").map(c => Number(c.dataset.v)));
  let html = "";
  for (let h = FIRST_HOUR; h <= LAST_HOUR; h++) if (h !== start) html += chip(shortHour(h), h, keep.has(h));
  $("#fallbackChips").innerHTML = html;
}

function updateOpensHint() {
  const v = addForm.play_date.value;
  if (!v) { $("#opensHint").textContent = ""; return; }
  const opens = addDays(v, -6);
  $("#opensHint").textContent = opens <= isoToday()
    ? "This date is already open, so the app will try to book it right away."
    : `The app will grab it on ${fmtDate(opens)} at 7:00 AM, when booking opens.`;
}

$("#addBtn").addEventListener("click", () => {
  addForm.reset();
  addForm.play_date.min = isoToday();
  addForm.play_date.value = addDays(isoToday(), 7);
  hourOptions($("#startSel"), 18);
  $("#courtChips").innerHTML = COURTS.map(c => chip(`Court ${c}`, c)).join("");
  buildFallbackChips();
  updateOpensHint();
  $("#addErr").textContent = "";
  sheet.showModal();
});

$("#startSel").addEventListener("change", buildFallbackChips);
addForm.play_date.addEventListener("change", updateOpensHint);
sheet.addEventListener("click", e => {
  if (e.target.classList.contains("chip"))
    e.target.setAttribute("aria-pressed", e.target.getAttribute("aria-pressed") !== "true");
});

addForm.addEventListener("submit", async e => {
  e.preventDefault();
  if (e.submitter && e.submitter.value === "cancel") { sheet.close(); return; }
  const f = new FormData(addForm);
  const body = {
    play_date: f.get("play_date"),
    start: Number(f.get("start")),
    fallbacks: $$("#fallbackChips .chip[aria-pressed=true]").map(c => Number(c.dataset.v)),
    hours: Number(f.get("hours")),
    courts: $$("#courtChips .chip[aria-pressed=true]").map(c => Number(c.dataset.v)),
    any_court: !!f.get("any_court"),
    event_name: f.get("event_name") || "Tennis",
  };
  $("#addSave").disabled = true;
  try {
    await api("POST", "/api/bookings", body);
    sheet.close();
    toast("Saved. You'll get an alert when a court is held.");
    await refresh();
  } catch (err) {
    $("#addErr").textContent = err.message;
  } finally {
    $("#addSave").disabled = false;
  }
});

// ------------------------------------------------------------------ settings

function fillSettings() {
  if (!me) return;
  $("#parkForm").cpd_email.value = me.cpd_email || "";
  $("#parkForm").cpd_password.value = "";
  $("#parkForm").cpd_password.required = !me.has_park_login;
  $("#parkForm").cpd_password.placeholder = me.has_park_login ? "Saved (enter to change)" : "";
  $("#whoami").textContent = `Signed in as ${me.name} (${me.email}).`;
  const t = $("#testForm");
  t.play_date.min = isoToday(); t.play_date.max = addDays(isoToday(), 6);
  if (!t.play_date.value) t.play_date.value = addDays(isoToday(), 1);
  if (!$("#testStart").options.length) hourOptions($("#testStart"), 6);
  updatePushUi();
}

$("#parkForm").addEventListener("submit", async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  const pw = f.get("cpd_password");
  if (!pw) { setMsg($("#parkMsg"), "Enter your Park District password to save.", false); return; }
  try {
    await api("PUT", "/api/me/park-login", { cpd_email: f.get("cpd_email"), cpd_password: pw });
    me = await api("GET", "/api/me");
    fillSettings();
    setMsg($("#parkMsg"), "Saved. Tap Test sign-in to check it.", true);
  } catch (err) { setMsg($("#parkMsg"), err.message, false); }
});

$("#testLogin").addEventListener("click", async e => {
  e.target.disabled = true;
  setMsg($("#parkMsg"), "Signing in to the Park District site... (about 15 seconds)", true);
  try {
    const r = await api("POST", "/api/me/park-login/test");
    setMsg($("#parkMsg"), r.message, r.ok);
  } catch (err) { setMsg($("#parkMsg"), err.message, false); }
  finally { e.target.disabled = false; }
});

$("#testForm").addEventListener("submit", async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("POST", "/api/bookings", {
      play_date: f.get("play_date"), start: Number(f.get("start")), fallbacks: [],
      hours: 1, courts: [], any_court: true, event_name: "Tennis", is_test: true,
    });
    toast("Test started. Watch it on the Courts tab.");
    show("home"); await refresh();
  } catch (err) { toast(err.message); }
});

// ------------------------------------------------------------------ push notifications

function isIos() { return /iphone|ipad|ipod/i.test(navigator.userAgent); }
function isStandalone() { return matchMedia("(display-mode: standalone)").matches || navigator.standalone; }

function updatePushUi() {
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const btn = $("#pushBtn");
  if (!supported) {
    btn.disabled = true;
    $("#pushHint").innerHTML = isIos() && !isStandalone()
      ? "On iPhone, first add this app to your home screen: tap <b>Share</b> → <b>Add to Home Screen</b>, then open it from there and come back here."
      : "This browser doesn't support notifications. Try Chrome on Android or Safari on iPhone (from the home screen).";
    return;
  }
  btn.disabled = false;
  btn.textContent = Notification.permission === "granted" ? "Notifications are on (re-connect)" : "Turn on notifications";
}

function b64ToUint8(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

$("#pushBtn").addEventListener("click", async () => {
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { setMsg($("#pushMsg"), "Notifications were blocked. Allow them in your phone's settings for this app.", false); return; }
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToUint8(me.vapid_key) });
    await api("POST", "/api/push/subscribe", sub.toJSON());
    setMsg($("#pushMsg"), "Notifications are on for this phone.", true);
    updatePushUi(); renderSetupNotice();
  } catch (err) { setMsg($("#pushMsg"), "Couldn't turn on notifications: " + err.message, false); }
});

$("#pushTest").addEventListener("click", async () => {
  try { await api("POST", "/api/push/test"); setMsg($("#pushMsg"), "Sent. It should arrive in a few seconds.", true); }
  catch (err) { setMsg($("#pushMsg"), err.message, false); }
});

// ------------------------------------------------------------------ data refresh

async function refresh() {
  if (!me) return;
  try {
    bookings = await api("GET", "/api/bookings");
    render();
  } catch (err) {
    if (err.status === 401) { me = null; setAuthMode(false); show("auth"); return; }
  }
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(pollTimer);
  const busy = bookings.some(b => LIVE.includes(b.status) || (b.status === "scheduled" && new Date(b.opens_at) - Date.now() < 10 * 60000));
  pollTimer = setTimeout(refresh, busy ? 2500 : 30000);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

// ------------------------------------------------------------------ start

(async function init() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  setAuthMode(false);
  try {
    me = await api("GET", "/api/me");
    show("home");
    await refresh();
  } catch {
    show("auth");
  }
})();
