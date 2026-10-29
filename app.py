import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

OKX = "https://www.okx.com/api/v5"
INST = "XCH-USDT"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# період -> (розмір свічки на OKX, кількість свічок) для головного графіка XCH
RANGES = {
    "1H": ("1m", 60),
    "1D": ("5m", 288),
    "1W": ("1H", 168),
    "1M": ("4H", 180),
    "3M": ("1D", 90),
    "1Y": ("1D", 365),
    "5Y": ("1D", 1825),
    "ALL": ("1W", 600),  # від початку торгів на OKX
}
LABELS = {"ALL": "Весь час"}

# скільки секунд тримати відповідь у кеші (довгі періоди змінюються повільно)
TTL = {"1H": 10, "1D": 15, "1W": 60, "1M": 120, "3M": 300, "1Y": 600, "5Y": 900, "ALL": 900}

# ---------- Порівняння ----------
# для порівняння довгі періоди беремо тижневими свічками, щоб не робити сотні запитів
CMP_CRYPTO = {
    "1H": ("1m", 60),
    "1D": ("5m", 288),
    "1W": ("1H", 168),
    "1M": ("4H", 180),
    "3M": ("1D", 90),
    "1Y": ("1D", 365),
    "5Y": ("1W", 270),
    "ALL": ("1W", 600),
}
# період -> (range, interval) для Yahoo Finance (беремо із запасом, обрізає клієнт)
YF_RANGES = {
    "1H": ("1d", "1m"),
    "1D": ("5d", "5m"),
    "1W": ("1mo", "60m"),
    "1M": ("3mo", "1d"),
    "3M": ("6mo", "1d"),
    "1Y": ("2y", "1d"),
    "5Y": ("5y", "1wk"),
    "ALL": ("10y", "1wk"),
}
CRYPTO = {
    "BTC": "BTC-USDT",
    "ETH": "ETH-USDT",
    "BNB": "BNB-USDT",
    "SOL": "SOL-USDT",
    "XRP": "XRP-USDT",
    "DOGE": "DOGE-USDT",
}
STOCKS = {
    "SPX": "^GSPC",   # S&P 500
    "NDX": "^IXIC",   # NASDAQ Composite
    "AAPL": "AAPL",
    "MSFT": "MSFT",
    "NVDA": "NVDA",
    "TSLA": "TSLA",
    "AMZN": "AMZN",
    "GOOGL": "GOOGL",
}

CACHE = {}


def cached(key, fn, ttl=10):
    now = time.time()
    item = CACHE.get(key)
    if item and now - item[0] < ttl:
        return item[1]
    value = fn()
    CACHE[key] = (now, value)
    return value


def okx_get(path, params):
    for attempt in range(4):
        r = requests.get(f"{OKX}{path}", params=params, headers=HEADERS, timeout=10)
        if r.status_code == 429:
            time.sleep(0.6 * (attempt + 1))
            continue
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") == "50011":  # перевищено ліміт запитів
            time.sleep(0.6 * (attempt + 1))
            continue
        if payload.get("code") not in ("0", 0):
            raise RuntimeError(payload.get("msg") or "OKX error")
        return payload.get("data", [])
    raise RuntimeError("Забагато запитів до OKX, спробуйте за хвилину")


def fetch_candles(inst, bar, total):
    """Свіжі свічки (до 300) + історія сторінками по 100."""
    first_limit = min(300, total)
    rows = list(okx_get("/market/candles", {"instId": inst, "bar": bar, "limit": first_limit}))
    exhausted = len(rows) < first_limit  # історія закінчилась

    while not exhausted and len(rows) < total and rows:
        need = min(100, total - len(rows))
        page = okx_get(
            "/market/history-candles",
            {"instId": inst, "bar": bar, "limit": need, "after": rows[-1][0]},
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < need:
            break

    rows.reverse()  # від старих до нових
    return [
        {
            "time": int(r[0]) // 1000,
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        }
        for r in rows
    ]


def fetch_yahoo(symbol, rng, interval):
    last_err = None
    for host in ("query1", "query2"):
        try:
            r = requests.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(symbol)}",
                params={"range": rng, "interval": interval},
                headers=HEADERS,
                timeout=10,
            )
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            ts = res.get("timestamp") or []
            closes = res["indicators"]["quote"][0]["close"]
            return [
                {"time": int(t), "close": float(c)}
                for t, c in zip(ts, closes)
                if c is not None
            ]
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err


@app.route("/")
def index():
    ranges = [(k, LABELS.get(k, k)) for k in RANGES]
    return render_template("index.html", ranges=ranges)


@app.route("/api/candles")
def api_candles():
    rng = request.args.get("range", "1D")
    if rng not in RANGES:
        return jsonify({"status": "error", "message": "Невідомий період"}), 400
    bar, total = RANGES[rng]
    try:
        candles = cached(("c", rng), lambda: fetch_candles(INST, bar, total), TTL[rng])
        return jsonify({"status": "ok", "range": rng, "bar": bar, "candles": candles})
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(e)}), 502


@app.route("/api/compare")
def api_compare():
    """Ціни обраних активів (крипто з OKX, акції та індекси з Yahoo Finance)."""
    rng = request.args.get("range", "1D")
    if rng not in RANGES:
        return jsonify({"status": "error", "message": "Невідомий період"}), 400
    symbols = [
        s for s in request.args.get("symbols", "").split(",")
        if s in CRYPTO or s in STOCKS
    ][:14]
    ttl = TTL[rng]

    def load(sym):
        try:
            if sym in CRYPTO:
                bar, total = CMP_CRYPTO[rng]
                data = cached(
                    ("cc", sym, rng),
                    lambda: [
                        {"time": c["time"], "close": c["close"]}
                        for c in fetch_candles(CRYPTO[sym], bar, total)
                    ],
                    ttl,
                )
            else:
                yr, yi = YF_RANGES[rng]
                data = cached(("cy", sym, rng), lambda: fetch_yahoo(STOCKS[sym], yr, yi), ttl)
            return sym, data, None
        except Exception as e:  # noqa: BLE001
            return sym, [], str(e)

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(load, symbols))

    return jsonify({
        "status": "ok",
        "series": {s: d for s, d, _ in results if d},
        "errors": {s: (e or "немає даних") for s, d, e in results if not d},
    })


@app.route("/api/market")
def api_market():
    """Тікер за 24 год + книга ордерів."""
    try:
        def load():
            t = okx_get("/market/ticker", {"instId": INST})[0]
            book = okx_get("/market/books", {"instId": INST, "sz": 14})[0]
            last = float(t["last"])
            open24 = float(t["open24h"])
            return {
                "last": last,
                "open24h": open24,
                "high24h": float(t["high24h"]),
                "low24h": float(t["low24h"]),
                "vol24h": float(t["vol24h"]),
                "volUsd24h": float(t["volCcy24h"]),
                "change24h": (last - open24) / open24 * 100 if open24 else 0,
                "bid": float(t["bidPx"]) if t.get("bidPx") else None,
                "ask": float(t["askPx"]) if t.get("askPx") else None,
                "asks": [[float(a[0]), float(a[1])] for a in book["asks"]],
                "bids": [[float(b[0]), float(b[1])] for b in book["bids"]],
            }

        return jsonify({"status": "ok", **cached(("m",), load, 10)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(e)}), 502


if __name__ == "__main__":
    app.run(debug=True)
