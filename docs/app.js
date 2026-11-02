// Marktbericht-Web-App: liest data/latest.json (von finanz_app.py erzeugt) und zeigt es in Tabs an.
"use strict";

const TABS = { heute: "Heute", watchlist: "Watchlist", maerkte: "Märkte", news: "News", archiv: "Archiv" };
const RANGES = [["1M", 21], ["3M", 63], ["6M", 126], ["1J", 0]];
const WEEKDAYS = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

const $ = (id) => document.getElementById(id);
const state = { latest: null, report: null, archive: null, newsTab: "markt", archived: null };

// ------------------------------------------------------------ Formatierung

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function num(x, d = 2) {
  if (x === null || x === undefined) return "–";
  const [i, f] = Math.abs(x).toFixed(d).split(".");
  const neg = x < 0 && Number(Math.abs(x).toFixed(d)) !== 0;
  return (neg ? "-" : "") + i.replace(/\B(?=(\d{3})+(?!\d))/g, ".") + (f ? "," + f : "");
}
function signed(x, d = 2, unit = " %") {
  if (x === null || x === undefined) return "–";
  return (x > 0 ? "+" : "") + num(x, d) + unit;
}
const fxDigits = (v) => (v < 10 ? 4 : 2); // EUR/USD 1,1216 - EUR/JPY 177,10
const cls = (x) => (x === null || x === undefined ? "flat" : x > 0.05 ? "up" : x < -0.05 ? "down" : "flat");
function dayLabel(iso) {
  const d = new Date(iso.slice(0, 10) + "T12:00:00Z");
  return `${WEEKDAYS[d.getUTCDay()]} ${iso.slice(8, 10)}.${iso.slice(5, 7)}.`;
}
function stampLabel(t) { return `${dayLabel(t)} ${t.slice(11, 16)} Uhr`; }
function ago(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 60000;
  if (diff < 60) return `vor ${Math.max(1, Math.round(diff))} Min.`;
  if (diff < 24 * 60) return `vor ${Math.round(diff / 60)} Std.`;
  const d = new Date(iso);
  return `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}.`;
}

// ------------------------------------------------------------ Favoriten (nur auf diesem Geraet)

function favorites() {
  try { return new Set(JSON.parse(localStorage.getItem("favoriten") || "[]")); } catch (e) { return new Set(); }
}
function toggleFavorite(sym) {
  const f = favorites();
  f.has(sym) ? f.delete(sym) : f.add(sym);
  try { localStorage.setItem("favoriten", JSON.stringify([...f])); } catch (e) { /* privater Modus */ }
}

// ------------------------------------------------------------ Charts

function slice(hist, n) {
  if (!hist || !hist.c) return [];
  return n ? hist.c.slice(-n) : hist.c;
}
function spark(values, positive) {
  if (values.length < 2) return "";
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 100},${28 - ((v - min) / span) * 26}`).join(" ");
  return `<svg viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${pts}" fill="none" stroke="var(--${positive ? "up" : "down"})" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}
