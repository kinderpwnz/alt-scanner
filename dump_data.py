#!/usr/bin/env python3
"""Вивантаження історії свічок (1D і 4h) по списку монет у CSV (ts_ms,open,high,low,close,volume).
Лише стандартна бібліотека. Джерела (по черзі): Binance (data-api.binance.vision) → MEXC → Bybit.
Запуск:  python dump_data.py OUT_DIR            (реальні біржі)
         python dump_data.py OUT_DIR --mock     (перевірка логіки без мережі)
Змінні: COINS="BTC,ETH,..." (замість стандартного списку), START_1D / START_4H (рік початку)."""
import csv, json, os, sys, time, math, random
import urllib.request, urllib.error, urllib.parse

COINS = "BTC HBAR RUNE AVAX XLM INJ ONDO RAY SUI ICP GRASS ENA TIA TEL RENDER BCH COMP SEI FET THETA NEAR FIL EGLD".split()
ALIAS = {"RENDER": ["RENDER", "RNDR"], "FET": ["FET"], "POL": ["POL", "MATIC"]}
IV = {"1d": 86400000, "4h": 14400000}
BIN_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com", "https://api-gcp.binance.com"]
MEXC_HOSTS = ["https://api.mexc.com"]
BYBIT_HOSTS = ["https://api.bybit.com", "https://api.bytick.com"]
MOCK = "--mock" in sys.argv


def ts_year(y):
    return int(time.mktime(time.strptime(f"{y}-01-01", "%Y-%m-%d"))) * 1000 - time.timezone * 1000


def get_json(url, tries=4):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (alt-scanner data dump)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (400, 404):          # немає такої пари
                return None
            if e.code in (418, 429):
                time.sleep(5 * (k + 1))
        except Exception as e:                # мережа / тайм-аут
            last = str(e)[:80]
        time.sleep(1.5 * (k + 1))
    return {"_err": last}


