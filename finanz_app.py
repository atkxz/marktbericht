"""Daten fuer die Finanz-Web-App (docs/): Kurse, Makro, Devisen, Nachrichten und Tagesbericht.

Quellen (alle offiziell bzw. mit Quellenangabe):
    Alpaca Markets  - Kurse von US-Aktien, ETFs und Bitcoin, Boersenuhr, Nachrichten
    FRED            - Makrodaten der Federal Reserve Bank of St. Louis
    EZB             - Euro-Referenzkurse (Ersatz bei Ausfall: FRED)
    Fed, EZB        - Pressemitteilungen der Notenbanken

Aufruf:
    python finanz_app.py                          # docs/data/ aktualisieren
    python finanz_app.py --only-at 15:45,20:00    # nur im Zeitfenster (fuer GitHub Actions)

Alpaca-Schluessel kommen aus den Umgebungsvariablen APCA_API_KEY_ID / APCA_API_SECRET_KEY
(GitHub: Repository-Secrets) oder, lokal, aus .env. Auswahl der Werte in finanz_app.json.
"""
import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from market_report import (WEEKDAYS, in_time_window, live_phrase, metrics, num, pct, price_digits,
                           signals, signed)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "docs" / "data"
ALPACA_DATA = "https://data.alpaca.markets"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
ECB_EXR = "https://data-api.ecb.europa.eu/service/data/EXR/D.{}.EUR.SP00.A"
NOTENBANK_FEEDS = {
    "Fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "EZB": "https://www.ecb.europa.eu/rss/press.html",
}
UA = {"User-Agent": "Mozilla/5.0 (Marktbericht-App)"}
SOURCES = [
    {"name": "Alpaca Markets (IEX)", "what": "Kurse, Börsenuhr, Nachrichten", "url": "https://alpaca.markets"},
    {"name": "FRED, Federal Reserve Bank of St. Louis", "what": "Index, VIX, Zinsen, Inflation, Arbeitsmarkt", "url": "https://fred.stlouisfed.org"},
    {"name": "Europäische Zentralbank", "what": "Euro-Referenzkurse, Pressemitteilungen", "url": "https://www.ecb.europa.eu"},
    {"name": "Federal Reserve", "what": "Pressemitteilungen", "url": "https://www.federalreserve.gov"},
]

log = logging.getLogger("app")


def setup_logging():
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  [app] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)
    log.setLevel(logging.INFO)


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} bei {url.split('?')[0]}") from None


def alpaca_keys():
    key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    env_file = BASE_DIR / ".env"
    if (not key or not secret) and env_file.exists():
        env = dict(re.findall(r"^\s*([A-Z_]+)\s*=\s*\"?(.*?)\"?\s*$", env_file.read_text(encoding="utf-8"), re.M))
        key, secret = env.get("APCA_API_KEY_ID"), env.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Alpaca-Schluessel fehlen (APCA_API_KEY_ID / APCA_API_SECRET_KEY).")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, **UA}


# ---------------------------------------------------------------- Alpaca

def alpaca_bars(headers, symbols, start):
    """Tagesschlusskurse {symbol: [(datum, kurs), ...]} fuer Aktien/ETFs, ueber alle Seiten."""
    out, token = {}, None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "feed": "iex",
                  "adjustment": "all", "limit": 10000}
        if token:
            params["page_token"] = token
        d = json.loads(fetch(f"{ALPACA_DATA}/v2/stocks/bars?{urllib.parse.urlencode(params)}", headers))
        for sym, bars in (d.get("bars") or {}).items():
            out.setdefault(sym, []).extend((b["t"][:10], b["c"]) for b in bars)
        token = d.get("next_page_token")
        if not token:
            return out


def alpaca_latest(headers, symbols):
    """Letzter gehandelter Kurs je Symbol (genauer als der Tagesbalken waehrend des Handels)."""
    d = json.loads(fetch(f"{ALPACA_DATA}/v2/stocks/snapshots?symbols={','.join(symbols)}&feed=iex", headers))
    return {s: v["latestTrade"]["p"] for s, v in d.items() if v and v.get("latestTrade")}


def alpaca_crypto(symbols, start):
    q = urllib.parse.urlencode({"symbols": ",".join(symbols), "timeframe": "1Day", "start": start})
    d = json.loads(fetch(f"{ALPACA_DATA}/v1beta3/crypto/us/bars?{q}"))
    return {s: [(b["t"][:10], b["c"]) for b in bars] for s, bars in (d.get("bars") or {}).items()}


