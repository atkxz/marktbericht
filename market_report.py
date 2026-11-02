"""Taeglicher Marktbericht: Indizes, Sektoren, Makro, Watchlist und Schlagzeilen.

Kommt wie bot.py ohne Zusatzpakete aus. Daten kommen von Yahoo Finance, die
Beschreibung wird regelbasiert erzeugt.

Aufruf:
    python market_report.py               # Bericht nach reports/JJJJ-MM-TT_HHMM.html schreiben
    python market_report.py --open        # ... und im Browser oeffnen (fuer die geplante Aufgabe)
    python market_report.py --site docs   # Webseite mit Archiv in docs/ aktualisieren (GitHub Pages)

Auswahl der Werte in market_report.json, Protokoll in logs/JJJJ-MM-TT.log.
Bewusst eigenstaendig (ohne bot.py), damit nur diese Datei auf GitHub laufen muss.
"""
import argparse
import html
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
REPORT_DIR = BASE_DIR / "reports"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
UA = {"User-Agent": "Mozilla/5.0"}

log = logging.getLogger("report")


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  [report] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(LOG_DIR / f"{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        log.addHandler(console)
    log.setLevel(logging.INFO)


# ---------------------------------------------------------------- Daten

def http_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} bei {url}") from None


def fetch_series(symbol):
    """Ein Jahr Tageskurse plus Kennzahlen. None, wenn Yahoo nichts liefert."""
    url = f"{YAHOO_CHART_URL}{urllib.parse.quote(symbol)}?range=1y&interval=1d"
    data = http_json(url, headers=UA)
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result or not result.get("timestamp"):
        return None
    meta = result["meta"]
    offset = timedelta(seconds=meta["gmtoffset"])
    rows = [
        ((datetime.fromtimestamp(ts, timezone.utc) + offset).date(), c)
        for ts, c in zip(result["timestamp"], result["indicators"]["quote"][0]["close"])
        if c is not None
    ]
    if len(rows) < 2:
        return None

    # Laeuft der Handel noch, ist der letzte Kurs nur ein Zwischenstand
    regular = meta.get("currentTradingPeriod", {}).get("regular", {})
    now = datetime.now(timezone.utc)
    live = rows[-1][0] == (now + offset).date() and now.timestamp() < regular.get("end", 0)
    m = metrics([c for _, c in rows], live, meta.get("currency", ""))
    m["date"] = rows[-1][0]
    return m


def pct(a, b):
    return (a / b - 1) * 100


def sma(values, window, end=None):
    end = len(values) if end is None else end
    if end < window:
        return None
    return sum(values[end - window:end]) / window


def rsi(values, period=14):
    if len(values) <= period:
        return None
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):  # Wilder-Glaettung
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)


def metrics(closes, live, currency):
    last = closes[-1]
    m = {
        "last": last,
        "prev": closes[-2],
        "d1": pct(last, closes[-2]),
        "d5": pct(last, closes[-6]) if len(closes) > 5 else None,
        "m1": pct(last, closes[-22]) if len(closes) > 21 else None,
        "hi52": max(closes),
        "lo52": min(closes),
        "sma50": sma(closes, 50),
        "sma200": sma(closes, 200),
        "rsi": rsi(closes),
        "live": live,
        "currency": currency,
        "cross": None,
    }
    # Golden/Death Cross von SMA50 und SMA200 in den letzten 5 Handelstagen
    if len(closes) >= 206:
        diffs = [sma(closes, 50, e) - sma(closes, 200, e) for e in range(len(closes) - 5, len(closes) + 1)]
        if diffs[0] <= 0 < diffs[-1]:
            m["cross"] = "golden"
        elif diffs[0] >= 0 > diffs[-1]:
            m["cross"] = "death"
    return m


def fetch_all(symbols):
    def one(sym):
        try:
            return sym, fetch_series(sym)
        except Exception as e:
            log.warning(f"{sym}: {e}")
            return sym, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        data = dict(pool.map(one, symbols))
    missing = [s for s, v in data.items() if v is None]
    if missing:
        log.warning(f"Keine Daten fuer: {', '.join(missing)}")
    return {s: v for s, v in data.items() if v is not None}


def fetch_news(symbols, count):
    url = f"{YAHOO_RSS_URL}?{urllib.parse.urlencode({'s': ','.join(symbols), 'region': 'US', 'lang': 'en-US'})}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        log.warning(f"Schlagzeilen nicht abrufbar: {e}")
        return []
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if title and title not in (t for t, _ in items):
            items.append((title, (item.findtext("link") or "").strip()))
    return items[:count]


