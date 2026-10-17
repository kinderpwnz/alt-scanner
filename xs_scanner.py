#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ДНО / ХАЙ v4 → TELEGRAM  (точки купівлі x4 / x6 на 30m · 1h · 4h · 1D, топ-400 за капіталізацією)

Це та сама логіка, що в TradingView-індикаторі «Spot Alt — Дно/Хай v4» (spot_alt_extremes_v4.pine), тільки для всіх монет одразу:
  рівень = кількість незалежних підтверджень розвороту на ЗАКРИТІЙ свічці:
    1) sweep   — збито мінімум (максимум) останніх ~24 свічок і закрито назад
    2) суворий молот / падаюча зірка (діапазон ≥ 1 ATR, тінь ≥ 50%)
    3) велике поглинання (тіло ≥ 1 ATR)
    4) дивергенція RSI
    5) RSI старшого ТФ (30m/1h → 4h, 4h → 1D, 1D → 1W)
  + зона (біля мін./макс. останніх 60 свічок, або sweep, опційно фібо-відкат)
  купівля: 1 підтвердження = x2, 2 = x4, 3+ = x6. Вихід (за бажанням, XS_SELL=1): на 1 більше (s2=2, s4=3, s6=4).

Працює ПОРУЧ зі scanner.py (імпортує з нього джерела даних, рейтинг, Telegram), сам scanner.py не змінюється.
Запуск:
  python xs_scanner.py --dry-run --all-tfs --lookback 6     показати сигнали за останні 6 свічок (нічого не надсилає)
  python xs_scanner.py                                       звичайний запуск (надсилає лише НОВІ сигнали; стан у .xs_state)
Лише СПОТ, лише ЛОНГ. Це інструмент пошуку зон, а не гарантія прибутку. Не фінансова порада.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import scanner as S  # noqa: E402  (джерела даних, CoinGecko, Telegram, калькулятор)

H4, DAY = S.H4, S.DAY
XS_TF_MS = {"30m": 1800 * 1000, "1h": 3600 * 1000, "4h": H4, "1d": DAY}
XS_ORDER = {"30m": 0, "1h": 1, "4h": 2, "1d": 3}
TV_IV = {"30m": "30", "1h": "60", "4h": "240", "1d": "D"}
HTF_NAME = {"30m": "4h", "1h": "4h", "4h": "1d", "1d": "1w"}
LIM = {"30m": 500, "1h": 500, "4h": 500, "1d": 400}
AUTO_COOL = {"30m": 24, "1h": 16, "4h": 8, "1d": 5}
AUTO_LIFE = {"30m": 200, "1h": 150, "4h": 90, "1d": 60}   # скільки свічок діє зона

# біржі: додаємо денний інтервал до джерел зі scanner.py (сам scanner.py не чіпаємо)
S.Binance.intervals["1d"] = "1d"
S.Mexc.intervals["1d"] = "1d"
S.Bybit.intervals["1d"] = "D"


