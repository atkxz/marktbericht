"""Daten fuer die Finanz-Web-App (docs/): Kurse, Makro, Devisen, Nachrichten und Tagesbericht.

Quellen (alle offiziell bzw. mit Quellenangabe):
    Alpaca Markets  - Kurse von US-Aktien, ETFs und Bitcoin, Boersenuhr, Nachrichten
    Cboe            - VIX
    US Treasury     - Renditen von US-Staatsanleihen
    New York Fed    - Leitzins (Effective Federal Funds Rate)
    BLS             - Inflation und Arbeitslosenquote der USA
    EZB             - Euro-Referenzkurse
    Fed, EZB        - Pressemitteilungen der Notenbanken

FRED wird bewusst nicht genutzt: es beantwortet Anfragen von GitHub-Servern nicht.

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
CBOE_VIX = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
TREASURY_CSV = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                "{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
NYFED_EFFR = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/{n}.json"
BLS_API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
ECB_EXR = "https://data-api.ecb.europa.eu/service/data/EXR/D.{}.EUR.SP00.A"
NOTENBANK_FEEDS = {
    "Fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "EZB": "https://www.ecb.europa.eu/rss/press.html",
}
UA = {"User-Agent": "Mozilla/5.0 (Marktbericht-App)"}
SOURCES = [
    {"name": "Alpaca Markets (IEX)", "what": "Kurse, Börsenuhr, Nachrichten", "url": "https://alpaca.markets"},
    {"name": "Cboe", "what": "VIX", "url": "https://www.cboe.com/tradable_products/vix/"},
    {"name": "U.S. Department of the Treasury", "what": "Renditen von US-Staatsanleihen", "url": "https://home.treasury.gov/policy-issues/financing-the-government/interest-rate-statistics"},
    {"name": "Federal Reserve Bank of New York", "what": "Leitzins (EFFR)", "url": "https://www.newyorkfed.org/markets/reference-rates/effr"},
    {"name": "U.S. Bureau of Labor Statistics", "what": "Inflation, Arbeitslosenquote", "url": "https://www.bls.gov"},
    {"name": "Europäische Zentralbank", "what": "Euro-Referenzkurse, Pressemitteilungen", "url": "https://www.ecb.europa.eu"},
    {"name": "Federal Reserve", "what": "Pressemitteilungen", "url": "https://www.federalreserve.gov"},
]
# Makro-Kennzahl -> (Quelle, Link zur Originalreihe)
MAKRO_SOURCES = {
    "VIX": ("Cboe", "https://www.cboe.com/tradable_products/vix/vix_historical_data/"),
    "UST10": ("US Treasury", "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"),
    "UST2": ("US Treasury", "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"),
    "EFFR": ("New York Fed", "https://www.newyorkfed.org/markets/reference-rates/effr"),
    "CPI": ("BLS", "https://data.bls.gov/timeseries/CUUR0000SA0"),
    "UNRATE": ("BLS", "https://data.bls.gov/timeseries/LNS14000000"),
}

log = logging.getLogger("app")


def setup_logging():
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  [app] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)
    log.setLevel(logging.INFO)


def fetch(url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={**(headers or UA), **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
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


# ---------------------------------------------------------------- Makro (Originalquellen), EZB, Notenbanken

def us_date(s):
    m, d, y = s.split("/")
    return f"{y}-{m}-{d}"


def cboe_vix(start):
    rows = csv.reader(io.StringIO(fetch(CBOE_VIX).decode()))
    next(rows)
    return [(d, float(close)) for d, close in ((us_date(r[0]), r[4]) for r in rows if len(r) >= 5) if d >= start]


def treasury_yields(start):
    """{"2 Yr": [(datum, rendite), ...], "10 Yr": [...]} - Zinskurve des US-Finanzministeriums."""
    out = {"2 Yr": [], "10 Yr": []}
    for year in range(int(start[:4]), date.today().year + 1):
        for r in csv.DictReader(io.StringIO(fetch(TREASURY_CSV.format(year=year)).decode())):
            for col in out:
                if r.get(col):
                    out[col].append((us_date(r["Date"]), float(r[col])))
    return {k: sorted(v) for k, v in out.items()}


def nyfed_effr(n=520):
    d = json.loads(fetch(NYFED_EFFR.format(n=n)))
    return sorted((r["effectiveDate"], float(r["percentRate"])) for r in d["refRates"])


def bls_series(series, start_year):
    """Monatswerte {serien_id: [(JJJJ-MM-01, wert), ...]} von der US-Statistikbehoerde."""
    d = json.loads(fetch(BLS_API, body={"seriesid": series, "startyear": str(start_year), "endyear": str(date.today().year)}))
    if d.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS: {d.get('message')}")
    out = {}
    for s in d["Results"]["series"]:
        # Monate ohne Erhebung stehen als "-" in den Daten
        out[s["seriesID"]] = sorted((f"{x['year']}-{x['period'][1:]}-01", float(x["value"]))
                                    for x in s["data"] if re.fullmatch(r"M(0[1-9]|1[0-2])", x["period"])
                                    and re.fullmatch(r"-?\d+(\.\d+)?", x["value"]))
    return out


def makro_rows(start):
    """Rohdaten je Makro-Kennzahl; faellt eine Quelle aus, fehlen nur ihre Werte."""
    rows = {}
    for name, load in [("VIX", lambda: {"VIX": cboe_vix(start)}),
                       ("Treasury", lambda: {"UST10": (y := treasury_yields(start))["10 Yr"], "UST2": y["2 Yr"]}),
                       ("New York Fed", lambda: {"EFFR": nyfed_effr()}),
                       ("BLS", lambda: {"CPI": (b := bls_series(["CUUR0000SA0", "LNS14000000"], int(start[:4])))["CUUR0000SA0"],
                                        "UNRATE": b["LNS14000000"]})]:
        try:
            rows.update(load())
        except Exception as e:
            log.warning(f"{name} nicht abrufbar: {e}")
    return rows


def ecb_rates(currencies, start):
    """{"EUR/USD": [(datum, kurs), ...]} - Referenzkurse der EZB."""
    url = ECB_EXR.format("+".join(currencies)) + "?" + urllib.parse.urlencode({"startPeriod": start, "format": "csvdata"})
    out = {}
    for r in csv.DictReader(io.StringIO(fetch(url).decode())):
        out.setdefault(f"EUR/{r['CURRENCY']}", []).append((r["TIME_PERIOD"], float(r["OBS_VALUE"])))
    return {k: sorted(v) for k, v in out.items()}


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
    if sid == "CPI":  # Preisindex in Inflationsrate umrechnen (gleicher Monat im Vorjahr)
        by_month = dict(rows)
        rows = [(d, pct(v, by_month[f"{int(d[:4]) - 1}{d[4:]}"])) for d, v in rows if f"{int(d[:4]) - 1}{d[4:]}" in by_month]
    if len(rows) < 2:
        return None
    (d_prev, v_prev), (d_last, v_last) = rows[-2], rows[-1]
    source, url = MAKRO_SOURCES[sid]
    return {"id": sid, "name": name, "unit": unit, "value": v_last, "prev": v_prev, "date": d_last,
            "source": source, "url": url,
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
    if "VIX" in mk:
        v = mk["VIX"]
        level = ("sehr ruhig" if v["value"] < 15 else "normal" if v["value"] < 20
                 else "erhöht" if v["value"] < 30 else "hoch – der Markt ist nervös")
        macro_bits.append(f"Der VIX schloss am {day_label(v['date'])} bei {num(v['value'])} – die erwartete Schwankung ist {level}.")
    if "UST10" in mk:
        y = mk["UST10"]
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
    raw = makro_rows((date.today() - timedelta(days=760)).isoformat())  # zwei Jahre fuer die Inflationsrate
    for sid, (name, unit) in cfg["makro"].items():
        entry = macro_entry(sid, name, unit, raw[sid]) if sid in raw else None
        if entry:
            makro.append(entry)

    try:
        rates = ecb_rates(cfg["devisen"], start)
    except Exception as e:
        log.warning(f"EZB-Kurse nicht abrufbar: {e}")
        rates = {}
    devisen = []
    for pair, rows in rates.items():
        if len(rows) >= 2:
            devisen.append({"pair": pair, "value": rows[-1][1], "d1": pct(rows[-1][1], rows[-2][1]),
                            "date": rows[-1][0], "source": "EZB",
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