# ───────────────────────── mock (лише для перевірки логіки) ─────────────────────────
_MOCK_CACHE = {}
def mock_series(sym, step):
    key = (sym, step)
    if key not in _MOCK_CACHE:
        rnd = random.Random(hash(sym) % 10**6)
        t0 = ts_year(2021) if sym != "NEWCOIN" else ts_year(2025)
        n = int((time.time() * 1000 - t0) // step) - 3
        p = 1.0; rows = []
        for i in range(n):
            o = p; c = o * math.exp(rnd.gauss(0.0002, 0.03)); h = max(o, c) * (1 + abs(rnd.gauss(0, .01))); l = min(o, c) * (1 - abs(rnd.gauss(0, .01)))
            rows.append([t0 + i * step, o, h, l, c, rnd.random() * 1e6]); p = c
        _MOCK_CACHE[key] = rows
    return _MOCK_CACHE[key]


def binance_like(host, sym, iv, start, kind):
    step = IV[iv]; out = []; cur = start; now = int(time.time() * 1000)
    while cur < now:
        if MOCK:
            S = mock_series(sym, step) if sym != "BADUSDT" else []
            if kind == "mexc" and sym.startswith("TEL"):
                S = mock_series(sym, step)
            elif kind == "bin" and sym.startswith("TEL"):
                S = []
            data = [[r[0], r[1], r[2], r[3], r[4], r[5], r[0] + step - 1] for r in S if r[0] >= cur][:1000]
        else:
            q = urllib.parse.urlencode({"symbol": sym, "interval": iv, "startTime": cur, "limit": 1000})
            data = get_json(f"{host}/api/v3/klines?{q}")
            if data is None:
                return out if out else None
            if isinstance(data, dict):
                return out if out else None
        if not data:
            break
        for r in data:
            out.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
        nxt = int(data[-1][0]) + step
        if nxt <= cur:
            break
        cur = nxt
        if len(data) < 1000:
            break
        if not MOCK:
            time.sleep(0.15)
    return out


def bybit(host, sym, iv, start):
    step = IV[iv]; code = {"1d": "D", "4h": "240"}[iv]; out = []; end = int(time.time() * 1000)
    while end > start:
        if MOCK:
            return None
        q = urllib.parse.urlencode({"category": "spot", "symbol": sym, "interval": code, "end": end, "limit": 1000})
        d = get_json(f"{host}/v5/market/kline?{q}")
        if not d or "_err" in d or d.get("retCode") != 0:
            return out if out else None
        lst = d["result"]["list"]
        if not lst:
            break
        for r in lst:
            out.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
        oldest = int(lst[-1][0])
        if oldest >= end:
            break
        end = oldest - 1
        if len(lst) < 1000:
            break
        time.sleep(0.15)
    out.sort()
    return [r for r in out if r[0] >= start]


def fetch(coin, iv, start):
    """повертає (рядки, джерело, символ) або (None, причина, None)"""
    now = int(time.time() * 1000); step = IV[iv]
    for base in ALIAS.get(coin, [coin]):
        sym = base + "USDT"
        for name, hosts, fn in (("binance", BIN_HOSTS, lambda h: binance_like(h, sym, iv, start, "bin")),
                                ("mexc", MEXC_HOSTS, lambda h: binance_like(h, sym, iv, start, "mexc")),
                                ("bybit", BYBIT_HOSTS, lambda h: bybit(h, sym, iv, start))):
            for h in hosts:
                rows = fn(h)
                if rows and len(rows) >= 60:
                    rows = sorted(set(rows))
                    rows = [r for r in rows if r[0] + step <= now]          # лише закриті свічки
                    return rows, name, sym
                if rows is None:
                    break                     # пари немає на цій біржі
    return None, "немає джерела", None


def merge_alias(coin, iv, start):
    """для перейменованих монет (RNDR→RENDER) склеюємо історію: старий тикер до першої свічки нового"""
    rows, src, sym = fetch(coin, iv, start)
    if rows is None or coin not in ALIAS or len(ALIAS[coin]) < 2:
        return rows, src, sym
    old = ALIAS[coin][1] + "USDT"
    if sym == old:
        return rows, src, sym
    first = rows[0][0]
    if first - start > 90 * 86400000:                                       # новий тикер починається пізно → пробуємо старий
        for name, hosts in (("binance", BIN_HOSTS),):
            for h in hosts:
                prev = binance_like(h, old, iv, start, "bin")
                if prev:
                    prev = [r for r in prev if r[0] < first]
                    return sorted(prev + rows), src + "+" + old, sym
    return rows, src, sym


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow([r[0]] + [f"{x:.12g}" for x in r[1:]])


def fng(out):
    d = get_json("https://api.alternative.me/fng/?limit=0&format=json") if not MOCK else {"data": [{"timestamp": str(int(time.time()) - 86400 * k), "value": "50"} for k in range(30)]}
    if not d or "data" not in d:
        return 0
    rows = sorted((int(x["timestamp"]) * 1000, int(x["value"])) for x in d["data"])
    with open(os.path.join(out, "fng.csv"), "w", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow(r)
    return len(rows)


def main():
    out = next((a for a in sys.argv[1:] if not a.startswith("--")), "out")
    os.makedirs(out, exist_ok=True)
    coins = [c.strip().upper() for c in os.environ.get("COINS", "").replace(",", " ").split() if c.strip()] or COINS
    s1 = ts_year(int(os.environ.get("START_1D", "2020"))); s4 = ts_year(int(os.environ.get("START_4H", "2023")))
    man = {}
    print(f"{'монета':8s} {'1D':>6s} {'з':10s} {'джерело':16s} | {'4h':>6s} {'з':10s}")
    for c in coins:
        rec = {}
        line = f"{c:8s}"
        for iv, st in (("1d", s1), ("4h", s4)):
            rows, src, sym = merge_alias(c, iv, st)
            if rows:
                write_csv(os.path.join(out, f"{c}_{iv}.csv"), rows)
                rec[iv] = dict(n=len(rows), first=rows[0][0], last=rows[-1][0], src=src, sym=sym)
                d0 = time.strftime("%Y-%m-%d", time.gmtime(rows[0][0] / 1000))
                line += f" {len(rows):6d} {d0:10s}" + (f" {src:16s} |" if iv == "1d" else "")
            else:
                rec[iv] = dict(n=0, err=src)
                line += f" {'—':>6s} {'':10s}" + (f" {src:16s} |" if iv == "1d" else "")
        man[c] = rec
        print(line, flush=True)
    n_f = fng(out)
    man["_meta"] = dict(made_utc=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), fng_rows=n_f, mock=MOCK)
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    miss = [c for c, r in man.items() if c != "_meta" and not r.get("1d", {}).get("n")]
    print("\nУсього монет:", len(coins), "· без денних даних:", miss or "немає", "· fng рядків:", n_f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