# ---------------------------------------------------------------- Text

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def price_digits(x):
    return 4 if x < 2 else 2  # Devisenkurse wie EUR/USD brauchen mehr Stellen


def num(x, digits=2):
    return f"{x:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def signed(x, digits=2, unit=" %"):
    return "–" if x is None else ("+" if x > 0 else "") + num(x, digits) + unit


def move_phrase(p):
    if p >= 1.5:
        return "kräftig zugelegt"
    if p >= 0.5:
        return "zugelegt"
    if p > 0.1:
        return "leicht zugelegt"
    if p > -0.1:
        return "kaum verändert geschlossen"
    if p > -0.5:
        return "leicht nachgegeben"
    if p > -1.5:
        return "nachgegeben"
    return "deutlich verloren"


def live_phrase(p):
    if p >= 1.5:
        return "kräftig im Plus"
    if p >= 0.5:
        return "im Plus"
    if p > 0.1:
        return "leicht im Plus"
    if p > -0.1:
        return "nahezu unverändert"
    if p > -0.5:
        return "leicht im Minus"
    if p > -1.5:
        return "im Minus"
    return "deutlich im Minus"


def describe_index(name, m, day):
    if m["live"]:
        return f"Der {name} liegt aktuell {live_phrase(m['d1'])} ({signed(m['d1'])})."
    # Liefert Yahoo fuer einen Index noch keinen neueren Kurs, das Datum nennen
    when = "" if m["date"] >= day else f" am {WEEKDAYS[m['date'].weekday()]}"
    return f"Der {name} hat{when} {move_phrase(m['d1'])} ({signed(m['d1'])})."


def describe_trend(indices):
    above = [n for n, m in indices.items() if m["sma200"] and m["last"] > m["sma200"]]
    below = [n for n, m in indices.items() if m["sma200"] and m["last"] <= m["sma200"]]
    if above and not below:
        return "Alle beobachteten Indizes notieren über ihrer 200-Tage-Linie – der langfristige Aufwärtstrend ist intakt."
    if below and not above:
        return "Alle beobachteten Indizes notieren unter ihrer 200-Tage-Linie – der langfristige Trend ist angeschlagen."
    if above:
        return f"Über der 200-Tage-Linie notieren {', '.join(above)}, darunter {', '.join(below)}."
    return ""


def market_mood(index_data, sector_data):
    changes = [m["d1"] for m in index_data.values()]
    avg = sum(changes) / len(changes) if changes else 0
    up = sum(1 for m in sector_data.values() if m["d1"] > 0)
    total = len(sector_data)
    if avg > 0.4 and (not total or up / total >= 0.6):
        mood = "freundlich"
    elif avg < -0.4 and (not total or up / total <= 0.4):
        mood = "schwach"
    else:
        mood = "gemischt"
    return mood, up, total


def vix_phrase(m):
    v = m["last"]
    if v < 15:
        level = "sehr ruhig"
    elif v < 20:
        level = "normal"
    elif v < 30:
        level = "erhöht – die Nervosität nimmt zu"
    else:
        level = "hoch – der Markt ist im Stressmodus"
    return f"Der VIX steht bei {num(v)} ({signed(m['d1'])}), die erwartete Schwankung ist damit {level}."


def signals(name, m):
    out = []
    if abs(m["d1"]) >= 3:
        out.append(f"{name}: starke Tagesbewegung von {signed(m['d1'])}")
    if m["last"] >= m["hi52"] * 0.995:
        out.append(f"{name}: auf bzw. nahe dem 52-Wochen-Hoch")
    elif m["last"] <= m["lo52"] * 1.005:
        out.append(f"{name}: auf bzw. nahe dem 52-Wochen-Tief")
    if m["rsi"] is not None and m["rsi"] >= 70:
        out.append(f"{name}: RSI {num(m['rsi'], 0)} – überkauft")
    elif m["rsi"] is not None and m["rsi"] <= 30:
        out.append(f"{name}: RSI {num(m['rsi'], 0)} – überverkauft")
    if m["cross"] == "golden":
        out.append(f"{name}: Golden Cross (SMA50 über SMA200) in den letzten Tagen")
    elif m["cross"] == "death":
        out.append(f"{name}: Death Cross (SMA50 unter SMA200) in den letzten Tagen")
    if m["sma200"] and (m["prev"] - m["sma200"]) * (m["last"] - m["sma200"]) < 0:
        side = "über" if m["last"] > m["sma200"] else "unter"
        out.append(f"{name}: am letzten Handelstag {side} die 200-Tage-Linie gekreuzt")
    return out