function bigChart(values, fmt) {
  if (values.length < 2) return '<p class="muted small">Für diesen Zeitraum liegt kein Kursverlauf vor.</p>';
  const W = 340, H = 190, top = 14, bottom = 22, left = 4, right = 4;
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const x = (i) => left + (i / (values.length - 1)) * (W - left - right);
  const y = (v) => top + (1 - (v - min) / span) * (H - top - bottom);
  const line = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const up = values[values.length - 1] >= values[0];
  const color = `var(--${up ? "up" : "down"})`;
  const grid = [0, 0.5, 1].map((t) => `<line x1="${left}" x2="${W - right}" y1="${top + t * (H - top - bottom)}" y2="${top + t * (H - top - bottom)}" stroke="var(--line)" stroke-width="1"/>`).join("");
  const iMax = values.indexOf(max), iMin = values.indexOf(min);
  const anchor = (i) => (i < values.length * 0.15 ? "start" : i > values.length * 0.85 ? "end" : "middle");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Kursverlauf">
    ${grid}
    <polygon points="${left},${H - bottom} ${line} ${W - right},${H - bottom}" fill="${color}" opacity="0.10"/>
    <polyline points="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>
    <circle cx="${x(values.length - 1)}" cy="${y(values[values.length - 1])}" r="3.5" fill="${color}"/>
    <text x="${x(iMax)}" y="${top - 3}" text-anchor="${anchor(iMax)}">${esc(fmt(max))}</text>
    <text x="${x(iMin)}" y="${H - 6}" text-anchor="${anchor(iMin)}">${esc(fmt(min))}</text>
  </svg>`;
}

// ------------------------------------------------------------ Bausteine

function tag(note) {
  if (/überkauft|Death Cross|unter die|Tief/.test(note)) return ["neg", "Achtung"];
  if (/überverkauft|Golden Cross|über die|Hoch/.test(note)) return ["pos", "Chance"];
  if (/Tagesbewegung von \+/.test(note)) return ["pos", "Bewegung"];
  if (/Tagesbewegung von -/.test(note)) return ["neg", "Bewegung"];
  return ["neu", "Hinweis"];
}
function sourcesBlock(rep) {
  const items = (rep.sources || []).map((s) => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a> – ${esc(s.what)}</li>`).join("");
  return `<section class="sources"><h2>Quellen</h2><ul>${items}</ul>
    <p>Kurse von Aktien und ETFs stammen von der Börse IEX und können leicht von anderen Börsenplätzen abweichen. Makrodaten erscheinen mit Verzögerung.
    Automatisch erzeugte Auswertung, keine Anlageberatung.</p></section>`;
}
function findItem(rep, key) {
  for (const rows of Object.values(rep.groups)) {
    const r = rows.find((x) => x.sym === key);
    if (r) return { kind: "kurs", item: r };
  }
  const m = rep.makro.find((x) => x.id === key);
  if (m) return { kind: "makro", item: m };
  const d = rep.devisen.find((x) => x.pair === key);
  if (d) return { kind: "devise", item: d };
  return null;
}

// ------------------------------------------------------------ Ansichten

function viewHeute(rep) {
  const tiles = rep.groups.maerkte.map((r) => `
    <button class="tile" type="button" data-open="${esc(r.sym)}">
      <span class="t-name">${esc(r.name)}</span>
      <span class="t-val num">${num(r.last, r.digits)}</span>
      <span class="t-chg num ${cls(r.d1)}">${signed(r.d1)}</span>
      ${spark(slice(r.hist, 63), (slice(r.hist, 63).at(-1) ?? 0) >= (slice(r.hist, 63)[0] ?? 0))}
    </button>`).join("");
  const paras = rep.paras.map((p) => `<p>${esc(p).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</p>`).join("");
  const notes = rep.notes.length
    ? rep.notes.map((n) => { const [c, l] = tag(n); return `<li class="signal"><span class="tag ${c}">${l}</span><span>${esc(n)}</span></li>`; }).join("")
    : '<li class="signal"><span class="tag neu">Ruhig</span><span>Keine auffälligen Signale.</span></li>';
  const sectors = rep.groups.sektoren;
  const maxAbs = Math.max(1, ...sectors.map((r) => Math.abs(r.d1)));
  const bars = sectors.slice().sort((a, b) => b.d1 - a.d1).map((r) => {
    const w = (Math.abs(r.d1) / maxAbs) * 50;
    const pos = r.d1 >= 0 ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
    return `<div class="barrow"><span class="lbl">${esc(r.name)}</span>
      <span class="track"><span class="fill ${r.d1 >= 0 ? "up" : "down"}" style="${pos}"></span></span>
      <span class="num ${cls(r.d1)}">${signed(r.d1)}</span></div>`;
  }).join("");
  return `
    <section class="hero">
      <span class="muted small">${rep.live ? "Stimmung jetzt" : "Stimmung zum Handelsschluss"}</span>
      <span class="mood ${esc(rep.mood)}">${esc(rep.mood[0].toUpperCase() + rep.mood.slice(1))}</span>
      <span class="muted small">Bericht vom ${esc(stampLabel(rep.time))}</span>
    </section>
    <section><h2>Märkte (Index-ETFs)</h2><div class="tiles">${tiles}</div></section>
    <section><h2>Auswertung</h2><div class="card pad prose">${paras}</div></section>
    <section><h2>Auffällige Signale</h2><ul class="card list">${notes}</ul></section>
    <section><h2>US-Sektoren</h2><div class="card bars">${bars}</div></section>
    ${sourcesBlock(rep)}`;
}

