#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SPOT ALT PLANNER — СКАНЕР  (топ-400 за капіталізацією → Telegram)

Що шукає:
  🚀 РІЗКІ ІМПУЛЬСИ ВГОРУ на 30m / 1h / 4h (як рух OP): велике зміщення ціни за кілька свічок,
     майже без відкатів, на новому 20-свічковому максимумі. Усе виміряно в ATR, тому працює для будь-якої монети.
  🟢 BUY за логікою індикатора «Spot Alt Planner» (перетин EMA 21/50 + свічка + 1D-фільтр) — на закритті 4h.
  👀 «Майже сигнал» (4h): EMA21 підтягується до EMA50 (НЕ тестувалось).

Дані: публічні API бірж (ключі НЕ потрібні) + CoinGecko для рейтингу за капіталізацією.
Без сторонніх бібліотек: лише Python 3.9+. Лише ЛОНГ, лише СПОТ.

Запуск:
  python scanner.py --selftest              перевірити джерела даних, CoinGecko і Telegram
  python scanner.py --chat-id               показати твій chat id (після /start боту)
  python scanner.py --dry-run --all-tfs --lookback 6
                                            показати, що відбувалось за останні 6 свічок на всіх ТФ (нічого не надсилає)
  python scanner.py                         звичайний запуск за розкладом (надсилає в Telegram)

Налаштування — змінні середовища (див. клас P нижче). Це інструмент пошуку, а не гарантія прибутку.
Не фінансова порада.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

H4 = 4 * 3600 * 1000
DAY = 24 * 3600 * 1000
TF_MS = {"30m": 1800 * 1000, "1h": 3600 * 1000, "4h": H4}
UA = {"User-Agent": "Mozilla/5.0 (compatible; SpotAltPlanner/2.0)"}
HERE = os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════ CONFIG (змінні середовища) ═══════════════════════════════
class P:  # значення за замовчуванням = налаштування індикатора + відкалібровані пороги імпульсів
    def __init__(self, env=None):
        e = (env if env is not None else os.environ).get

        def f(n, d):
            v = e(n)
            try:
                return float(v) if v not in (None, "") else d
            except ValueError:
                return d

        def s(n, d):
            v = e(n)
            return v.strip() if v not in (None, "") else d

        self.token = s("TELEGRAM_TOKEN", "")
        self.chat_id = s("TELEGRAM_CHAT_ID", "")
        self.sources = [x.strip().lower() for x in s("SOURCES", "binance,bybit,mexc").split(",") if x.strip()]
        self.tfs = [x.strip() for x in s("TFS", "30m,1h,4h").split(",") if x.strip() in TF_MS]
        self.every_min = f("RUN_EVERY_MIN", 30)              # як часто запускається розклад (хв) — щоб не повторювати ТФ
        # вибірка монет
        self.top_n = int(f("TOP_N", 400))                    # топ-N за капіталізацією
        self.min_vol = f("MIN_QUOTE_VOL", 300_000)           # мін. обіг пари за 24h, USDT
        self.min_atr_pct = f("MIN_ATR_PCT", 0.3)             # відсікає стейблкоїни (ATR 4h < 0.3% ціни)
        self.cg_key = s("COINGECKO_API_KEY", "")
        # --- різкий імпульс ---
        self.imp_g3 = f("IMP_G3", 3.0)                       # зсув за 3 свічки, в ATR
        self.imp_g6 = f("IMP_G6", 4.5)                       # зсув за 6 свічок, в ATR
        self.imp_er = f("IMP_ER", 0.7)                       # ефективність руху (1.0 = пряма лінія вгору)
        self.imp_brk = int(f("IMP_BRK_LEN", 20))             # закриття вище максимуму N попередніх свічок
        self.imp_cool = int(f("IMP_COOLDOWN", 6))            # не повторювати імпульс частіше ніж раз на N свічок
        self.imp_min_pct = {"30m": f("IMP_MIN_PCT_30M", 3.0), "1h": f("IMP_MIN_PCT_1H", 4.0), "4h": f("IMP_MIN_PCT_4H", 6.0)}
        self.imp_rvol_flag = f("IMP_RVOL_FLAG", 1.5)         # RVOL ≥ цього = «V✓» (на відбір не впливає)
        self.imp_top = int(f("IMP_TOP", 10))                 # макс. монет у одному повідомленні
        # --- сигнал BUY (як в індикаторі) ---
        self.signals = f("SIGNALS", 1) != 0
        self.fast = int(f("FAST", 21))
        self.slow = int(f("SLOW", 50))
        self.atr_len = int(f("ATR_LEN", 14))
        self.sl_mult = f("SL_MULT", 2.5)
        self.need_candle = f("NEED_CANDLE", 1) != 0
        self.use_htf = f("USE_HTF", 1) != 0
        self.htf_len = int(f("HTF_LEN", 21))
        self.btc_mode = s("BTC_MODE", "warn").lower()        # off | warn | block
        self.btc_len = int(f("BTC_LEN", 50))
        self.vol_mode = s("VOL_MODE", "mark").lower()        # off | mark | block
        self.vol_len = int(f("VOL_LEN", 20))
        self.min_rvol = f("MIN_RVOL", 1.0)
        self.tp1r, self.tp2r, self.tp3r = f("TP1R", 1.0), f("TP2R", 2.0), f("TP3R", 3.0)
        self.w1, self.w2 = f("W1", 40.0), f("W2", 30.0)
        self.w3 = max(0.0, 100.0 - self.w1 - self.w2)
        self.be_after_tp1 = f("BE_AFTER_TP1", 1) != 0
        self.lock_after_tp2 = f("LOCK_AFTER_TP2", 1) != 0
        self.max_days = f("MAX_DAYS", 7.0)
        # калькулятор
        self.deposit = f("DEPOSIT", 1000.0)
        self.risk_pct = f("RISK_PCT", 1.0)
        self.max_pos_pct = f("MAX_POS_PCT", 30.0)
        self.fee_pct = f("FEE_PCT", 0.1)
        # звіт
        self.lookback = max(1, int(f("LOOKBACK_BARS", 1)))
        self.show_blocked = f("SHOW_BLOCKED", 0) != 0
        self.show_near = f("SHOW_NEAR", 0) != 0
        self.near_atr = f("NEAR_ATR", 0.6)
        self.near_max = int(f("NEAR_MAX", 8))
        self.heartbeat = f("HEARTBEAT", 1) != 0              # раз на добу «сканер живий», навіть без сигналів
        self.tz_name = s("TZ_NAME", "Europe/Paris")
        self.tv_ex = s("TV_EXCHANGE", "").upper()            # напр. BINANCE — біржа для посилань на графік (інакше = джерело даних)
        self.workers = int(f("WORKERS", 10))