def alpaca_clock(headers):
    base = re.sub(r"/v2/?$", "", (os.environ.get("APCA_API_BASE_URL") or "https://paper-api.alpaca.markets").rstrip("/"))
    return json.loads(fetch(f"{base}/v2/clock", headers))


def alpaca_news(headers, symbols, count):
    q = urllib.parse.urlencode({"symbols": ",".join(symbols), "limit": count, "sort": "desc"})
    d = json.loads(fetch(f"{ALPACA_DATA}/v1beta1/news?{q}", headers))
    return [{"title": n["headline"], "url": n.get("url") or "", "source": n.get("source", "").capitalize(),
             "time": n["created_at"], "symbols": n.get("symbols", [])[:4]}
            for n in d.get("news", [])]


# ---------------------------------------------------------------- FRED, EZB, Notenbanken

def fred_series(sid, start):
    text = fetch(f"{FRED_CSV}?{urllib.parse.urlencode({'id': sid, 'cosd': start})}").decode()
    rows = []
    for r in list(csv.reader(io.StringIO(text)))[1:]:
        try:
            rows.append((r[0], float(r[1])))
        except (ValueError, IndexError):
            continue  # FRED markiert fehlende Tage mit "." oder leer
    return rows


def ecb_rates(currencies, start):
    """{"EUR/USD": [(datum, kurs), ...]} - Referenzkurse der EZB, Ersatz fuer EUR/USD: FRED."""
    try:
        url = ECB_EXR.format("+".join(currencies)) + "?" + urllib.parse.urlencode({"startPeriod": start, "format": "csvdata"})
        out = {}
        for r in csv.DictReader(io.StringIO(fetch(url).decode())):
            out.setdefault(f"EUR/{r['CURRENCY']}", []).append((r["TIME_PERIOD"], float(r["OBS_VALUE"])))
        return {k: sorted(v) for k, v in out.items()}, "EZB"
    except Exception as e:
        log.warning(f"EZB-Kurse nicht abrufbar ({e}) - nutze FRED fuer EUR/USD.")
        return {"EUR/USD": fred_series("DEXUSEU", start)}, "FRED"