# ═══════════════════════════════ CONFIG ═══════════════════════════════
class XP(S.P):
    def __init__(self, env=None):
        super().__init__(env)
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

        self.xs_on = f("XS", 1) != 0
        self.xs_tfs = [x.strip().lower() for x in s("XS_TFS", "30m,1h,4h,1d").split(",") if x.strip().lower() in XS_TF_MS]
        # мін. рівень купівлі (2 / 4 / 6) на кожному ТФ. За замовчуванням x4 і x6 на всіх ТФ (30m і 1h — головні).
        # Якщо повідомлень забагато — XS_MIN_30M=6 / XS_MIN_1H=6 або XS_MIN=6 для всіх ТФ.
        dflt = {"30m": 4, "1h": 4, "4h": 4, "1d": 4}
        glob = e("XS_MIN")
        self.xs_min = {tf: int(f("XS_MIN_" + tf.upper(), int(f("XS_MIN", dflt[tf])) if glob not in (None, "") else dflt[tf]))
                       for tf in XS_TF_MS}
        self.xs_sell = f("XS_SELL", 0) != 0                    # 1 = надсилати ще й виходи s
        self.xs_sell_min = int(f("XS_SELL_MIN", 6))
        self.xs_max = int(f("XS_MAX", 12))                     # макс. сигналів у одному повідомленні (решта — лічильником)
        self.xs_lookback = max(1, int(f("XS_LOOKBACK", 2)))    # скільки останніх закритих свічок враховуємо (запізнення запуску)
        self.xs_window = f("XS_WINDOW_MIN", 70)                # ТФ перевіряється, якщо його остання свічка закрилась < N хв тому
        self.xs_state_dir = s("XS_STATE_DIR", os.path.join(HERE, ".xs_state"))
        # рівні (кількість підтверджень) і умови — як в індикаторі
        self.lv2, self.lv4, self.lv6 = int(f("XS_LV2", 1)), int(f("XS_LV4", 2)), int(f("XS_LV6", 3))
        self.sell_extra = int(f("XS_SELL_EXTRA", 1))
        self.need_ext = f("XS_NEED_EXT", 0) != 0
        self.zone_sw = f("XS_ZONE_SWEEP", 1) != 0
        self.zone_fib = f("XS_ZONE_FIB", 0) != 0
        self.gate_n = int(f("XS_GATE_N", 60))
        self.gate_atr = f("XS_GATE_ATR", 1.5)
        self.sw_n = int(f("XS_SWEEP_N", 24))
        self.hm_rng = f("XS_HAMMER_RNG", 1.0)
        self.hm_wick = f("XS_HAMMER_WICK", 0.5)
        self.eng_body = f("XS_ENGULF_BODY", 1.0)
        self.htf_os = f("XS_HTF_OS", 35.0)
        self.htf_ob = f("XS_HTF_OB", 65.0)
        self.cool_over = int(f("XS_COOLDOWN", 0))              # 0 = авто за ТФ
        self.stop_atr = f("XS_STOP_ATR", 0.5)                  # орієнтир стопу: мінімум свічки − N ATR
        # зони купівлі / продажу (злиття повторів + інвалідація)
        # у старих версіях scanner.py цього параметра немає — задаємо тут (мін. відстань стопу від входу, % ціни)
        self.min_stop_pct = f("MIN_STOP_PCT", getattr(self, "min_stop_pct", 1.0))
        self.zones_on = f("XS_ZONES", 1) != 0
        self.z_below = f("XS_ZONE_BELOW_ATR", 1.0)             # низ зони купівлі = мінімум свічки − N ATR (верх зони продажу = максимум + N ATR)
        self.z_tol = f("XS_ZONE_TOL_ATR", 1.0)                 # новий сигнал ближче ніж N ATR до зони = та сама зона (зливається)
        self.z_maxh = f("XS_ZONE_MAX_ATR", 2.0)                # верх зони купівлі не вище мінімуму + N ATR (низ зони продажу не нижче максимуму − N ATR)
        self.z_life = int(f("XS_ZONE_LIFE", 0))                # скільки свічок діє зона (0 = авто за ТФ)