def build_summary(cfg, data):
    def pick(group):
        return {cfg[group][s]: data[s] for s in cfg[group] if s in data}

    indices, macro, sectors, watch = pick("indices"), pick("macro"), pick("sectors"), pick("watchlist")
    paras = []

    # Am Wochenende oder Feiertag beziehen sich die Zahlen auf den letzten Handelstag
    day = max((m["date"] for m in indices.values()), default=datetime.now().date())
    mood, up, total = market_mood({n: m for n, m in indices.items() if m["date"] >= day}, sectors)
    if any(m["live"] for m in indices.values()):
        first = f"Die Stimmung an den Aktienmärkten ist aktuell <b>{mood}</b>."
        breadth = "liegen"
    else:
        when = "heute" if day == datetime.now().date() else f"am {WEEKDAYS[day.weekday()]}, {day:%d.%m.},"
        first = f"Die Stimmung an den Aktienmärkten war {when} <b>{mood}</b>."
        breadth = "lagen"
    if total:
        first += f" {up} von {total} US-Sektoren {breadth} im Plus."
    paras.append(first)
    paras.append(" ".join([describe_index(n, m, day) for n, m in indices.items()] + [describe_trend(indices)]))

    if sectors:
        ranked = sorted(sectors.items(), key=lambda kv: kv[1]["d1"], reverse=True)
        best, worst = ranked[0], ranked[-1]
        paras.append(
            f"Stärkster Sektor ist {best[0]} ({signed(best[1]['d1'])}), am schwächsten zeigt sich "
            f"{worst[0]} ({signed(worst[1]['d1'])})."
        )

    macro_bits = []
    for sym, name in cfg["macro"].items():
        m = data.get(sym)
        if not m:
            continue
        if sym == "^VIX":
            macro_bits.insert(0, vix_phrase(m))
        elif sym == "^TNX":
            bp = (m["last"] - m["prev"]) * 100
            macro_bits.append(f"Die {name} liegt bei {num(m['last'])} % ({signed(bp, 0, ' Bp.')}).")
        else:
            macro_bits.append(f"{name}: {num(m['last'], price_digits(m['last']))} ({signed(m['d1'])}).")
    if macro_bits:
        paras.append(" ".join(macro_bits))

    if watch:
        ranked = sorted(watch.items(), key=lambda kv: kv[1]["d1"], reverse=True)
        tops = [f"{n} ({signed(m['d1'])})" for n, m in ranked[:2] if m["d1"] > 0]
        flops = [f"{n} ({signed(m['d1'])})" for n, m in ranked[::-1][:2] if m["d1"] < 0]
        text = []
        if tops:
            text.append(f"Vorne in der Watchlist {'liegen' if len(tops) > 1 else 'liegt'} {' und '.join(tops)}")
        if flops:
            text.append(f"am Ende {'stehen' if len(flops) > 1 else 'steht'} {' und '.join(flops)}")
        if text:
            paras.append(", ".join(text) + ".")

    notes = []
    for name, m in {**indices, **watch}.items():
        notes += signals(name, m)
    return paras, notes


# ---------------------------------------------------------------- HTML

CSS = """
:root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1f; --muted:#6b6b70; --line:#e4e4e0; --up:#13804a; --down:#c62828; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141416; --card:#1e1e21; --text:#ececee; --muted:#9a9aa0; --line:#2e2e33; --up:#4cc38a; --down:#ff6b6b; }
}
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.55 system-ui, "Segoe UI", sans-serif; }
main { max-width:960px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:26px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 10px; }
.sub { color:var(--muted); margin:0 0 20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 20px; }
.card p { margin:0 0 10px; } .card p:last-child { margin:0; }
ul { margin:0; padding-left:20px; }
.tbl { overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { padding:8px 12px; text-align:right; white-space:nowrap; border-bottom:1px solid var(--line); }
th:first-child, td:first-child { text-align:left; } tr:last-child td { border-bottom:0; }
th { color:var(--muted); font-weight:600; font-size:13px; }
.up { color:var(--up); } .down { color:var(--down); } .live { color:var(--muted); font-size:12px; }
a { color:inherit; } .foot { color:var(--muted); font-size:13px; margin-top:28px; }
.nav { display:flex; gap:8px; margin:0 0 18px; }
.nav a { text-decoration:none; font-size:14px; font-weight:600; padding:6px 12px; border:1px solid var(--line); border-radius:999px; background:var(--card); }
.days { list-style:none; padding:0; display:grid; gap:8px; }
.days li { display:flex; flex-wrap:wrap; align-items:center; gap:8px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; }
.days b { min-width:9em; } .days a { font-variant-numeric:tabular-nums; }
"""

