#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вивантаження свічок усіх USDT-пар Binance spot — ВКЛЮЧНО З ДЕЛІСТНУТИМИ — з публічного архіву data.binance.vision.

Навіщо: тест на «живих сьогодні» монетах брехливо оптимістичний (мертві монети зникають із вибірки). Архів Binance зберігає історію і закритих пар.

Що качає:
  • 4h — для ВСІХ USDT-пар, уся історія (з 2017);
  • 1h — лише для ліквідних (були в топ-TOP1H за обсягом торгів у будь-який день від START1H), від START1H.
  «Хвіст» (поточний місяць, якого ще немає в місячному архіві) добирається з публічного дзеркала API data-api.binance.vision; запасний шлях — денні архіви.

Результат (папка out/):
  h4/<SYMBOL>.npz, h1/<SYMBOL>.npz  — масиви: ts (мс, int64), o,h,l,c (float32), qv (обсяг у USDT), tb (обсяг, куплений тейкерами, USDT), n (кількість угод)
  manifest.json — на кожен символ і ТФ: перша/остання свічка, кількість, звідки хвіст
  info.json     — підсумок запуску

Запуск локально:  python dump_wide.py out [--limit 20]      (потрібен лише numpy)
Секрети не потрібні.
"""
import csv, io, json, os, re, sys, time, zipfile, threading, urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt

import numpy as np

BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CDN = "https://data.binance.vision"
API = "https://data-api.binance.vision/api/v3/klines"      # публічне дзеркало ринкових даних (без гео-блоку)
DAY = 86400000
STEP = {"1h": 3600000, "4h": 4 * 3600000}
UA = {"User-Agent": "Mozilla/5.0 (candle-dump)"}
TOP1H = int(os.environ.get("TOP1H", "150") or 150)
START1H = os.environ.get("START1H", "2023-01") or "2023-01"
WORKERS = 24

SKIP_BASES = set("""USDC TUSD BUSD USDP USDS FDUSD DAI PAX USDSB UST AEUR EUR EURI GBP AUD BRL TRY RUB UAH NGN ZAR IDR PLN RON ARS JPY MXN CZK COP
WBTC WBETH BETH WBNB PAXG XAUT USD1 USDE BFUSD XUSD RLUSD""".split())
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")

_lock = threading.Lock()
_stats = dict(http=0, retry=0, fail=0)


def http_get(url, tries=5, timeout=40):
    """bytes або None (404). На інших помилках повторює з паузою; після вичерпання кидає виняток."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            with _lock:
                _stats["http"] += 1
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
            if e.code in (418, 429):
                time.sleep(5 + 5 * i)
        except Exception as e:
            last = e
        with _lock:
            _stats["retry"] += 1
        time.sleep(0.6 * (i + 1))
    with _lock:
        _stats["fail"] += 1
    raise RuntimeError(f"{url}: {last}")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def s3_list(prefix, delimiter="/"):
    """Повний лістинг з пагінацією → (префікси, ключі)."""
    prefixes, keys, marker = [], [], ""
    while True:
        q = {"prefix": prefix, "max-keys": "1000"}
        if delimiter:
            q["delimiter"] = delimiter
        if marker:
            q["marker"] = marker
        body = http_get(BUCKET + "?" + urllib.parse.urlencode(q))
        if body is None:
            break
        root = ET.fromstring(body)
        trunc, nxt, last = False, "", ""
        for el in root:
            t = _local(el.tag)
            if t == "IsTruncated":
                trunc = (el.text or "").strip().lower() == "true"
            elif t == "NextMarker":
                nxt = el.text or ""
            elif t == "CommonPrefixes":
                for p in el:
                    if _local(p.tag) == "Prefix":
                        prefixes.append(p.text); last = p.text
            elif t == "Contents":
                for p in el:
                    if _local(p.tag) == "Key":
                        keys.append(p.text); last = p.text
        if not trunc:
            break
        marker = nxt or last
        if not marker:
            break
    return prefixes, keys


def norm_ts(ts):
    """З 2025-01-01 архів Binance spot пише мікросекунди; зводимо до мілісекунд."""
    return ts // 1000 if ts >= 10 ** 14 else ts


def parse_zip(blob):
    """Рядки (ts_ms, o, h, l, c, qv, tb, n). Заголовок і сміття пропускаються."""
    out = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            if not name.lower().endswith(".csv"):
                continue
            with z.open(name) as f:
                for row in csv.reader(io.TextIOWrapper(f, "utf-8")):
                    if len(row) < 11 or not row[0].strip().isdigit():
                        continue
                    try:
                        out.append((norm_ts(int(row[0])), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                                    float(row[7]), float(row[10]), float(row[8])))
                    except ValueError:
                        continue
    return out