# ═══════════════════════════════ ІНДИКАТОРНІ ФУНКЦІЇ (дзеркало Pine) ═══════════════════════════════
def rsi_series(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    g /= n
    l /= n
    out[n] = 100 - 100 / (1 + g / l) if l > 0 else 100
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 - 100 / (1 + g / l) if l > 0 else 100
    return out


def sma(v, n):
    out = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def stdev(v, n):
    out = [None] * len(v)
    for i in range(n - 1, len(v)):
        w = v[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return out


def week_key(t):
    return (t // DAY + 3) // 7          # тиждень з понеділка 00:00 UTC


def htf_rsi_map(rows, key_fn, n=14):
    """RSI старшого ТФ, відомий на момент відкриття кожної свічки: беремо останню ПОВНІСТЮ ЗАКРИТУ свічку старшого ТФ
    (старший ТФ збирається з тих самих свічок). Це відповідає request.security(..., rsi[1], lookahead_on) в Pine."""
    bars, cur, key = [], None, None
    for r in rows:
        k = key_fn(r[0])
        if k != key:
            if cur:
                bars.append(cur)
            cur = [k, r[4]]
            key = k
        else:
            cur[1] = r[4]
    rs = rsi_series([b[1] for b in bars], n)
    by_key = {b[0]: rs[j] for j, b in enumerate(bars)}
    return [by_key.get(key_fn(r[0]) - 1) for r in rows]


def htf_key_for(tf):
    return {"30m": lambda t: t // H4, "1h": lambda t: t // H4, "4h": lambda t: t // DAY, "1d": week_key}[tf]


def xs_events(rows, tf, p, start=70, zones_out=None):
    """Усі мітки (кластерна логіка як в Pine) на закритих свічках rows. Повертає список подій:
    dict(i, side 'b'/'s', lvl 2/4/6, cnt, ev=[...], zone=[...], ...). Події рахуються від індексу start."""
    n = len(rows)
    o = [r[1] for r in rows]
    h = [r[2] for r in rows]
    l = [r[3] for r in rows]
    c = [r[4] for r in rows]
    v = [r[5] for r in rows]
    if n < 120:
        return []
    a = S.atr(rows, 14)
    ema50 = S.ema(c, 50)
    rsi = rsi_series(c, 14)
    bbm, bbs = sma(c, 20), stdev(c, 20)
    vsma = sma(v, 20)
    hr = htf_rsi_map(rows, htf_key_for(tf))
    cool = p.cool_over if p.cool_over > 0 else AUTO_COOL[tf]
    N, GA = p.gate_n, p.gate_atr

    def lvl(cnt, extra):
        return 6 if cnt >= p.lv6 + extra else 4 if cnt >= p.lv4 + extra else 2 if cnt >= p.lv2 + extra else 0

    out = []
    last = {"b": (-10**9, 0), "s": (-10**9, 0)}
    zone = {"b": None, "s": None}
    life = p.z_life if p.z_life > 0 else AUTO_LIFE[tf]
    for i in range(max(start, 70), n):
        if not a[i] or rsi[i] is None or bbs[i] is None or i < 60:
            continue
        rng = h[i] - l[i]
        lw = min(o[i], c[i]) - l[i]
        uw = h[i] - max(o[i], c[i])
        big = rng >= p.hm_rng * a[i]
        ham = big and lw >= p.hm_wick * rng and c[i] >= l[i] + 0.5 * rng
        star = big and uw >= p.hm_wick * rng and c[i] <= h[i] - 0.5 * rng
        engB = c[i] > o[i] and c[i - 1] < o[i - 1] and c[i] >= o[i - 1] and o[i] <= c[i - 1] and (c[i] - o[i]) >= p.eng_body * a[i]
        engS = c[i] < o[i] and c[i - 1] > o[i - 1] and c[i] <= o[i - 1] and o[i] >= c[i - 1] and (o[i] - c[i]) >= p.eng_body * a[i]
        swLo = min(l[i - p.sw_n - 1:i - 1])           # бари i-2 … i-(sw_n+1)
        swHi = max(h[i - p.sw_n - 1:i - 1])
        swpB = rng > 0 and l[i] < swLo and c[i] > swLo and c[i] >= l[i] + 0.5 * rng and c[i] > o[i]
        swpS = rng > 0 and h[i] > swHi and c[i] < swHi and c[i] <= h[i] - 0.5 * rng and c[i] < o[i]
        # дивергенція (вікно 5…45 свічок тому)
        w_lo = min(range(i - 45, i - 4), key=lambda m: l[m])
        w_hi = max(range(i - 45, i - 4), key=lambda m: h[m])
        divB = rsi[w_lo] is not None and l[i] <= l[w_lo] and rsi[i] > rsi[w_lo] + 2 and rsi[i] < 50
        divS = rsi[w_hi] is not None and h[i] >= h[w_hi] and rsi[i] < rsi[w_hi] - 2 and rsi[i] > 50
        htfB = hr[i] is not None and hr[i] <= p.htf_os
        htfS = hr[i] is not None and hr[i] >= p.htf_ob
        # розтягнутість (довідково або обов'язково)
        r3 = [x for x in rsi[i - 2:i + 1] if x is not None]
        rv = 0.0
        for m in (i - 1, i):
            if vsma[m - 1]:
                rv = max(rv, v[m] / vsma[m - 1])
        extB = (min(r3) <= 30 or any(l[m] < bbm[m] - 2 * bbs[m] for m in (i - 1, i)) or (ema50[i] - c[i]) / a[i] >= 3.0 or rv >= 2.0)
        extS = (max(r3) >= 70 or any(h[m] > bbm[m] + 2 * bbs[m] for m in (i - 1, i)) or (c[i] - ema50[i]) / a[i] >= 3.0 or rv >= 2.0)
        gB = (l[i] - min(l[i - N + 1:i + 1])) <= GA * a[i]
        gS = (max(h[i - N + 1:i + 1]) - h[i]) <= GA * a[i]
        fibB = fibS = False
        if p.zone_fib:
            fibB, fibS = _fib_zone(h, l, a, i)
        for side, sw, hm, eg, dv, ht, ex, g, fz in (
                ("b", swpB, ham, engB, divB, htfB, extB, gB, fibB),
                ("s", swpS, star, engS, divS, htfS, extS, gS, fibS)):
            buy = side == "b"
            z = zone[side]
            if z is not None and p.zones_on:            # 1) зона гасне: закриття за її межею або вичерпано строк
                if (c[i] < z["bot"] if buy else c[i] > z["top"]) or i - z["born"] > life:
                    z["end"] = i
                    z["why"] = "інвалідація" if (c[i] < z["bot"] if buy else c[i] > z["top"]) else "строк"
                    if zones_out is not None:
                        zones_out.append(z)
                    zone[side] = z = None
            trig = int(sw) + int(hm) + int(eg) + int(dv)
            zn_ok = g or (p.zone_sw and sw) or fz
            if not (trig > 0 and zn_ok and (not p.need_ext or ex)):
                continue
            cnt = trig + int(ht)
            L = lvl(cnt, 0 if buy else p.sell_extra)
            if not L:
                continue
            names = [nm for nm, f_ in (("sweep", sw), ("ham", hm), ("eng", eg), ("div", dv), ("htf", ht)) if f_]
            zn = [nm for nm, f_ in (("gate", g), ("sweep", p.zone_sw and sw), ("fib", fz)) if f_]
            li, ll = last[side]
            if not p.zones_on:                           # без зон: стара кластерна логіка (пауза між мітками, апгрейд рівня)
                if i - li > cool or L > ll:
                    out.append(dict(i=i, side=side, lvl=L, cnt=cnt, ev=names, zone=zn, prev=(ll if i - li <= cool else 0),
                                    prev_bars=(i - li if i - li <= cool else None), htf_rsi=hr[i], atr=a[i], ext=bool(ex), kind="new"))
                    last[side] = (i, L)
                continue
            # 2) сигнал у вже існуючій зоні — зливаємо (мітка лише при посиленні рівня)
            if z is not None and (l[i] <= z["top"] + p.z_tol * a[i] if buy else h[i] >= z["bot"] - p.z_tol * a[i]):
                if buy:
                    z["bot"] = min(z["bot"], l[i] - p.z_below * a[i])
                else:
                    z["top"] = max(z["top"], h[i] + p.z_below * a[i])
                if L > z["lvl"]:
                    prev = z["lvl"]
                    z["lvl"] = L
                    last[side] = (i, L)
                    out.append(dict(i=i, side=side, lvl=L, cnt=cnt, ev=names, zone=zn, prev=prev, prev_bars=i - z["born"], htf_rsi=hr[i],
                                    atr=a[i], ext=bool(ex), kind="up", zbot=z["bot"], ztop=z["top"], zborn=z["born"]))
                continue
            # 3) нова зона (з паузою між новими зонами; вищий рівень проходить одразу)
            if i - li > cool or L > ll:
                if buy:
                    bot = l[i] - p.z_below * a[i]
                    top = min(max(o[i], c[i]), l[i] + p.z_maxh * a[i])
                else:
                    top = h[i] + p.z_below * a[i]
                    bot = max(min(o[i], c[i]), h[i] - p.z_maxh * a[i])
                zone[side] = dict(side=side, born=i, bot=bot, top=top, lvl=L, end=None, why=None)
                last[side] = (i, L)
                out.append(dict(i=i, side=side, lvl=L, cnt=cnt, ev=names, zone=zn, prev=0, prev_bars=None, htf_rsi=hr[i], atr=a[i],
                                ext=bool(ex), kind="new", zbot=bot, ztop=top, zborn=i))
    if zones_out is not None:
        zones_out.extend(z for z in zone.values() if z is not None)
    return out


def _fib_zone(h, l, a, i):
    """Фібо-відкат 0.382–0.786 останнього руху (низ → максимум після нього; дзеркально для хая), рух ≥ 3 ATR, пивот не старший за 100 свічок."""
    fb = fs = False
    lo_p = hi_p = None
    for q in range(i - 5, max(i - 101, 5), -1):
        if lo_p is None and l[q] < min(l[q - 5:q]) and l[q] <= min(l[q + 1:q + 6]):
            lo_p = q
        if hi_p is None and h[q] > max(h[q - 5:q]) and h[q] >= max(h[q + 1:q + 6]):
            hi_p = q
        if lo_p is not None and hi_p is not None:
            break
    if lo_p is not None:
        top = max(h[lo_p:i + 1])
        leg = top - l[lo_p]
        if leg >= 3 * a[i] and leg > 0:
            r = (top - l[i]) / leg
            fb = 0.382 <= r <= 0.786
    if hi_p is not None:
        bot = min(l[hi_p:i + 1])
        leg = h[hi_p] - bot
        if leg >= 3 * a[i] and leg > 0:
            r = (h[i] - bot) / leg
            fs = 0.382 <= r <= 0.786
    return fb, fs


# ═══════════════════════════════ СТАН (щоб не повторювати повідомлення) ═══════════════════════════════
def load_state(p):
    path = os.path.join(p.xs_state_dir, "seen.json")
    try:
        d = json.load(open(path, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(p, seen, now_ms):
    try:
        os.makedirs(p.xs_state_dir, exist_ok=True)
        keep = {k: t for k, t in seen.items() if now_ms - t < 4 * DAY}
        json.dump(keep, open(os.path.join(p.xs_state_dir, "seen.json"), "w", encoding="utf-8"))
    except Exception as ex:  # стан — лише захист від повторів, не критично
        print("  ! не вдалося зберегти стан:", ex)


# ═══════════════════════════════ СКАН ═══════════════════════════════
def tf_due(now_ms, tf, window_min):
    L = XS_TF_MS[tf]
    return (now_ms - (now_ms // L) * L) / 60000.0 < window_min


def closed_only(rows, now_ms, tf):
    L = XS_TF_MS[tf]
    return [r for r in rows if r[0] + L <= now_ms]


def scan_xs(p, now_ms=None, all_tfs=False, lookback=None, src=None, uni=None, mcap=None, log=print):
    now_ms = now_ms or int(time.time() * 1000)
    lookback = lookback or p.xs_lookback
    errs = []
    if src is None:
        src, uni, _btc, errs = S.pick_source(p, log)
        if src is None:
            return dict(error="; ".join(errs) or "жодного джерела", errs=errs)
    ranked_by = "капіталізацією (CoinGecko)"
    if mcap is None:
        try:
            mcap = S.fetch_mcap_top(p, p.top_n)
        except S.FetchError as ex:
            log(f"  ✗ CoinGecko: {ex} → беру топ за обігом на біржі")
            errs.append(f"coingecko: {ex}")
            mcap, ranked_by = None, "обігом на біржі (CoinGecko недоступний)"
    pool = S.build_pool(p, uni, mcap)
    due = [tf for tf in p.xs_tfs if all_tfs or tf_due(now_ms, tf, p.xs_window)]

    def fetch(job):
        (base, sym, rank, qv), tf = job
        try:
            rows = closed_only(src.klines(sym, tf, LIM[tf]), now_ms, tf)
        except S.FetchError:
            return job, None
        time.sleep(0.03)
        return job, rows

    jobs = [(u, tf) for tf in due for u in pool]
    with ThreadPoolExecutor(max_workers=max(1, p.workers)) as ex:
        results = list(ex.map(fetch, jobs))

    sigs, failed, scanned = [], 0, set()
    for ((base, sym, rank, qv), tf), rows in results:
        if rows is None:
            failed += 1
            continue
        L = XS_TF_MS[tf]
        exp = (now_ms // L) * L - L                      # відкриття останньої закритої свічки
        if len(rows) < 120 or rows[-1][0] < exp - L:     # мало історії / завислий / делістнутий
            continue
        scanned.add(base)
        n = len(rows)
        a14 = S.atr(rows, 14)[-1]
        if not a14 or a14 / rows[-1][4] * 100 < p.min_atr_pct * math.sqrt(L / H4):   # стейблкоїни / мертві пари
            continue
        first = n - lookback
        life = p.z_life if p.z_life > 0 else AUTO_LIFE[tf]
        evs = xs_events(rows, tf, p, start=max(70, n - lookback - life - 2 * AUTO_COOL[tf] - 10))
        for e in evs:
            if e["i"] < first:
                continue
            need = p.xs_min[tf] if e["side"] == "b" else p.xs_sell_min
            if e["side"] == "s" and not p.xs_sell:
                continue
            if e["lvl"] < need:
                continue
            r = rows[e["i"]]
            sigs.append(dict(e, base=base, sym=sym, rank=rank, tf=tf, bar=r[0], open=r[1], high=r[2], low=r[3], close=r[4],
                             bars_ago=(n - 1) - e["i"], last_close=rows[-1][4],
                             key=f"{base}|{tf}|{r[0]}|{e['side']}|{e['lvl']}"))
    sigs.sort(key=lambda s: (-s["lvl"], -s["cnt"], -XS_ORDER[s["tf"]], s["rank"] or 9999))
    return dict(src=src, sigs=sigs, scanned=len(scanned), pool=len(pool), failed=failed, now=now_ms, due=due,
                ranked_by=ranked_by, errs=errs, lookback=lookback)


# ═══════════════════════════════ ПОВІДОМЛЕННЯ ═══════════════════════════════
EV_BUY = {"sweep": "sweep (мінімум збито й повернулась)", "ham": "суворий молот", "eng": "велике поглинання", "div": "дивергенція RSI"}
EV_SELL = {"sweep": "sweep (максимум збито й повернулась)", "ham": "сувора падаюча зірка", "eng": "велике поглинання вниз", "div": "дивергенція RSI"}
ZONE_TXT = {"gate": "біля мін./макс. останніх 60 свічок", "sweep": "після sweep", "fib": "фібо-відкат 0.382–0.786"}


def fmt_sig(p, s, tv):
    buy = s["side"] == "b"
    tag = f"{'x' if buy else 's'}{s['lvl']}"
    icon = "🟢" if buy else "🔴"
    rank = f" · #{s['rank']}" if s.get("rank") else ""
    names = EV_BUY if buy else EV_SELL
    parts = [names[k] for k in s["ev"] if k != "htf"]
    if "htf" in s["ev"]:
        v = s.get("htf_rsi")
        parts.append(f"RSI {HTF_NAME[s['tf']]} {'перепроданий' if buy else 'перекуплений'}" + (f" ({v:.0f})" if v is not None else ""))
    close_ms = s["bar"] + XS_TF_MS[s["tf"]]
    up = s.get("kind") == "up"
    what = ("посилення зони " if up else "нова зона ") + ("купівлі" if buy else "продажу")
    head = f"{icon} {tag} · {s['tf']} · {s['base']}/USDT{rank} · {what} · закриття {S.fmt_price(s['close'])}"
    L = [head]
    if s.get("zbot") is not None:
        bot, top = s["zbot"], s["ztop"]
        ref = s["close"]
        L.append(f"   ЗОНА {'КУПІВЛІ' if buy else 'ПРОДАЖУ'}: {S.fmt_price(bot)} – {S.fmt_price(top)} "
                 f"({(bot / ref - 1) * 100:+.1f}% … {(top / ref - 1) * 100:+.1f}% від закриття) · "
                 + (f"скасовується, якщо свічка {s['tf']} закриється нижче {S.fmt_price(bot)}" if buy
                    else f"скасовується, якщо свічка {s['tf']} закриється вище {S.fmt_price(top)}"))
    L.append(f"   підтверджень {s['cnt']}: " + ", ".join(parts))
    L.append(f"   сигнальна свічка {s['tf']} закрилась {S.local_time(close_ms, p.tz_name)} · біля: {', '.join(ZONE_TXT[z] for z in s['zone'])}")
    if s["bars_ago"]:
        pct = (s["last_close"] / s["close"] - 1) * 100
        L.append(f"   це було {s['bars_ago']} св. тому; ціна зараз {S.fmt_price(s['last_close'])} ({pct:+.1f}% від закриття)")
    if up and s.get("prev"):
        L.append(f"   посилення: зона виникла {s['prev_bars']} св. тому з рівнем {'x' if buy else 's'}{s['prev']}")
    if buy:
        if s.get("zbot") is not None:
            stop = s["zbot"]
        else:
            stop = s["low"] - p.stop_atr * s["atr"]
        r_unit = max(s["close"] - stop, s["close"] * p.min_stop_pct / 100.0)
        stop = s["close"] - r_unit
        if stop > 0:
            pos, risk = S.size_calc(p, s["close"], r_unit)
            L.append(f"   орієнтир стопу = низ зони {S.fmt_price(stop)} ({(stop / s['close'] - 1) * 100:.1f}%) · "
                     f"під ризик {p.risk_pct:g}% депозиту ≈ {pos:.0f} USDT")
    L.append(f"   https://www.tradingview.com/chart/?symbol={tv}%3A{s['sym']}&interval={TV_IV[s['tf']]}")
    return L


def build_message(p, r, sigs):
    tv = p.tv_ex or r["src"].tv
    buys = [s for s in sigs if s["side"] == "b"]
    sells = [s for s in sigs if s["side"] == "s"]
    L = [f"📍 Дно/Хай v4 · {S.local_time(r['now'], p.tz_name)} · ТФ: {', '.join(r['due'])}"]
    shown = 0
    if buys:
        L += ["", f"🟢 ЗОНИ КУПІВЛІ — {len(buys)} шт. (x4 = 2 підтвердження, x6 = 3+; зручно набирати частинами всередині зони, а не «все одразу»):"]
        for s in buys:
            if shown >= p.xs_max:
                break
            L.append("")
            L += fmt_sig(p, s, tv)
            shown += 1
    if sells and shown < p.xs_max:
        L += ["", f"🔴 ЗОНИ ПРОДАЖУ / ВИХОДУ (s) — {len(sells)} шт.:"]
        for s in sells:
            if shown >= p.xs_max:
                break
            L.append("")
            L += fmt_sig(p, s, tv)
            shown += 1
    rest = len(sigs) - shown
    if rest > 0:
        L += ["", f"… ще {rest} слабших сигналів не показано (XS_MAX={p.xs_max}); поки вони свіжі, прийдуть наступним запуском. "
                  f"Щоб їх було менше — постав XS_MIN=6 (лише x6)."]
    L += ["", "⚠️ Це зони виснаження, а не гарантія розвороту. Логіку перевірено лише на 4h (6 монет, ≈24 монето-місяці, переважно трендовий період): "
              "x4+ — ≈69% справжніх мінімумів проти 44% на випадкових свічках (зона купівлі жива через 20 свічок у ≈78%, через 50 — у ≈70%), "
              "x6 — ≈90% (лише 10 випадків); зони продажу надійні гірше (≈54%, жива через 20 свічок ≈58%). 30m, 1h і 1D не перевірялись.",
          f"Джерело: {r['src'].name} · рейтинг за {r['ranked_by']} · перевірено {r['scanned']}/{r['pool']}"
          + (f" · помилок завантаження: {r['failed']}" if r["failed"] else ""),
          "Це не фінансова порада."]
    return "\n".join(L)


# ═══════════════════════════════ ЗАПУСК ═══════════════════════════════
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="лише показати в консолі, стан не пишеться")
    ap.add_argument("--all-tfs", action="store_true", help="перевірити всі ТФ незалежно від розкладу")
    ap.add_argument("--lookback", type=int, default=0, help="скільки останніх закритих свічок враховувати (за замовч. XS_LOOKBACK=2)")
    ap.add_argument("--ignore-state", action="store_true", help="надіслати навіть те, що вже надсилалось")
    a = ap.parse_args(argv)
    p = XP()
    if a.selftest:
        rc = S.cmd_selftest(p)
        try:
            src = S.pick_source(p, lambda *_: None)[0]
            k = src.klines("BTCUSDT", "1d", 30)
            print(f"  ✓ денні свічки BTC ({src.name}): {len(k)}")
        except Exception as ex:
            print("  ✗ денні свічки:", ex)
            rc = 1
        return rc
    if not p.xs_on:
        print("XS=0 — вимкнено.")
        return 0
    r = scan_xs(p, all_tfs=a.all_tfs, lookback=a.lookback or None)
    if r.get("error"):
        print("⚠️ жодне джерело даних недоступне:", r["error"])
        return 1
    if not r["due"]:
        print("Жоден ТФ зараз не потребує перевірки.")
        return 0
    seen = {} if a.ignore_state else load_state(p)
    fresh = [s for s in r["sigs"] if s["key"] not in seen]
    print(f"Перевірено {r['scanned']}/{r['pool']} монет, ТФ {', '.join(r['due'])}: знайдено {len(r['sigs'])}, нових {len(fresh)}")
    if not fresh:
        return 0
    text = build_message(p, r, fresh)
    print(text)
    if a.dry_run:
        return 0
    try:
        S.tg_send(p, text)
    except S.FetchError as ex:
        print("Помилка Telegram:", ex)
        return 1
    now_ms = r["now"]
    for s in fresh[:p.xs_max]:                  # показані — позначаємо; решта має шанс піти наступного запуску, поки свіжі
        seen[s["key"]] = now_ms
    save_state(p, seen, now_ms)
    return 0


if __name__ == "__main__":
    sys.exit(main())