def notenbank_news(count):
    items = []
    for bank, url in NOTENBANK_FEEDS.items():
        try:
            root = ET.fromstring(fetch(url))
        except Exception as e:
            log.warning(f"{bank}-Pressemitteilungen nicht abrufbar: {e}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            when = it.findtext("pubDate") or ""
            try:
                when = datetime.strptime(when.strip()[:25], "%a, %d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                when = ""
            if title:
                items.append({"title": title, "url": (it.findtext("link") or "").strip(), "source": bank, "time": when})
    return sorted(items, key=lambda i: i["time"], reverse=True)[:count]


# ---------------------------------------------------------------- Auswertung

def row(sym, name, closes, live, source, with_hist):
    m = metrics([c for _, c in closes], live, "USD")
    r = {
        "sym": sym, "name": name, "last": round(m["last"], 4), "digits": price_digits(m["last"]),
        "d1": m["d1"], "d5": m["d5"], "m1": m["m1"], "rsi": m["rsi"],
        "above200": None if not m["sma200"] else m["last"] > m["sma200"],
        "fromHigh": pct(m["last"], m["hi52"]), "live": live, "date": closes[-1][0], "source": source,
        "cross": m["cross"], "prev": m["prev"], "sma200": m["sma200"], "hi52": m["hi52"], "lo52": m["lo52"],
    }
    if with_hist:
        r["hist"] = {"from": closes[0][0], "to": closes[-1][0], "c": [round(c, 4) for _, c in closes]}
    return r


def macro_entry(sid, name, unit, rows):
    if sid == "CPIAUCSL":  # Index in Inflationsrate umrechnen (Vorjahresmonat)
        rows = [(d, pct(v, rows[i - 12][1])) for i, (d, v) in enumerate(rows) if i >= 12]
    if len(rows) < 2:
        return None
    (d_prev, v_prev), (d_last, v_last) = rows[-2], rows[-1]
    return {"id": sid, "name": name, "unit": unit, "value": v_last, "prev": v_prev, "date": d_last,
            "source": "FRED", "url": f"https://fred.stlouisfed.org/series/{sid}",
            "hist": {"from": rows[0][0], "to": d_last, "c": [round(v, 4) for _, v in rows[-260:]]}}


def mood_of(markets, sectors):
    avg = sum(r["d1"] for r in markets) / len(markets) if markets else 0
    up = sum(1 for r in sectors if r["d1"] > 0)
    share = up / len(sectors) if sectors else 0.5
    if avg > 0.4 and share >= 0.6:
        return "freundlich", up
    if avg < -0.4 and share <= 0.4:
        return "schwach", up
    return "gemischt", up


def day_label(iso):
    d = date.fromisoformat(iso)
    return f"{WEEKDAYS[d.weekday()]}, {d:%d.%m.}"


def summary(groups, makro, devisen, live, today):
    markets, sectors, watch = groups["maerkte"], groups["sektoren"], groups["watchlist"]
    mood, up = mood_of(markets, sectors)
    paras = []
    day = max((r["date"] for r in markets), default=today)
    if live:
        first = f"Die Stimmung an den US-Börsen ist aktuell **{mood}**. {up} von {len(sectors)} Sektoren liegen im Plus."
    else:
        when = "heute" if day == today else f"am {day_label(day)}"
        first = f"Die Stimmung an den US-Börsen war {when} **{mood}**. {up} von {len(sectors)} Sektoren lagen im Plus."
    paras.append(first)

    bits = [f"{r['name']} {live_phrase(r['d1'])} ({signed(r['d1'])})" for r in markets]
    if bits:
        lead = "Die großen Index-ETFs im Überblick: " if live else "Die großen Index-ETFs zum Handelsschluss: "
        paras.append(lead + ", ".join(bits[:-1]) + (" und " if len(bits) > 1 else "") + bits[-1] + ".")

    if sectors:
        s = sorted(sectors, key=lambda r: r["d1"], reverse=True)
        paras.append(f"Stärkster Sektor ist {s[0]['name']} ({signed(s[0]['d1'])}), "
                     f"am schwächsten zeigt sich {s[-1]['name']} ({signed(s[-1]['d1'])}).")

    mk = {m["id"]: m for m in makro}
    macro_bits = []
    if "VIXCLS" in mk:
        v = mk["VIXCLS"]
        level = ("sehr ruhig" if v["value"] < 15 else "normal" if v["value"] < 20
                 else "erhöht" if v["value"] < 30 else "hoch – der Markt ist nervös")
        macro_bits.append(f"Der VIX schloss am {day_label(v['date'])} bei {num(v['value'])} – die erwartete Schwankung ist {level}.")
    if "DGS10" in mk:
        y = mk["DGS10"]
        macro_bits.append(f"Die 10-jährige US-Rendite lag zuletzt bei {num(y['value'])} % "
                          f"({signed((y['value'] - y['prev']) * 100, 0, ' Bp.')}).")
    eur = next((d for d in devisen if d["pair"] == "EUR/USD"), None)
    if eur:
        macro_bits.append(f"Ein Euro kostete laut {eur['source']} {num(eur['value'], 4)} US-Dollar ({signed(eur['d1'])}).")
    if macro_bits:
        paras.append(" ".join(macro_bits))

    if watch:
        w = sorted(watch, key=lambda r: r["d1"], reverse=True)
        tops = [f"{r['name']} ({signed(r['d1'])})" for r in w[:2] if r["d1"] > 0]
        flops = [f"{r['name']} ({signed(r['d1'])})" for r in w[::-1][:2] if r["d1"] < 0]
        text = []
        if tops:
            text.append(f"Vorne in der Watchlist {'liegen' if len(tops) > 1 else 'liegt'} {' und '.join(tops)}")
        if flops:
            text.append(f"am Ende {'stehen' if len(flops) > 1 else 'steht'} {' und '.join(flops)}")
        if text:
            paras.append(", ".join(text) + ".")

    notes = []
    for r in markets + watch + groups["rohstoffe"]:
        notes += signals(r["name"], r)
    return mood, paras, notes


# ---------------------------------------------------------------- Ablauf

def run(args):
    now = datetime.now()
    if args.only_at and not in_time_window(args.only_at, now):
        log.info(f"{now:%H:%M} liegt nicht im Zeitfenster {args.only_at} - keine Aktualisierung.")
        return

    cfg = json.loads((BASE_DIR / "finanz_app.json").read_text(encoding="utf-8"))
    headers = alpaca_keys()
    start = (date.today() - timedelta(days=372)).isoformat()
    stocks = [s for g in ("maerkte", "sektoren", "watchlist", "rohstoffe") for s in cfg[g] if "/" not in s]
    cryptos = [s for s in cfg["rohstoffe"] if "/" in s]

    clock = alpaca_clock(headers)
    live = bool(clock["is_open"])
    bars = alpaca_bars(headers, stocks, start)
    latest = alpaca_latest(headers, stocks) if live else {}
    crypto = alpaca_crypto(cryptos, start) if cryptos else {}
    today_et = clock["timestamp"][:10]

    groups = {}
    for g in ("maerkte", "sektoren", "watchlist", "rohstoffe"):
        groups[g] = []
        for sym, name in cfg[g].items():
            closes = list(crypto.get(sym) or bars.get(sym) or [])
            if len(closes) < 2:
                log.warning(f"{sym}: keine Kursdaten")
                continue
            is_crypto = sym in crypto
            sym_live = is_crypto or (live and closes[-1][0] == today_et)
            if sym in latest and sym_live:
                closes[-1] = (closes[-1][0], latest[sym])
            source = "Alpaca (Krypto)" if is_crypto else "Alpaca (IEX)"
            groups[g].append(row(sym, name, closes, sym_live, source, g != "sektoren"))

    makro = []
    fred_start = (date.today() - timedelta(days=760)).isoformat()  # zwei Jahre fuer die Inflationsrate
    for sid, (name, unit) in cfg["fred"].items():
        try:
            entry = macro_entry(sid, name, unit, fred_series(sid, fred_start))
            if entry:
                makro.append(entry)
        except Exception as e:
            log.warning(f"FRED {sid}: {e}")

    rates, fx_source = ecb_rates(cfg["devisen"], start)
    devisen = []
    for pair, rows in rates.items():
        if len(rows) >= 2:
            devisen.append({"pair": pair, "value": rows[-1][1], "d1": pct(rows[-1][1], rows[-2][1]),
                            "date": rows[-1][0], "source": fx_source,
                            "hist": {"from": rows[0][0], "to": rows[-1][0], "c": [round(v, 5) for _, v in rows]}})
    devisen.sort(key=lambda d: cfg["devisen"].index(d["pair"][4:]) if d["pair"][4:] in cfg["devisen"] else 99)

    try:
        news = alpaca_news(headers, list(cfg["watchlist"]) + ["SPY", "QQQ"], cfg["news_count"])
    except Exception as e:
        log.warning(f"Nachrichten nicht abrufbar: {e}")
        news = []
    notenbank = notenbank_news(cfg["notenbank_count"])

    mood, paras, notes = summary(groups, makro, devisen, live, today_et)
    stamp = f"{now:%Y-%m-%dT%H:%M}"
    report = {
        "version": 1, "time": stamp, "live": live, "mood": mood, "paras": paras, "notes": notes,
        "groups": groups, "makro": makro, "devisen": devisen, "news": news, "notenbank": notenbank,
        "sources": SOURCES, "next_open": clock.get("next_open"), "next_close": clock.get("next_close"),
    }
    write(report, stamp)


def rounded(obj):
    return json.loads(json.dumps(obj), parse_float=lambda s: round(float(s), 4))


def write(report, stamp):
    (DATA_DIR / "reports").mkdir(parents=True, exist_ok=True)
    report = rounded(report)
    (DATA_DIR / "latest.json").write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # Archiv ohne Kursverlaeufe, damit die Dateien klein bleiben
    slim = json.loads(json.dumps(report))
    for rows in slim["groups"].values():
        for r in rows:
            r.pop("hist", None)
    for m in slim["makro"] + slim["devisen"]:
        m.pop("hist", None)
    name = stamp.replace(":", "")
    (DATA_DIR / "reports" / f"{name}.json").write_text(json.dumps(slim, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    index_path = DATA_DIR / "archiv.json"
    archive = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    archive = [a for a in archive if a["file"] != f"{name}.json"]
    archive.insert(0, {"file": f"{name}.json", "time": stamp, "mood": report["mood"], "live": report["live"]})
    index_path.write_text(json.dumps(archive, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    counts = {g: len(r) for g, r in report["groups"].items()}
    log.info(f"Daten geschrieben: {counts}, {len(report['makro'])} Makro, {len(report['devisen'])} Devisen, "
             f"{len(report['news'])} News, {len(report['notenbank'])} Notenbank, {len(archive)} im Archiv")


def main():
    parser = argparse.ArgumentParser(description="Daten fuer die Finanz-Web-App")
    parser.add_argument("--only-at", metavar="HH:MM,...", help="nur laufen, wenn es bis zu 59 Min. nach einer dieser Zeiten ist")
    args = parser.parse_args()
    setup_logging()
    try:
        run(args)
    except Exception:
        log.exception("Fehler:")
        sys.exit(1)


if __name__ == "__main__":
    main()
