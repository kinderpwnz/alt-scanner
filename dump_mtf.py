# -*- coding: utf-8 -*-
"""Вивантаження свічок 30m і 1h для 23 монет зі списку Влада (Binance spot, пара до USDT) — для тесту перетинів EMA на дрібних таймфреймах.
Потрібен файл dump_wide.py у тій самій папці (з нього беруться завантажувачі). Результат: out_mtf/m30/*.npz, out_mtf/h1/*.npz, manifest.json, info.json.
Змінні середовища: START (РРРР-ММ, за замовчуванням 2022-01)."""
import os, sys, json, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import dump_wide as W

W.STEP["30m"] = 30 * 60 * 1000
COINS = "BTC HBAR RUNE AVAX XLM INJ ONDO RAY SUI ICP GRASS ENA TIA TEL RENDER BCH COMP SEI FET THETA NEAR FIL EGLD".split()
ALIASES = {"RENDER": ["RNDR"]}          # стара назва тікера: історія до перейменування
START = os.environ.get("START", "2022-01") or "2022-01"
DIRS = {"30m": "m30", "1h": "h1"}


def fetch_coin(base, iv, now_ms):
    """Об'єднує основну пару й старі назви; повертає (рядки, мета, знайдені_символи)."""
    rows, found, meta = {}, [], {}
    for name in ALIASES.get(base, []) + [base]:          # старе спочатку, нове перекриває дублі
        sym = name + "USDT"
        r, m = W.fetch_series(sym, iv, now_ms, START)
        if r:
            found.append(sym); meta = m
            for x in r:
                rows[x[0]] = x
    return [rows[t] for t in sorted(rows)], meta, found


def save(outdir, iv, base, rows):
    a = np.array(rows, dtype=np.float64)
    np.savez_compressed(os.path.join(outdir, DIRS[iv], f"{base}.npz"), ts=a[:, 0].astype(np.int64), o=a[:, 1].astype(np.float32), h=a[:, 2].astype(np.float32),
                        l=a[:, 3].astype(np.float32), c=a[:, 4].astype(np.float32), qv=a[:, 5].astype(np.float32), tb=a[:, 6].astype(np.float32), n=a[:, 7].astype(np.float32))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    outdir = argv[0] if argv else "out_mtf"
    for d in DIRS.values():
        os.makedirs(os.path.join(outdir, d), exist_ok=True)
    now_ms = int(time.time() * 1000); now_ms -= now_ms % W.DAY
    t0 = time.time(); manifest, errors = {}, {}
    jobs = [(b, iv) for b in COINS for iv in ("30m", "1h")]
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fetch_coin, b, iv, now_ms): (b, iv) for b, iv in jobs}
        for fu in as_completed(futs):
            b, iv = futs[fu]
            try:
                rows, meta, found = fu.result()
            except Exception as e:
                errors[f"{iv}:{b}"] = str(e)[:200]; continue
            if not rows:
                errors[f"{iv}:{b}"] = "порожньо (пари немає в архіві Binance)"; continue
            save(outdir, iv, b, rows)
            manifest.setdefault(b, {})[iv] = dict(first=rows[0][0], last=rows[-1][0], n=len(rows), symbols=found, **meta)
    json.dump(manifest, open(os.path.join(outdir, "manifest.json"), "w"), indent=0)
    ok30 = sum(1 for m in manifest.values() if "30m" in m); ok1h = sum(1 for m in manifest.values() if "1h" in m)
    fmt = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    info = dict(made_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), start=START, coins_30m=ok30, coins_1h=ok1h, errors=errors,
                last_bar={f"{b}:{iv}": fmt(v["last"]) for b, m in manifest.items() for iv, v in m.items() if b in ("BTC", "SUI")},
                rows_30m=sum(m["30m"]["n"] for m in manifest.values() if "30m" in m), rows_1h=sum(m["1h"]["n"] for m in manifest.values() if "1h" in m),
                seconds=round(time.time() - t0))
    json.dump(info, open(os.path.join(outdir, "info.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(info, ensure_ascii=False, indent=1))
    if ok30 < 10 or ok1h < 10:
        print("ПОМИЛКА: замало монет завантажено (архів недоступний?)"); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