function kursRow(r, withStar, favs) {
  const s = slice(r.hist, 63);
  const star = withStar
    ? `<span class="star ${favs.has(r.sym) ? "on" : ""}" role="button" tabindex="0" data-star="${esc(r.sym)}" aria-label="${favs.has(r.sym) ? "Aus Favoriten entfernen" : "Zu Favoriten"}">★</span>`
    : "";
  return `<li><div class="row ${withStar ? "" : "nostar"}" role="button" tabindex="0" data-open="${esc(r.sym)}">
      ${star}
      <span><span class="r-name">${esc(r.name)}</span><br><span class="r-sub">${esc(r.sym)}${r.live ? " · läuft" : ""}</span></span>
      ${s.length ? spark(s, s.at(-1) >= s[0]) : "<span></span>"}
      <span class="r-right"><span class="num">${num(r.last, r.digits)}</span><span class="pill num ${cls(r.d1)}">${signed(r.d1)}</span></span>
    </div></li>`;
}

function viewWatchlist(rep) {
  const favs = favorites();
  const rows = rep.groups.watchlist.slice().sort((a, b) => (favs.has(b.sym) - favs.has(a.sym)));
  return `
    <section><ul class="card list">${rows.map((r) => kursRow(r, true, favs)).join("")}</ul>
      <p class="muted small">Tippe auf ★, um Werte als Favoriten nach oben zu holen. Neue Aktien kommen über finanz_app.json dazu.</p></section>
    ${sourcesBlock(rep)}`;
}

function viewMaerkte(rep) {
  const rohstoffe = rep.groups.rohstoffe.map((r) => kursRow(r, false)).join("");
  const devisen = rep.devisen.map((d) => {
    const s = slice(d.hist, 63);
    return `<li><div class="row nostar" role="button" tabindex="0" data-open="${esc(d.pair)}">
      <span><span class="r-name">${esc(d.pair)}</span><br><span class="r-sub">${esc(d.source)} · ${esc(dayLabel(d.date))}</span></span>
      ${s.length ? spark(s, s.at(-1) >= s[0]) : "<span></span>"}
      <span class="r-right"><span class="num">${num(d.value, fxDigits(d.value))}</span><span class="pill num ${cls(d.d1)}">${signed(d.d1)}</span></span></div></li>`;
  }).join("");
  const makro = rep.makro.map((m) => {
    const s = slice(m.hist, 63);
    const diff = m.unit.startsWith("%") ? signed((m.value - m.prev) * 100, 0, " Bp.") : signed((m.value / m.prev - 1) * 100);
    const diffCls = cls(m.value - m.prev);
    return `<li><div class="row nostar" role="button" tabindex="0" data-open="${esc(m.id)}">
      <span><span class="r-name">${esc(m.name)}</span><br><span class="r-sub">FRED · Stand ${esc(dayLabel(m.date))}</span></span>
      ${s.length > 1 ? spark(s, s.at(-1) >= s[0]) : "<span></span>"}
      <span class="r-right"><span class="num">${num(m.value)}${m.unit.startsWith("%") ? " %" : ""}</span><span class="small num ${diffCls}">${diff}</span></span></div></li>`;
  }).join("");
  return `
    <section><h2>Rohstoffe &amp; Krypto</h2><ul class="card list">${rohstoffe}</ul></section>
    <section><h2>Devisen (Euro-Referenzkurse)</h2><ul class="card list">${devisen || '<li class="signal muted">Keine Daten.</li>'}</ul></section>
    <section><h2>Konjunktur &amp; Zinsen (USA)</h2><ul class="card list">${makro}</ul></section>
    ${sourcesBlock(rep)}`;
}