# ═══════════════════════════════ HTTP ═══════════════════════════════
class FetchError(Exception):
    pass


def http_json(url, params=None, data=None, timeout=20, retries=3, headers=None):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = "?"
    for k in range(retries):
        try:
            hdr = dict(UA)
            if headers:
                hdr.update(headers)
            body = None
            if data is not None:
                body = json.dumps(data).encode("utf-8")
                hdr["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            last = f"HTTP {ex.code}"
            if ex.code in (400, 401, 403, 404, 451):  # блокування/помилка запиту — повтор не допоможе
                break
            time.sleep(6.0 * (k + 1) if ex.code in (418, 429) else 1.5 * (k + 1))
        except Exception as ex:  # мережа, таймаут, JSON
            last = f"{type(ex).__name__}: {str(ex)[:80]}"
            time.sleep(1.0 * (k + 1))
    raise FetchError(f"{url.split('?')[0]} → {last}")


# ═══════════════════════════════ ДЖЕРЕЛА ДАНИХ (біржі) ═══════════════════════════════
class _Src:
    name = ""
    tv = ""
    hosts: list = []
    intervals: dict = {}

    def __init__(self):
        self._h = 0

    def _get(self, path, params=None):
        errs = []
        for off in range(len(self.hosts)):
            h = self.hosts[(self._h + off) % len(self.hosts)]
            try:
                d = http_json(h + path, params)
                self._h = (self._h + off) % len(self.hosts)
                return d
            except FetchError as ex:
                errs.append(str(ex))
        raise FetchError(" | ".join(errs))


class Binance(_Src):
    name, tv = "binance", "BINANCE"
    hosts = ["https://data-api.binance.vision", "https://api.binance.com", "https://api-gcp.binance.com"]
    intervals = {"30m": "30m", "1h": "1h", "4h": "4h"}

    def universe(self):
        out = []
        for t in self._get("/api/v3/ticker/24hr"):
            s = t.get("symbol", "")
            if s.endswith("USDT"):
                out.append((s[:-4], s, float(t.get("quoteVolume") or 0)))
        return out

    def klines(self, symbol, tf="4h", limit=500):
        raw = self._get("/api/v3/klines", {"symbol": symbol, "interval": self.intervals[tf], "limit": limit})
        return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in raw]


class Mexc(Binance):  # той самий формат, інші хост і назва 1h
    name, tv = "mexc", "MEXC"
    hosts = ["https://api.mexc.com"]
    intervals = {"30m": "30m", "1h": "60m", "4h": "4h"}


class Bybit(_Src):
    name, tv = "bybit", "BYBIT"
    hosts = ["https://api.bybit.com", "https://api.bytick.com"]
    intervals = {"30m": "30", "1h": "60", "4h": "240"}

    def _chk(self, d):
        if d.get("retCode") != 0:
            raise FetchError(f"bybit retCode={d.get('retCode')} {d.get('retMsg')}")
        return d["result"]["list"]

    def universe(self):
        out = []
        for t in self._chk(self._get("/v5/market/tickers", {"category": "spot"})):
            s = t.get("symbol", "")
            if s.endswith("USDT"):
                out.append((s[:-4], s, float(t.get("turnover24h") or 0)))
        return out

    def klines(self, symbol, tf="4h", limit=500):
        raw = self._chk(self._get("/v5/market/kline", {"category": "spot", "symbol": symbol,
                                                         "interval": self.intervals[tf], "limit": limit}))
        rows = [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in raw]
        rows.sort()  # Bybit віддає від нових до старих
        return rows