def api_tail(sym, iv, start_ms, now_ms):
    """Закриті свічки з публічного API від start_ms. None — ендпоінт недоступний."""
    rows, cur, step = [], start_ms, STEP[iv]
    for _ in range(6):
        url = f"{API}?symbol={sym}&interval={iv}&startTime={cur}&limit=1000"
        try:
            body = http_get(url, tries=3)
        except Exception:
            return None
        if body is None:
            return None
        arr = json.loads(body)
        if not arr:
            break
        for k in arr:
            if int(k[6]) > now_ms:                   # свічка ще не закрита
                continue
            rows.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7]), float(k[10]), float(k[8])))
        if len(arr) < 1000:
            break
        cur = int(arr[-1][0]) + step
    return rows


def daily_archive_tail(sym, iv, after_ms):
    _, keys = s3_list(f"data/spot/daily/klines/{sym}/{iv}/", delimiter="")
    rows = []
    for k in keys:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})\.zip$", k)
        if not m or not k.endswith(".zip"):
            continue
        d = dt.datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=dt.timezone.utc)
        if int(d.timestamp() * 1000) + DAY - 1 <= after_ms:
            continue
        blob = http_get(CDN + "/" + k)
        if blob:
            rows += parse_zip(blob)
    return rows


def month_of(key):
    m = re.search(r"-(\d{4})-(\d{2})\.zip$", key)
    return f"{m[1]}-{m[2]}" if m else None


def fetch_series(sym, iv, now_ms, since_month=None):
    """Місячні архіви + «хвіст». Повертає (відсортовані рядки без дублів, мета)."""
    _, keys = s3_list(f"data/spot/monthly/klines/{sym}/{iv}/", delimiter="")
    zips = sorted(k for k in keys if k.endswith(".zip"))
    if since_month:
        zips = [k for k in zips if (month_of(k) or "") >= since_month]
    rows = {}
    for k in zips:
        blob = http_get(CDN + "/" + k)
        if blob:
            for r in parse_zip(blob):
                rows[r[0]] = r
    last_ts = max(rows) if rows else None
    tail_src = "—"
    now_dt = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone.utc)
    prev_month = (now_dt.replace(day=1) - dt.timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # «хвіст» лише для активних пар (останній місячний файл — минулий або поточний місяць)
    if last_ts is not None and last_ts >= int(prev_month.timestamp() * 1000):
        start = last_ts + STEP[iv]
        tail = api_tail(sym, iv, start, now_ms)
        if tail is not None:
            tail_src = "api"
        else:
            try:
                tail = daily_archive_tail(sym, iv, last_ts); tail_src = "daily-archive"
            except Exception:
                tail = []; tail_src = "немає"
        for r in tail:
            rows[r[0]] = r
    out = [rows[t] for t in sorted(rows)]
    return out, dict(files=len(zips), tail=tail_src)


def is_wanted(sym, all_bases):
    if not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    if not base or base in SKIP_BASES:
        return False
    for suf in LEV_SUFFIX:                           # леверидж-токени BTCUP / ETHDOWN / XRPBULL
        if base.endswith(suf) and len(base) > len(suf) and base[:-len(suf)] in all_bases:
            return False
    return True


def save_npz(outdir, iv, sym, rows):
    a = np.array(rows, dtype=np.float64)
    path = os.path.join(outdir, "h4" if iv == "4h" else "h1", f"{sym}.npz")
    np.savez_compressed(path, ts=a[:, 0].astype(np.int64), o=a[:, 1].astype(np.float32), h=a[:, 2].astype(np.float32),
                        l=a[:, 3].astype(np.float32), c=a[:, 4].astype(np.float32), qv=a[:, 5].astype(np.float32),
                        tb=a[:, 6].astype(np.float32), n=a[:, 7].astype(np.float32))


def run_phase(syms, iv, now_ms, outdir, since_month, label, manifest, errors):
    done, t0 = 0, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_series, s, iv, now_ms, since_month): s for s in syms}
        for fu in as_completed(futs):
            s = futs[fu]
            try:
                rows, meta = fu.result()
            except Exception as e:
                errors[f"{iv}:{s}"] = str(e)[:200]; continue
            if not rows:
                errors[f"{iv}:{s}"] = "порожньо"; continue
            save_npz(outdir, iv, s, rows)
            manifest.setdefault(s, {})[iv] = dict(first=rows[0][0], last=rows[-1][0], n=len(rows), **meta)
            done += 1
            if done % 100 == 0:
                print(f"  [{label}] {done}/{len(syms)} · {time.time() - t0:.0f} с · http {_stats['http']} · повторів {_stats['retry']}", flush=True)
    print(f"[{label}] готово: {done}/{len(syms)} · {time.time() - t0:.0f} с", flush=True)