function viewNews(rep) {
  const list = state.newsTab === "markt" ? rep.news : rep.notenbank;
  const items = (list || []).map((n) => `<li><a href="${esc(n.url)}" target="_blank" rel="noopener">
      <span class="n-title">${esc(n.title)}</span>
      <span class="n-meta">${esc(n.source)}${n.time ? " · " + esc(ago(n.time)) : ""}${n.symbols && n.symbols.length ? " · " + esc(n.symbols.join(", ")) : ""}</span></a></li>`).join("");
  return `
    <div class="seg" role="group" aria-label="Nachrichtenart">
      <button type="button" data-news="markt" aria-pressed="${state.newsTab === "markt"}">Unternehmen</button>
      <button type="button" data-news="notenbank" aria-pressed="${state.newsTab === "notenbank"}">Notenbanken</button>
    </div>
    <section><ul class="card list news">${items || '<li class="signal muted">Keine Nachrichten.</li>'}</ul>
      <p class="muted small">${state.newsTab === "markt" ? "Unternehmensnachrichten über Alpaca, mit Angabe des jeweiligen Verlags. Überschriften auf Englisch." : "Original-Pressemitteilungen der US-Notenbank und der Europäischen Zentralbank."}</p></section>
    ${sourcesBlock(rep)}`;
}

function viewArchiv() {
  if (!state.archive) return '<div class="loading"><span></span><span></span><span></span></div>';
  if (!state.archive.length) return '<p class="muted">Noch keine Berichte im Archiv.</p>';
  const items = state.archive.map((a) => `<li><button class="arch" type="button" data-report="${esc(a.file)}">
      <span><span class="dot ${esc(a.mood)}"></span>${esc(stampLabel(a.time))}</span>
      <span class="muted small">${esc(a.mood)}${a.live ? " · Handel lief" : ""}</span></button></li>`).join("");
  return `<section><h2>${state.archive.length} Berichte</h2><ul class="card list">${items}</ul>
    <p class="muted small">Archivierte Berichte enthalten keine Kursverläufe.</p></section>`;
}

// ------------------------------------------------------------ Detail

function openDetail(key) {
  const rep = state.report;
  const found = findItem(rep, key);
  if (!found) return;
  const { kind, item } = found;
  let title, head, stats, fmt, source;
  if (kind === "kurs") {
    title = `${item.name} (${item.sym})`;
    fmt = (v) => num(v, item.digits);
    head = `<span class="big num">${num(item.last, item.digits)}</span> <span class="num ${cls(item.d1)}">${signed(item.d1)}</span>`;
    stats = [["5 Tage", signed(item.d5)], ["1 Monat", signed(item.m1)], ["RSI 14", item.rsi === null ? "–" : num(item.rsi, 0)],
      ["200-Tage-Linie", item.sma200 ? num(item.sma200, item.digits) : "–"], ["52-W-Hoch", num(item.hi52, item.digits)],
      ["52-W-Tief", num(item.lo52, item.digits)], ["Abstand Hoch", signed(item.fromHigh, 1)], ["Stand", dayLabel(item.date) + (item.live ? " (läuft)" : "")]];
    source = item.source;
  } else if (kind === "makro") {
    title = item.name;
    const pctUnit = item.unit.startsWith("%");
    fmt = (v) => num(v) + (pctUnit ? " %" : "");
    head = `<span class="big num">${fmt(item.value)}</span> <span class="muted small">${esc(item.unit)}</span>`;
    stats = [["Vorwert", fmt(item.prev)], ["Stand", dayLabel(item.date)]];
    source = `FRED – <a href="${esc(item.url)}" target="_blank" rel="noopener">Originalreihe ${esc(item.id)}</a>`;
  } else {
    title = item.pair;
    fmt = (v) => num(v, fxDigits(item.value));
    head = `<span class="big num">${fmt(item.value)}</span> <span class="num ${cls(item.d1)}">${signed(item.d1)}</span>`;
    stats = [["Stand", dayLabel(item.date)], ["Quelle", item.source]];
    source = item.source === "EZB" ? "Europäische Zentralbank, Referenzkurs 14:15 Uhr" : item.source;
  }
  const hasHist = item.hist && item.hist.c && item.hist.c.length > 1;
  const seg = hasHist && kind !== "makro"
    ? `<div class="seg" role="group" aria-label="Zeitraum">${RANGES.map(([l, n], i) => `<button type="button" data-range="${n}" aria-pressed="${i === 3}">${l}</button>`).join("")}</div>` : "";
  $("sheet-title").textContent = title;
  $("sheet-body").innerHTML = `
    <div>${head}</div>
    ${seg}
    <div class="card pad chart" id="chart">${hasHist ? bigChart(item.hist.c, fmt) : '<p class="muted small">Im Archiv sind keine Kursverläufe gespeichert.</p>'}</div>
    <div class="card stats">${stats.map(([k, v]) => `<div><span class="muted">${esc(k)}</span><span class="num">${esc(v)}</span></div>`).join("")}</div>
    <p class="muted small">Quelle: ${source.includes("<a") ? source : esc(source)}</p>`;
  $("sheet-body").querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => {
    $("sheet-body").querySelectorAll("[data-range]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    $("chart").innerHTML = bigChart(slice(item.hist, Number(b.dataset.range)), fmt);
  }));
  $("sheet").hidden = false;
  $("sheet-close").focus();
}
function closeDetail() { $("sheet").hidden = true; }