# Fuer "Zum Startbildschirm hinzufuegen" auf dem Handy
HEAD_EXTRA = (
    '<meta name="theme-color" content="#f7f7f5"><meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-title" content="Marktbericht">'
    '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22%3E'
    '%3Crect width=%2232%22 height=%2232%22 rx=%227%22 fill=%22%2313804a%22/%3E%3Cpath d=%22M6 22l7-7 5 4 8-10%22 '
    'stroke=%22white%22 stroke-width=%223%22 fill=%22none%22 stroke-linecap=%22round%22/%3E%3C/svg%3E">'
)
WEEKDAYS_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def cell(x, digits=2, unit=" %"):
    if x is None:
        return "<td>–</td>"
    cls = "up" if x > 0 else "down" if x < 0 else ""
    return f'<td class="{cls}">{signed(x, digits, unit)}</td>'


def table(cfg_group, data):
    rows = []
    for sym, name in cfg_group.items():
        m = data.get(sym)
        if not m:
            continue
        live = ' <span class="live">läuft</span>' if m["live"] else ""
        sma200 = "–" if not m["sma200"] else ("darüber" if m["last"] > m["sma200"] else "darunter")
        rows.append(
            f"<tr><td>{html.escape(name)}{live}</td><td>{num(m['last'], price_digits(m['last']))}</td>"
            f"{cell(m['d1'])}{cell(m['d5'])}{cell(m['m1'])}"
            f"<td>{'–' if m['rsi'] is None else num(m['rsi'], 0)}</td><td>{sma200}</td>"
            f"{cell(pct(m['last'], m['hi52']), 1)}</tr>"
        )
    head = ("<tr><th>Wert</th><th>Kurs</th><th>Tag</th><th>5 Tage</th><th>1 Monat</th>"
            "<th>RSI 14</th><th>SMA 200</th><th>vom 52W-Hoch</th></tr>")
    return f'<div class="tbl"><table>{head}{"".join(rows)}</table></div>'


def nav(prefix):
    return (f'<nav class="nav"><a href="{prefix}index.html">Neuester Bericht</a>'
            f'<a href="{prefix}archiv.html">Archiv</a></nav>')


def render(cfg, data, paras, notes, news, now, nav_html=""):
    news_html = "".join(
        f'<li><a href="{html.escape(link)}">{html.escape(title)}</a></li>' if link else f"<li>{html.escape(title)}</li>"
        for title, link in news
    ) or "<li>Keine Schlagzeilen verfügbar.</li>"
    notes_html = "".join(f"<li>{html.escape(n)}</li>" for n in notes) or "<li>Keine auffälligen Signale.</li>"
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Marktbericht {now:%d.%m.%Y}</title>{HEAD_EXTRA}<style>{CSS}</style></head>
<body><main>
{nav_html}
<h1>Marktbericht {now:%d.%m.%Y}</h1>
<p class="sub">Stand {now:%H:%M} Uhr · Daten: Yahoo Finance · Werte mit „läuft“ sind Zwischenstände, die Börse ist noch offen.</p>
<div class="card">{"".join(f"<p>{p}</p>" for p in paras)}</div>
<h2>Auffällige Signale</h2><div class="card"><ul>{notes_html}</ul></div>
<h2>Indizes</h2>{table(cfg["indices"], data)}
<h2>US-Sektoren</h2>{table(cfg["sectors"], data)}
<h2>Watchlist</h2>{table(cfg["watchlist"], data)}
<h2>Makro &amp; Rohstoffe</h2>{table(cfg["macro"], data)}
<h2>Schlagzeilen</h2><div class="card"><ul>{news_html}</ul></div>
<p class="foot">Automatisch erzeugte Auswertung, keine Anlageberatung.</p>
</main></body></html>"""


def render_archive(names):
    """names: Dateinamen JJJJ-MM-TT_HHMM.html, neueste zuerst - eine Zeile pro Tag."""
    days = {}
    for name in names:
        days.setdefault(name[:10], []).append(name)
    rows = []
    for day, files in days.items():
        d = datetime.strptime(day, "%Y-%m-%d")
        links = " · ".join(f'<a href="reports/{f}">{f[11:13]}:{f[13:15]}</a>' for f in sorted(files))
        rows.append(f"<li><b>{WEEKDAYS_SHORT[d.weekday()]} {d:%d.%m.%Y}</b>{links}</li>")
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Archiv – Marktbericht</title>{HEAD_EXTRA}<style>{CSS}</style></head>
<body><main>{nav("")}<h1>Archiv</h1>
<p class="sub">{len(names)} Berichte</p><ul class="days">{"".join(rows)}</ul>
<p class="foot">Automatisch erzeugte Auswertung, keine Anlageberatung.</p></main></body></html>"""