def select_liquid(outdir, syms, top, since_month):
    """Символи, що хоч раз від since_month були в топ-`top` за 30-денною медіаною денного обсягу (з 4h-даних)."""
    t_from = int(dt.datetime.strptime(since_month + "-01", "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000) // DAY
    series = {}
    for s in syms:
        p = os.path.join(outdir, "h4", f"{s}.npz")
        if not os.path.exists(p):
            continue
        z = np.load(p)
        d = z["ts"] // DAY
        u, inv = np.unique(d, return_inverse=True)
        series[s] = (u, np.bincount(inv, weights=z["qv"].astype(np.float64)))
    if not series:
        return []
    d0 = min(v[0][0] for v in series.values()); d1 = max(v[0][-1] for v in series.values())
    names = sorted(series)
    M = np.full((len(names), int(d1 - d0 + 1)), np.nan)
    for i, s in enumerate(names):
        u, q = series[s]
        M[i, (u - d0).astype(int)] = q
    W = 30
    med = np.full_like(M, np.nan)
    if M.shape[1] >= W:
        import warnings
        for a in range(0, len(names), 40):                     # шматками, щоб не роздувати пам'ять
            win = np.lib.stride_tricks.sliding_window_view(M[a:a + 40], W, axis=1)
            cnt = np.sum(~np.isnan(win), axis=2)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                m = np.nanmedian(win, axis=2)
            m[cnt < 20] = np.nan
            med[a:a + 40, W - 1:] = m
    days = np.arange(d0, d1 + 1)
    keep = set()
    for j in range(len(days)):
        if days[j] < t_from or (days[j] - t_from) % 7:         # вибірка раз на тиждень
            continue
        col = med[:, j]
        ok = np.where(~np.isnan(col))[0]
        if len(ok) == 0:
            continue
        order = ok[np.argsort(-col[ok])][:top]
        keep.update(names[i] for i in order)
    return sorted(keep)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    limit = None
    if "--limit" in argv:
        i = argv.index("--limit"); limit = int(argv[i + 1]); del argv[i:i + 2]
    outdir = argv[0] if argv else "out"
    os.makedirs(os.path.join(outdir, "h4"), exist_ok=True); os.makedirs(os.path.join(outdir, "h1"), exist_ok=True)
    now_ms = int(time.time() * 1000); now_ms -= now_ms % DAY      # межа поточного UTC-дня
    t0 = time.time()
    prefixes, _ = s3_list("data/spot/monthly/klines/")
    syms_all = sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes)
    print(f"символів в архіві (усі котирування): {len(syms_all)}", flush=True)
    bases = {s[:-4] for s in syms_all if s.endswith("USDT")}
    syms = [s for s in syms_all if is_wanted(s, bases)]
    if limit:
        syms = syms[:limit]
    print(f"USDT-пар до вивантаження: {len(syms)}", flush=True)
    if not syms:
        print("ПОМИЛКА: лістинг порожній — архів недоступний з цього сервера"); return 2
    manifest, errors = {}, {}
    run_phase(syms, "4h", now_ms, outdir, None, "4h усі", manifest, errors)
    liquid = select_liquid(outdir, [s for s in syms if s in manifest], TOP1H, START1H)
    print(f"ліквідних для 1h (топ-{TOP1H} від {START1H}): {len(liquid)}", flush=True)
    run_phase(liquid, "1h", now_ms, outdir, START1H, "1h ліквідні", manifest, errors)
    json.dump(manifest, open(os.path.join(outdir, "manifest.json"), "w"), indent=0)
    n4 = sum(1 for m in manifest.values() if "4h" in m); n1 = sum(1 for m in manifest.values() if "1h" in m)
    last4 = max((m["4h"]["last"] for m in manifest.values() if "4h" in m), default=0)
    info = dict(made_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), symbols_4h=n4, symbols_1h=n1, errors=len(errors),
                last_bar_4h=dt.datetime.fromtimestamp(last4 / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") if last4 else None,
                rows_4h=sum(m["4h"]["n"] for m in manifest.values() if "4h" in m), rows_1h=sum(m["1h"]["n"] for m in manifest.values() if "1h" in m),
                tail_api=sum(1 for m in manifest.values() for v in m.values() if v["tail"] == "api"),
                tail_daily=sum(1 for m in manifest.values() for v in m.values() if v["tail"] == "daily-archive"),
                tail_none=sum(1 for m in manifest.values() for v in m.values() if v["tail"] == "немає"),
                http=_stats["http"], failed_requests=_stats["fail"], seconds=round(time.time() - t0), error_examples=dict(list(errors.items())[:10]),
                top1h=TOP1H, start1h=START1H)
    json.dump(info, open(os.path.join(outdir, "info.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(info, ensure_ascii=False, indent=1))
    if n4 < 50 and not limit:
        print("ПОМИЛКА: вивантажено надто мало символів"); return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