// ------------------------------------------------------------ Ablauf

function currentTab() {
  const t = location.hash.slice(1);
  return TABS[t] ? t : "heute";
}

function render() {
  const tab = currentTab();
  $("title").textContent = TABS[tab];
  document.querySelectorAll(".tabs a").forEach((a) => { if (a.dataset.tab === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });

  const rep = state.report;
  const status = $("status");
  if (rep) {
    status.textContent = rep.live ? "Börse offen" : "Börse geschlossen";
    status.className = "status" + (rep.live ? " open" : "");
  }
  $("archive-banner").hidden = !state.archived;
  if (state.archived) $("archive-label").textContent = `Archiv: ${stampLabel(state.archived.time)}`;

  if (tab === "archiv") {
    $("view").innerHTML = viewArchiv();
    if (!state.archive) loadArchive();
  } else if (!rep) {
    return;
  } else {
    $("view").innerHTML = { heute: viewHeute, watchlist: viewWatchlist, maerkte: viewMaerkte, news: viewNews }[tab](rep);
  }
  window.scrollTo(0, 0);
}

async function getJSON(url) {
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function loadLatest() {
  try {
    state.latest = await getJSON("data/latest.json");
    if (!state.archived) state.report = state.latest;
    render();
  } catch (e) {
    $("view").innerHTML = `<div class="card pad error"><p>Die Daten konnten nicht geladen werden. Prüfe deine Internetverbindung und lade die App neu.</p></div>`;
  }
}

async function loadArchive() {
  try {
    state.archive = await getJSON("data/archiv.json");
  } catch (e) {
    state.archive = [];
  }
  if (currentTab() === "archiv") render();
}

async function openReport(file) {
  try {
    const rep = await getJSON(`data/reports/${encodeURIComponent(file)}`);
    state.archived = rep;
    state.report = rep;
    location.hash = "#heute";
    render();
  } catch (e) {
    alert("Dieser Bericht konnte nicht geladen werden.");
  }
}

document.addEventListener("click", (ev) => {
  const star = ev.target.closest("[data-star]");
  if (star) { ev.stopPropagation(); toggleFavorite(star.dataset.star); render(); return; }
  const open = ev.target.closest("[data-open]");
  if (open) { openDetail(open.dataset.open); return; }
  const news = ev.target.closest("[data-news]");
  if (news) { state.newsTab = news.dataset.news; render(); return; }
  const report = ev.target.closest("[data-report]");
  if (report) { openReport(report.dataset.report); return; }
  if (ev.target === $("sheet")) closeDetail();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeDetail();
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches("[role=button]")) { ev.preventDefault(); ev.target.click(); }
});
$("sheet-close").addEventListener("click", closeDetail);
$("archive-close").addEventListener("click", () => { state.archived = null; state.report = state.latest; render(); });
window.addEventListener("hashchange", () => { closeDetail(); render(); });
// Beim Zurueckkehren in die App neue Daten holen
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadLatest(); });

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
loadLatest();