SOURCES = {c.name: c for c in (Binance, Bybit, Mexc)}

STABLES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "USD1", "BUSD", "EUR", "EURI", "AEUR", "XUSD", "PYUSD", "RLUSD",
    "BFUSD", "USDS", "USDD", "GUSD", "FRAX", "LUSD", "SUSD", "UST", "TRY", "BRL", "GBP", "AUD", "USDX", "USDT",
    "USDG", "USDY", "USDF", "USD0", "USDTB", "BUIDL", "USYC", "SYRUPUSDC",
}
LEV_RE = re.compile(r"(3L|3S|5L|5S)$")


def good_base(b):
    return b not in STABLES and not (len(b) > 3 and LEV_RE.search(b)) and b.isascii()


# ═══════════════════════════════ РЕЙТИНГ ЗА КАПІТАЛІЗАЦІЄЮ (CoinGecko) ═══════════════════════════════
def fetch_mcap_top(p, n):
    """[(rank, SYMBOL, market_cap)] — топ-n за капіталізацією. Безкоштовний API без ключа; ключ (demo) — опційно."""
    hdr = {"x-cg-demo-api-key": p.cg_key} if p.cg_key else None
    out, page = [], 1
    while len(out) < n and page <= 3:
        d = http_json("https://api.coingecko.com/api/v3/coins/markets",
                      {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page,
                       "sparkline": "false"}, headers=hdr, retries=4)
        if not isinstance(d, list) or not d:
            break
        for c in d:
            out.append((int(c.get("market_cap_rank") or len(out) + 1), str(c.get("symbol", "")).upper(),
                        float(c.get("market_cap") or 0)))
        page += 1
        time.sleep(2.0)
    if len(out) < 50:
        raise FetchError("CoinGecko: замало даних")
    return out[:n]


def build_pool(p, uni, mcap):
    """Зіставляє топ-N за капіталізацією з USDT-парами біржі. Повертає [(base, symbol, rank|None, quote_vol)]."""
    by_base = {}
    for base, sym, qv in uni:
        by_base.setdefault(base, (sym, qv))
    pool, seen = [], set()
    if mcap:
        for rank, base, _cap in mcap:
            if base in seen or not good_base(base) or base not in by_base:
                continue
            sym, qv = by_base[base]
            if qv < p.min_vol:
                continue
            seen.add(base)
            pool.append((base, sym, rank, qv))
    else:  # запасний варіант: найбільші за обігом
        ranked = sorted([(b, s, None, q) for b, (s, q) in by_base.items() if good_base(b) and q >= p.min_vol],
                        key=lambda x: -x[3])
        pool = ranked[:p.top_n]
    return pool


# ═══════════════════════════════ ІНДИКАТОРИ (як у Pine) ═══════════════════════════════
def ema(vals, n):
    a = 2.0 / (n + 1)
    e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = a * v + (1 - a) * e
        out.append(e)
    return out


def atr(rows, n):
    tr = []
    for i, r in enumerate(rows):
        if i == 0:
            tr.append(r[2] - r[3])
        else:
            pc = rows[i - 1][4]
            tr.append(max(r[2] - r[3], abs(r[2] - pc), abs(r[3] - pc)))
    out = [None] * len(rows)
    if len(rows) < n:
        return out
    s = sum(tr[:n]) / n
    out[n - 1] = s
    for i in range(n, len(rows)):
        s = (s * (n - 1) + tr[i]) / n
        out[i] = s
    return out