def write_site(site, page, now):
    """Bericht ins Archiv der Webseite legen, Startseite und Archivliste neu schreiben."""
    reports = Path(site) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{now:%Y-%m-%d_%H%M}.html").write_text(page(nav("../")), encoding="utf-8")
    (Path(site) / "index.html").write_text(page(nav("")), encoding="utf-8")
    names = sorted((p.name for p in reports.glob("*.html")), reverse=True)
    (Path(site) / "archiv.html").write_text(render_archive(names), encoding="utf-8")
    (Path(site) / ".nojekyll").touch()  # GitHub Pages soll die Dateien unveraendert ausliefern
    log.info(f"Webseite aktualisiert: {site} ({len(names)} Berichte im Archiv)")


def in_time_window(only_at, now):
    """True, wenn now hoechstens 59 Min. nach einer der Zeiten "HH:MM,HH:MM" liegt.

    GitHub startet geplante Laeufe in UTC und oft verspaetet; die Laeufe sind deshalb fuer
    Sommer- und Winterzeit doppelt angelegt, und hier wird der passende ausgewaehlt.
    """
    for t in only_at.split(","):
        h, m = map(int, t.split(":"))
        delta = now - now.replace(hour=h, minute=m, second=0, microsecond=0)
        if timedelta(0) <= delta < timedelta(hours=1):
            return True
    return False


# ---------------------------------------------------------------- Ablauf

def run(args):
    now = datetime.now()
    if args.only_at and not in_time_window(args.only_at, now):
        log.info(f"{now:%H:%M} liegt nicht im Zeitfenster {args.only_at} - kein Bericht.")
        return

    with open(BASE_DIR / "market_report.json", encoding="utf-8") as f:
        cfg = json.load(f)
    symbols = [s for g in ("indices", "macro", "sectors", "watchlist") for s in cfg[g]]
    log.info(f"Start: {len(symbols)} Werte")

    data = fetch_all(symbols)
    if not data:
        raise RuntimeError("Keine Kursdaten erhalten - Internetverbindung pruefen.")
    news = fetch_news(cfg["news_symbols"], cfg["news_count"])
    paras, notes = build_summary(cfg, data)

    log.info(f"{len(data)} Werte, {len(notes)} Signale, {len(news)} Schlagzeilen")

    def page(nav_html=""):
        return render(cfg, data, paras, notes, news, now, nav_html)

    if args.site:
        write_site(args.site, page, now)
        return
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"{now:%Y-%m-%d_%H%M}.html"  # mehrere Berichte pro Tag
    path.write_text(page(), encoding="utf-8")
    log.info(f"Bericht geschrieben: {path}")
    if args.open:
        os.startfile(path)


def main():
    parser = argparse.ArgumentParser(description="Taeglicher Marktbericht")
    parser.add_argument("--open", action="store_true", help="Bericht danach im Browser oeffnen")
    parser.add_argument("--site", metavar="ORDNER", help="Webseite mit Archiv in diesem Ordner aktualisieren")
    parser.add_argument("--only-at", metavar="HH:MM,...", help="nur laufen, wenn es bis zu 59 Min. nach einer dieser Zeiten ist")
    args = parser.parse_args()

    setup_logging()
    try:
        run(args)
    except Exception:
        log.exception("Fehler beim Bericht:")
        sys.exit(1)


if __name__ == "__main__":
    main()