def htf_flags(rows, length):
    """Для кожної 4h-свічки: True/False = вчорашній (завершений) денний close вище/нижче EMA(length) денного ТФ; None = мало історії."""
    days = {}
    for r in rows:
        days.setdefault(r[0] // DAY, []).append(r)
    keys = [k for k in sorted(days) if days[k][-1][0] % DAY == DAY - H4]  # день завершений (є остання 20:00 UTC свічка)
    closes = [days[k][-1][4] for k in keys]
    e = ema(closes, length) if closes else []
    st = {k: (closes[j] > e[j]) for j, k in enumerate(keys) if j >= length}
    return [st.get(r[0] // DAY - 1) for r in rows]


def btc_up_map(btc_rows, length):
    c = [r[4] for r in btc_rows]
    e = ema(c, length)
    return {r[0]: (c[i] > e[i]) for i, r in enumerate(btc_rows)}


def fmt_price(x):
    if x is None or x != x:
        return "–"
    d = max(2, min(8, 5 - int(math.floor(math.log10(abs(x)))))) if x else 2
    return f"{x:.{d}f}"


# ═══════════════════════════════ ДЕТЕКТОР РІЗКОГО ІМПУЛЬСУ ═══════════════════════════════
def detect_impulses(rows, p, tf):
    """
    rows: ЗАКРИТІ свічки (ms,o,h,l,c,v) за зростанням часу. Повертає події (без стану: усе перераховується з історії).
    Подія на свічці i, якщо одночасно:
      • зсув закриття за k=3 свічки ≥ IMP_G3 ATR  АБО  за k=6 свічок ≥ IMP_G6 ATR   (ATR береться ДО руху, на свічці i-k);
      • ефективність руху ER = чистий зсув / сума модулів змін закриттів ≥ IMP_ER   (відсікає «пилу», лишає майже пряму лінію);
      • зсув у % ≥ мінімуму для цього ТФ                                             (відсікає малорухливі монети);
      • закриття вище максимуму IMP_BRK_LEN попередніх свічок                        (пробій, а не відскок усередині діапазону);
      • минуло > IMP_COOLDOWN свічок після попередньої події                         (одна подія на один рух).
    """
    n = len(rows)
    if n < 60:
        return []
    c = [r[4] for r in rows]
    a = atr(rows, p.atr_len)
    vol = [r[5] for r in rows]
    floor = p.imp_min_pct.get(tf, 3.0)
    events, last = [], -10 ** 9
    for i in range(max(30, p.imp_brk + 1), n):
        best = None
        for k, gmin in ((3, p.imp_g3), (6, p.imp_g6)):
            j = i - k
            base = a[j]
            if not base:
                continue
            net = c[i] - c[j]
            if net <= 0:
                continue
            gain = net / base
            pct = (c[i] / c[j] - 1) * 100
            path = sum(abs(c[m] - c[m - 1]) for m in range(j + 1, i + 1))
            er = net / path if path > 0 else 0.0
            if gain >= gmin and er >= p.imp_er and pct >= floor:
                score = gain / gmin
                if best is None or score > best[0]:
                    best = (score, k, gain, er, pct)
        if not best:
            continue
        if c[i] <= max(r[2] for r in rows[i - p.imp_brk:i]):
            continue
        if i - last <= p.imp_cool:
            continue
        last = i
        _, k, gain, er, pct = best
        seg = rows[i - k:i + 1]
        avg = sum(vol[i - 20:i]) / 20 if i >= 20 else 0
        rv = max((vol[m] / avg if avg > 0 else 0.0) for m in range(max(0, i - 2), i + 1))
        events.append(dict(i=i, k=k, gain=gain, er=er, pct=pct, close=c[i], atr=a[i], rvol=rv,
                           origin=min(r[3] for r in seg), top=max(r[2] for r in seg)))
    return events


# ═══════════════════════════════ ЛОГІКА ІНДИКАТОРА (state machine, 4h) ═══════════════════════════════
def analyze(rows, p, btc_up):
    """rows: ЗАКРИТІ 4h-свічки (ms,o,h,l,c,v) за зростанням часу. Повертає події та контекст останньої свічки."""
    n = len(rows)
    if n < max(120, p.slow * 2):
        return None
    c = [r[4] for r in rows]
    f = ema(c, p.fast)
    s = ema(c, p.slow)
    a = atr(rows, p.atr_len)
    htf = htf_flags(rows, p.htf_len)
    vol = [r[5] for r in rows]
    rv = [0.0] * n
    for i in range(p.vol_len, n):
        avg = sum(vol[i - p.vol_len:i]) / p.vol_len
        rv[i] = vol[i] / avg if avg > 0 else 0.0
    rvs = [max(rv[max(0, i - 2):i + 1]) for i in range(n)]
    max_hold = max(1, round(p.max_days * 6))
    warm = max(60, p.slow + p.atr_len + 1)

    in_trade = False
    entry_bar = -1
    entry = stop = r_unit = tp1 = tp2 = tp3 = None
    hit1 = hit2 = False
    events, raw_buys = [], []

    for i in range(1, n):
        op, hi, lo, cl = rows[i][1], rows[i][2], rows[i][3], rows[i][4]
        cross_up = f[i - 1] <= s[i - 1] and f[i] > s[i]
        # 1) керування відкритою (віртуальною) угодою — як у Pine, до пошуку нового сигналу
        if in_trade and i > entry_bar:
            sl = t1 = t2 = t3 = tex = False
            if lo <= stop:
                sl = True
            else:
                t1 = (not hit1) and hi >= tp1
                t2 = (hit1 or t1) and (not hit2) and hi >= tp2
                t3 = (hit2 or t2) and hi >= tp3
                if not t3 and i - entry_bar >= max_hold:
                    tex = True
            if t1:
                hit1 = True
                events.append(dict(i=i, kind="TP1"))
                if p.be_after_tp1:
                    stop = max(stop, entry)
            if t2:
                hit2 = True
                events.append(dict(i=i, kind="TP2"))
                if p.lock_after_tp2:
                    stop = max(stop, tp1)
            if sl or t3 or tex:
                in_trade = False
                events.append(dict(i=i, kind="STOP" if sl else ("TP3" if t3 else "TIME")))
        # 2) сигнал
        if i >= warm and a[i] is not None and cross_up:
            candle_ok = (not p.need_candle) or cl > op
            htf_ok = (not p.use_htf) or bool(htf[i])
            bu = btc_up.get(rows[i][0] - H4) if btc_up is not None else None  # як в Pine: BTC за попередню 4h-свічку
            btc_ok = p.btc_mode != "block" or bool(bu)
            vol_strong = rvs[i] >= p.min_rvol
            vol_ok = p.vol_mode != "block" or vol_strong
            all_ok = htf_ok and btc_ok and vol_ok
            if candle_ok and all_ok:
                raw_buys.append(i)
            if not in_trade:
                if candle_ok and all_ok:
                    in_trade, entry_bar, hit1, hit2 = True, i, False, False
                    entry = cl
                    r_unit = p.sl_mult * a[i]
                    stop = entry - r_unit
                    tp1, tp2, tp3 = entry + p.tp1r * r_unit, entry + p.tp2r * r_unit, entry + p.tp3r * r_unit
                    events.append(dict(i=i, kind="BUY", entry=entry, r=r_unit, stop=stop, tp1=tp1, tp2=tp2, tp3=tp3,
                                       htf=bool(htf[i]), btc=bu, rvol=rvs[i], vol_strong=vol_strong, atr_pct=a[i] / cl * 100))
                else:
                    why = ("свічка" if not candle_ok else "1D проти" if not htf_ok else "BTC ↓" if not btc_ok else "слабкий об'єм")
                    events.append(dict(i=i, kind="BLOCKED", why=why))

    last = n - 1
    near = None
    if (f[last] < s[last] and a[last] and last >= 3 and a[last - 3] and not in_trade
            and (htf[last] or not p.use_htf)):
        g0 = (s[last] - f[last]) / a[last]
        g3 = (s[last - 3] - f[last - 3]) / a[last - 3]
        if g0 <= p.near_atr and g0 < g3:
            near = dict(gap=g0, gap3=g3)
    return dict(events=events, raw_buys=raw_buys, near=near, last=last, in_trade=in_trade,
                atr_pct=(a[last] / c[last] * 100 if a[last] else None), htf_last=htf[last], rvol_last=rvs[last],
                atr_last=a[last], trend4h=f[last] > s[last], htf_ok_last=htf[last])


def size_calc(p, entry, r_unit):
    risk_frac = r_unit / entry
    loss_unit = risk_frac + 2.0 * p.fee_pct / 100.0
    raw = p.deposit * p.risk_pct / 100.0 / loss_unit
    pos = min(raw, p.deposit * p.max_pos_pct / 100.0)
    return pos, pos * loss_unit


# ═══════════════════════════════ TELEGRAM ═══════════════════════════════
def tg_send(p, text):
    if not p.token or not p.chat_id:
        raise FetchError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не задано")
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for ch in chunks:
        d = http_json(f"https://api.telegram.org/bot{p.token}/sendMessage",
                      data={"chat_id": p.chat_id, "text": ch.rstrip(), "disable_web_page_preview": True})
        if not d.get("ok"):
            raise FetchError(f"telegram: {d}")


def local_time(ms, tz_name):
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    s = dt.strftime("%d.%m %H:%M UTC")
    try:
        from zoneinfo import ZoneInfo
        s += " · " + dt.astimezone(ZoneInfo(tz_name)).strftime("%H:%M") + " " + tz_name.split("/")[-1]
    except Exception:
        pass
    return s


# ═══════════════════════════════ СКАН ═══════════════════════════════
def pick_source(p, log=print):
    errs = []
    for name in p.sources:
        cls = SOURCES.get(name)
        if not cls:
            continue
        src = cls()
        try:
            uni = src.universe()
            btc = src.klines("BTCUSDT", "4h", 500)
            if len(uni) < 20 or len(btc) < 100:
                raise FetchError("замало даних")
            return src, uni, btc, errs
        except FetchError as ex:
            errs.append(f"{name}: {ex}")
            log(f"  ✗ {name}: {ex}")
    return None, None, None, errs


def closed_only(rows, now_ms, tf):
    L = TF_MS[tf]
    return [r for r in rows if r[0] + L <= now_ms]


def tf_due(now_ms, tf, every_min):
    """ТФ перевіряється, якщо його остання свічка закрилась менш ніж RUN_EVERY_MIN хвилин тому (без стану між запусками)."""
    L = TF_MS[tf]
    age_min = (now_ms - (now_ms // L) * L) / 60000.0
    return age_min < every_min


def scan(p, now_ms=None, log=print, all_tfs=False, lookback=None, src=None, uni=None, btc_raw=None, mcap=None):
    now_ms = now_ms or int(time.time() * 1000)
    lookback = lookback or p.lookback
    errs = []
    if src is None:
        src, uni, btc_raw, errs = pick_source(p, log)
        if src is None:
            return dict(error="; ".join(errs) or "жодного джерела", errs=errs)
    # рейтинг за капіталізацією
    ranked_by = "капіталізацією (CoinGecko)"
    if mcap is None:
        try:
            mcap = fetch_mcap_top(p, p.top_n)
        except FetchError as ex:
            log(f"  ✗ CoinGecko: {ex} → беру топ за обігом на біржі")
            errs.append(f"coingecko: {ex}")
            mcap, ranked_by = None, "обігом на біржі (CoinGecko недоступний)"
    pool = build_pool(p, uni, mcap)
    due = [tf for tf in p.tfs if all_tfs or tf_due(now_ms, tf, p.every_min)]
    do_signals = p.signals and "4h" in due

    btc_rows = closed_only(btc_raw, now_ms, "4h")
    btc_up = btc_up_map(btc_rows, p.btc_len)
    exp4 = (now_ms // H4) * H4 - H4
    btc_now = btc_up.get(exp4 - H4)

    def fetch(job):
        (base, sym, rank, qv), tf = job
        lim = 500 if tf == "4h" else 150
        try:
            rows = closed_only(src.klines(sym, tf, lim), now_ms, tf)
        except FetchError:
            return job, None
        time.sleep(0.03)
        return job, rows

    jobs = [(u, tf) for tf in due for u in pool]
    with ThreadPoolExecutor(max_workers=max(1, p.workers)) as ex:
        results = list(ex.map(fetch, jobs))

    imp = {}                    # base -> {tf: event} + мета
    buys, blocked, nears = [], [], []
    failed, scanned = 0, set()
    rows4_cache = {}
    for ((base, sym, rank, qv), tf), rows in results:
        if rows is None:
            failed += 1
            continue
        L = TF_MS[tf]
        exp = (now_ms // L) * L - L
        if len(rows) < 60 or rows[-1][0] < exp - L:      # мало історії / завислий / делістнутий
            continue
        report_from = exp - (lookback - 1) * L
        scanned.add(base)
        if tf == "4h":
            rows4_cache[base] = rows
        # 1) різкі імпульси
        for ev in detect_impulses(rows, p, tf):
            if rows[ev["i"]][0] < report_from:
                continue
            d = imp.setdefault(base, dict(base=base, sym=sym, rank=rank, tfs={}))
            d["tfs"][tf] = dict(ev, bar=rows[ev["i"]][0], bars_ago=(len(rows) - 1) - ev["i"])
        # 2) сигнали індикатора на 4h
        if tf == "4h" and do_signals:
            res = analyze(rows, p, btc_up)
            if not res or (res["atr_pct"] or 0) < p.min_atr_pct:
                continue
            for ev in res["events"]:
                if rows[ev["i"]][0] < report_from:
                    continue
                ev = dict(ev, base=base, sym=sym, qv=qv, bar=rows[ev["i"]][0])
                if ev["kind"] == "BUY":
                    buys.append(ev)
                elif ev["kind"] == "BLOCKED":
                    blocked.append(ev)
            if res["near"] and p.show_near:
                nears.append(dict(base=base, sym=sym, gap=res["near"]["gap"], gap3=res["near"]["gap3"], rvol=res["rvol_last"]))

    # відсікаємо стейблкоїни/мертві пари за ATR та беремо контекст 4h для знайдених імпульсів
    hits = []
    for base, d in imp.items():
        rows4 = rows4_cache.get(base)
        if rows4 is None:
            try:
                rows4 = closed_only(src.klines(d["sym"], "4h", 500), now_ms, "4h")
            except FetchError:
                rows4 = None
        ctx = None
        if rows4 and len(rows4) >= 120:
            a4 = atr(rows4, p.atr_len)
            c4 = [r[4] for r in rows4]
            f4, s4 = ema(c4, p.fast), ema(c4, p.slow)
            hf = htf_flags(rows4, p.htf_len)
            ctx = dict(atr4=a4[-1], atr4_pct=(a4[-1] / c4[-1] * 100 if a4[-1] else None),
                       trend4h=f4[-1] > s4[-1], htf=hf[-1], dist50=(c4[-1] / s4[-1] - 1) * 100)
        if ctx and (ctx["atr4_pct"] or 0) < p.min_atr_pct:
            continue
        d["ctx"] = ctx
        d["score"] = max(e["gain"] / (p.imp_g3 if e["k"] == 3 else p.imp_g6) for e in d["tfs"].values())
        hits.append(d)
    hits.sort(key=lambda d: (-len(d["tfs"]), -d["score"]))
    buys.sort(key=lambda e: -e["rvol"])
    nears.sort(key=lambda e: e["gap"])
    hb = False
    if p.heartbeat and "4h" in due and not all_tfs:
        hb = datetime.fromtimestamp(exp4 / 1000, tz=timezone.utc).hour == 20   # свічка 20:00 закрилась о 00:00 UTC
    return dict(src=src, hits=hits[:p.imp_top], hits_total=len(hits), buys=buys, blocked=blocked, nears=nears[:p.near_max],
                scanned=len(scanned), pool=len(pool), failed=failed, btc_now=btc_now, now=now_ms, due=due,
                ranked_by=ranked_by, heartbeat=hb, errs=errs, lookback=lookback)


def build_message(p, r):
    L = []
    tv = p.tv_ex or r["src"].tv
    bn = {True: "BTC ↑", False: "BTC ↓", None: "BTC ?"}[r["btc_now"]]
    L.append(f"📊 Spot Alt Planner · {local_time(r['now'], p.tz_name)} · перевірено ТФ: {', '.join(r['due'])} · {bn}")
    iv = {"30m": "30", "1h": "60", "4h": "240"}
    if r["hits"]:
        L.append("")
        L.append(f"🚀 РІЗКІ ІМПУЛЬСИ ВГОРУ (топ-{p.top_n} за капіталізацією) — {r['hits_total']} шт., показано {len(r['hits'])}:")
        for d in r["hits"]:
            ctx = d.get("ctx") or {}
            tfs = sorted(d["tfs"], key=lambda t: TF_MS[t])
            big = d["tfs"][tfs[-1]]                       # метрики старшого ТФ — для рівнів і плану
            rank = f" · #{d['rank']}" if d.get("rank") else ""
            L.append("")
            L.append(f"{d['base']}/USDT{rank} · ТФ: {' + '.join(tfs)}")
            for t in tfs:
                e = d["tfs"][t]
                age = f" ({e['bars_ago']} св. тому)" if e["bars_ago"] else ""
                v = "V✓" if e["rvol"] >= p.imp_rvol_flag else "V слабкий"
                L.append(f"   {t}: +{e['pct']:.1f}% за {e['k']} св. · {e['gain']:.1f} ATR · ER {e['er']:.2f} · RVOL {e['rvol']:.1f} {v}{age}")
            lv50 = big["top"] - 0.5 * (big["top"] - big["origin"])
            L.append(f"   Ціна {fmt_price(big['close'])} · початок руху {fmt_price(big['origin'])} · максимум {fmt_price(big['top'])}")
            L.append(f"   Відкат до 50% імпульсу = {fmt_price(lv50)} (глибше — імпульс слабшає)")
            if ctx.get("atr4"):
                r_unit = p.sl_mult * ctx["atr4"]
                entry = big["close"]
                stop = entry - r_unit
                if stop > 0:
                    pos, risk = size_calc(p, entry, r_unit)
                    pct = lambda x: (x / entry - 1) * 100
                    L.append(f"   План (ATR 4h): стоп {fmt_price(stop)} ({pct(stop):.1f}%) · TP1 {fmt_price(entry + p.tp1r * r_unit)} (+{pct(entry + p.tp1r * r_unit):.1f}%)"
                             f" · TP2 {fmt_price(entry + p.tp2r * r_unit)} · TP3 {fmt_price(entry + p.tp3r * r_unit)}")
                    L.append(f"   Розмір ≈ {pos:.0f} USDT ({pos / p.deposit * 100:.0f}% деп.), ризик ≈ {risk:.1f} USDT")
            if ctx:
                ht = {True: "1D ↑", False: "1D ↓", None: "1D ?"}[ctx.get("htf")]
                L.append(f"   Контекст: {ht} · 4h {'↑' if ctx['trend4h'] else '↓'} · від EMA50(4h) {ctx['dist50']:+.1f}%")
            tfk = tfs[0]
            L.append(f"   https://www.tradingview.com/chart/?symbol={tv}%3A{d['sym']}&interval={iv[tfk]}")
        L.append("")
        L.append("⚠️ Імпульс уже стався: купувати «на закритті» — це гонитва. Тест на 4h: глибокі відкати (≥50%) частіше закінчувались провалом, ніж гарною точкою входу; у боковику такі пробої давали мінус.")
    if r["buys"]:
        L.append("")
        for e in r["buys"]:
            pos, risk = size_calc(p, e["entry"], e["r"])
            pct = lambda x: (x / e["entry"] - 1) * 100
            vflag = "V✓" if e["vol_strong"] else "V слабкий"
            L.append(f"🟢 BUY (4h, перетин 21/50) {e['base']}/USDT  ·  вхід ≈ {fmt_price(e['entry'])}")
            L.append(f"   Стоп {fmt_price(e['stop'])} ({pct(e['stop']):.1f}%) · stop-limit ліміт {fmt_price(e['stop'] * (1 - 0.005))}")
            L.append(f"   TP1 {fmt_price(e['tp1'])} (+{pct(e['tp1']):.1f}%) · TP2 {fmt_price(e['tp2'])} (+{pct(e['tp2']):.1f}%) · TP3 {fmt_price(e['tp3'])} (+{pct(e['tp3']):.1f}%)")
            L.append(f"   Розмір ≈ {pos:.0f} USDT ({pos / p.deposit * 100:.0f}% деп.), ризик ≈ {risk:.1f} USDT · RVOL {e['rvol']:.1f} {vflag} · 1D ↑")
            L.append(f"   https://www.tradingview.com/chart/?symbol={tv}%3A{e['sym']}&interval=240")
        L.append("")
        L.append(f"⚠️ Якщо брати всі {len(r['buys'])} BUY: сумарний ризик ≈ {len(r['buys']) * p.risk_pct:.1f}% депозиту. Постав ліміт на одночасні позиції.")
    if p.show_blocked and r["blocked"]:
        names = ", ".join(f"{e['base']} ({e['why']})" for e in r["blocked"][:15])
        more = f" +ще {len(r['blocked']) - 15}" if len(r["blocked"]) > 15 else ""
        L.append("")
        L.append(f"⛔ BUY-сигнал 21/50 є, але фільтр не пустив ({len(r['blocked'])}): {names}{more}")
    if r["nears"]:
        L.append("")
        L.append("👀 Майже сигнал 21/50 (4h; 1D ↑; НЕ тестувались):")
        for e in r["nears"]:
            L.append(f"   {e['base']}: розрив {e['gap']:.2f} ATR (3 свічки тому {e['gap3']:.2f}) · RVOL {e['rvol']:.1f}")
    if not (r["hits"] or r["buys"] or r["blocked"] or r["nears"]):
        L.append("")
        L.append("Різких імпульсів і сигналів зараз немає.")
    L.append("")
    foot = f"Джерело: {r['src'].name} · рейтинг за {r['ranked_by']} · перевірено {r['scanned']}/{r['pool']}"
    if r["failed"]:
        foot += f" · помилок завантаження: {r['failed']}"
    L.append(foot)
    L.append("Це не фінансова порада.")
    return "\n".join(L)


# ═══════════════════════════════ КОМАНДИ ═══════════════════════════════
def cmd_selftest(p):
    print("— Перевірка джерел даних (з цього комп'ютера/сервера):")
    ok_any = False
    for name in ("binance", "bybit", "mexc"):
        src = SOURCES[name]()
        try:
            uni = src.universe()
            k = src.klines("BTCUSDT", "30m", 50)
            print(f"  ✓ {name}: пар USDT {len(uni)}, свічок BTC(30m) {len(k)}, хост {src.hosts[src._h]}")
            ok_any = True
        except FetchError as ex:
            print(f"  ✗ {name}: {ex}")
    print(f"— Порядок використання: {', '.join(p.sources)} (береться перше робоче)")
    try:
        top = fetch_mcap_top(p, 50)
        print(f"  ✓ CoinGecko: отримано {len(top)} монет, №1 = {top[0][1]}")
    except FetchError as ex:
        print(f"  ✗ CoinGecko: {ex} (сканер перейде на рейтинг за обігом біржі)")
    if p.token:
        try:
            d = http_json(f"https://api.telegram.org/bot{p.token}/getMe")
            print(f"  ✓ Telegram-бот: @{d['result'].get('username')}")
            if p.chat_id:
                tg_send(p, "✅ Spot Alt Planner: сканер підключено. Джерела: " + ", ".join(p.sources))
                print("  ✓ тестове повідомлення надіслано")
            else:
                print("  ! TELEGRAM_CHAT_ID не задано (запусти: python scanner.py --chat-id)")
        except FetchError as ex:
            print(f"  ✗ Telegram: {ex}")
    else:
        print("  ! TELEGRAM_TOKEN не задано")
    return 0 if ok_any else 1


def cmd_chat_id(p):
    if not p.token:
        print("Задай TELEGRAM_TOKEN і спершу напиши своєму боту будь-яке повідомлення (/start).")
        return 1
    d = http_json(f"https://api.telegram.org/bot{p.token}/getUpdates")
    seen = {}
    for u in d.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        ch = m.get("chat")
        if ch:
            seen[ch["id"]] = ch.get("username") or ch.get("title") or ch.get("first_name")
    if not seen:
        print("Оновлень немає: відкрий свого бота в Telegram, натисни Start / напиши йому щось і запусти ще раз.")
        return 1
    for cid, nm in seen.items():
        print(f"chat id: {cid}   ({nm})")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--chat-id", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="лише показати в консолі")
    ap.add_argument("--all-tfs", action="store_true", help="перевірити всі ТФ незалежно від розкладу")
    ap.add_argument("--lookback", type=int, default=0, help="скільки останніх свічок враховувати (за замовч. 1)")
    a = ap.parse_args(argv)
    p = P()
    if a.selftest:
        return cmd_selftest(p)
    if a.chat_id:
        return cmd_chat_id(p)

    r = scan(p, all_tfs=a.all_tfs, lookback=a.lookback or None)
    if r.get("error"):
        msg = "⚠️ Spot Alt Planner: жодне джерело даних недоступне.\n" + "\n".join(r["errs"][:6])
        print(msg)
        if not a.dry_run and p.token and p.chat_id:
            try:
                tg_send(p, msg)
            except FetchError:
                pass
        return 1
    if not r["due"]:
        print("Жоден ТФ зараз не потребує перевірки.")
        return 0
    content = r["hits"] or r["buys"] or r["blocked"] or r["nears"]
    if not (content or r["heartbeat"] or a.dry_run):
        print(f"Нічого нового (перевірено {r['scanned']}/{r['pool']}, ТФ {', '.join(r['due'])}) — повідомлення не надсилаю.")
        return 0
    text = build_message(p, r)
    print(text)
    if a.dry_run:
        return 0
    try:
        tg_send(p, text)
    except FetchError as ex:
        print("Помилка Telegram:", ex)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
