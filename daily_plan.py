#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Денний план: щодня після закриття дня (UTC) — режим ринку, стан кожної монети зі списку, конкретні рівні, твої позиції.

Запуск:
  python daily_plan.py                      # живі дані (Binance → MEXC → Bybit) → Telegram (якщо є TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)
  python daily_plan.py --dry                # лише показати текст, без Telegram
  python daily_plan.py --csv DIR [--asof YYYY-MM-DD]   # офлайн, з CSV (ts_ms,o,h,l,c,v) — для перевірки / «перемотки»
Файли поруч: plan_config.json (монети, капітал), positions.json (твої позиції).
Правила рішень — у dp_engine.py (перевірені бектестом, див. README_plan.md). Це не фінансова порада.
"""
import argparse, io, json, math, os, sys, time, uuid
import datetime as dt
import urllib.request, urllib.parse, urllib.error

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dp_engine as E
try:
    import ew2
except Exception:                       # хвильовий орієнтир необов'язковий
    ew2 = None

DEFAULT_COINS = "BTC HBAR RUNE AVAX XLM INJ ONDO RAY SUI ICP GRASS ENA TIA TEL RENDER BCH COMP SEI FET THETA NEAR FIL EGLD".split()
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
DAY = 86400000
MIN_BARS = 61                           # як у бектесті: монета торгується після 60 днів історії (бал на молодих монетах занижений)


# ───────────────────────── форматування ─────────────────────────
def fp(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    a = abs(x)
    if a >= 1000: s = f"{x:,.0f}".replace(",", " ")
    elif a >= 100: s = f"{x:.1f}"
    elif a >= 10: s = f"{x:.2f}"
    elif a >= 1: s = f"{x:.3f}"
    elif a >= 0.1: s = f"{x:.4f}"
    elif a >= 0.01: s = f"{x:.5f}"
    elif a >= 0.001: s = f"{x:.6f}"
    else: s = f"{x:.7f}"
    return s


def pc(x, d=1, sign=True):
    return (f"{x * 100:+.{d}f}%" if sign else f"{x * 100:.{d}f}%")


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ───────────────────────── дані ─────────────────────────
def load_csv(dirpath, coins, asof):
    """CSV: ts_ms,o,h,l,c,v. Лише закриті свічки до кінця дня asof (UTC)."""
    cut = None
    if asof:
        d = dt.datetime.strptime(asof, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        cut = int(d.timestamp() * 1000) + DAY            # свічка з ts < cut закрита
    data = {}
    for c in coins:
        p = os.path.join(dirpath, f"{c}_1d.csv")
        if not os.path.exists(p):
            continue
        rows = []
        for ln in open(p):
            f = ln.strip().split(",")
            if len(f) >= 5:
                rows.append((int(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[4]), float(f[5]) if len(f) > 5 else 0.0))
        if cut:
            rows = [r for r in rows if r[0] + DAY <= cut]
        data[c] = rows
    fng = None
    pf = os.path.join(dirpath, "fng.csv")
    if os.path.exists(pf):
        fr = [tuple(map(int, ln.strip().split(",")[:2])) for ln in open(pf) if ln.strip()]
        if cut:
            fr = [r for r in fr if r[0] < cut]
        fng = fr
    return data, fng


def load_live(coins, bars=1100):
    import dump_data as D
    start = int(time.time() * 1000) - bars * DAY
    data, srcs = {}, {}
    for c in coins:
        try:
            rows, src, sym = D.merge_alias(c, "1d", start)
        except Exception as e:
            rows, src = None, f"помилка: {e}"
        if rows:
            data[c] = rows; srcs[c] = src
        else:
            srcs[c] = src or "немає даних"
    fng = None
    try:
        d = D.get_json("https://api.alternative.me/fng/?limit=10&format=json")
        if d and "data" in d:
            fng = sorted((int(x["timestamp"]) * 1000, int(x["value"])) for x in d["data"])
    except Exception:
        pass
    return data, fng, srcs


def align(data, coins):
    """Спільна денна вісь по BTC; решта вирівнюється за ts."""
    btc = data["BTC"]
    ts = [r[0] for r in btc]
    n = len(ts)
    pos = {t: i for i, t in enumerate(ts)}
    A = {}
    for c in coins:
        if c not in data:
            continue
        o = np.full(n, np.nan); h = o.copy(); l = o.copy(); cl = o.copy(); v = o.copy()
        for r in data[c]:
            i = pos.get(r[0])
            if i is not None:
                o[i], h[i], l[i], cl[i], v[i] = r[1], r[2], r[3], r[4], r[5]
        # малі пропуски (≤5 днів) усередині історії монети — заповнюємо попереднім закриттям (технічні перерви біржі)
        fv = np.where(~np.isnan(cl))[0]
        if len(fv):
            i = fv[0]
            while i < n:
                if np.isnan(cl[i]):
                    j = i
                    while j < n and np.isnan(cl[j]): j += 1
                    if j < n and j - i <= 5 and i > 0:
                        for k in range(i, j):
                            o[k] = h[k] = l[k] = cl[k] = cl[i - 1]; v[k] = 0.0
                    i = j
                else:
                    i += 1
        A[c] = dict(o=o, h=h, l=l, c=cl, v=v)
    return ts, A


# ───────────────────────── розрахунок плану ─────────────────────────
def build_plan(ts, A, cfg, positions, fng):
    p = E.P
    btc = A["BTC"]["c"]
    G, sma_b = E.btc_gate(btc)
    g_now, g_up, g_dn = E.btc_gate_levels(btc)
    n = len(ts)
    coins = [c for c in cfg["coins"] if c in A]
    plan = dict(ts=ts, n=n, G=G, g_now=g_now, g_up=g_up, g_dn=g_dn, sma_btc=sma_b[-1], coins={})
    for c in coins:
        a = A[c]
        valid = int(np.sum(~np.isnan(a["c"])))
        cn = E.Coin(a["o"], a["h"], a["l"], a["c"])
        if valid < MIN_BARS or np.isnan(a["c"][-1]):
            plan["coins"][c] = dict(coin=c, status="NODATA", price=a["c"][-1] if valid else None, bars=valid)
            continue
        w, sc, M = E.run_A(cn, G)
        wmr, mrs = E.run_MR(cn, G, with_state=True)
        trg = E.triggers_A(cn, M, G[-1])
        sw = E.swing_info(cn)
        px = a["c"][-1]
        d_in = 0
        if w[-1] > 0:
            k = n - 1
            while k >= 0 and w[k] > 0:
                k -= 1
            d_in = n - 1 - k
        # що змінилось на останньому закритті
        if w[-1] > 0 and w[-2] <= 0: status = "BUY"
        elif w[-1] > 0: status = "HOLD"
        elif w[-2] > 0: status = "EXIT"
        else: status = "BLOCKED" if G[-1] <= 0 else "WAIT"
        exit_reason = None
        if status == "EXIT":
            if G[-1] <= 0 and G[-2] > 0: exit_reason = "режим BTC вимкнувся"
            elif M.blocked: exit_reason = "стоп 2×ATR від максимуму"
            elif M.score <= p["lo"]: exit_reason = f"тренд зламано (бал {int(round(M.score * 8))}/8)"
            else: exit_reason = "режим BTC вимкнений"
        mr = None; mr_reason = None
        if w[-1] <= 0:                           # поки тренд-позиція відкрита, відскок нею перекритий (вага = max(A, 0.5·відскок))
            if wmr[-1] > 0 and mrs["sig"][-1] > 0: mr = "BUY"
            elif wmr[-1] > 0: mr = "HOLD"
            elif wmr[-2] > 0:
                mr = "EXIT"
                pxl = a["c"][-1]
                mr_reason = "закриття нижче стопу" if pxl < mrs["stop"] else ("RSI досяг 50" if cn.rsi[-1] >= p["mr_rsi_out"] else "минуло 10 днів")
        score8 = int(round(M.score * 8)) if not np.isnan(M.score) else None
        after_stop = bool((not M.inpos) and M.blocked)
        rsi = cn.rsi[-1]; atr = cn.atr[-1]
        sm50 = cn.sma[50][-1]; sm100 = cn.sma[100][-1]
        # хвильовий орієнтир (за потреби)
        wave = None
        if ew2 is not None and status in ("BUY", "EXIT", "WAIT", "HOLD"):
            try:
                rows = [[ts[i], a["o"][i], a["h"][i], a["l"][i], a["c"][i]] for i in range(max(0, n - 700), n) if not np.isnan(a["c"][i])]
                res = ew2.analyse(rows, 3.0, 0.05)
                if res and res.get("plan"):
                    pl = res["plan"]
                    wave = dict(status=pl.get("status", ""), nxt=pl.get("next", ""), inval=pl.get("inval"), inval_below=pl.get("inval_below", True),
                                targets=[(nm, v) for nm, v in pl.get("targets", [])][:3], typ=pl.get("typ"))
            except Exception:
                wave = None
        r30 = px / a["c"][-31] - 1 if n > 31 and not np.isnan(a["c"][-31]) else None
        r90 = px / a["c"][-91] - 1 if n > 91 and not np.isnan(a["c"][-91]) else None
        rs60 = None
        if n > 61 and not np.isnan(a["c"][-61]) and not np.isnan(btc[-61]):
            rs60 = (px / a["c"][-61]) / (btc[-1] / btc[-61]) - 1
        plan["coins"][c] = dict(coin=c, status=status, price=px, w=w, score=score8, models=M.models_now, inpos=bool(w[-1] > 0), days_in=d_in,
                                trig=trg, stop=trg.get("exit") if w[-1] > 0 else None, entry=trg.get("entry") if w[-1] <= 0 else None,
                                exit_reason=exit_reason, after_stop=after_stop, mr=mr, mr_reason=mr_reason, mr_state=mrs, rsi=rsi, atr=atr, atr_pct=atr / px, sm50=sm50, sm100=sm100,
                                swing=sw, wave=wave, r30=r30, r90=r90, rs60=rs60, hiC=M.hiC if w[-1] > 0 else None, bars=valid)
    return plan


# ───────────────────────── текст ─────────────────────────
def slot_usdt(cfg, n_alts):
    return cfg["capital_usdt"] / max(1, n_alts)


def fng_label(v):
    return "Екстремальний страх" if v <= 24 else "Страх" if v <= 44 else "Нейтрально" if v <= 55 else "Жадібність" if v <= 74 else "Екстремальна жадібність"


def fq(q):
    return f"{q:,.0f}".replace(",", " ") if q >= 1000 else f"{q:g}"


def stop_kind(d):
    k = (d.get("trig") or {}).get("exit_kind")
    return "трейлінг 2×ATR від максимуму" if k == "trail" else ("злам тренду" if k == "тренд" else "")


def coin_line_hold(c, d, slot):
    stop = d["stop"]
    dist = (d["price"] - stop) / d["price"] if stop else None
    s = f"• <b>{c}</b> {fp(d['price'])} · бал {d['score']}/8 · {d['days_in']} дн. у системі"
    if stop:
        s += f"\n   стоп (закриття нижче): <b>{fp(stop)}</b> ({pc(-dist, 0)}) · {stop_kind(d)}"
    return s


def build_messages(plan, cfg, positions, fng, srcs=None, prev=None):
    ts = plan["ts"]; n = plan["n"]
    last = dt.datetime.fromtimestamp(ts[-1] / 1000, dt.timezone.utc)
    nxt = last + dt.timedelta(days=1)
    coins = plan["coins"]
    alts = [c for c in coins if c != "BTC"]
    slot = slot_usdt(cfg, len([c for c in cfg["coins"] if c != "BTC"]))
    cap = cfg["capital_usdt"]
    msgs = []

    # ── 1. ринок ──
    btc = coins.get("BTC")
    head = f"📊 <b>Денний план</b> · {WD[nxt.weekday()]} {nxt.strftime('%d.%m.%Y')}\n<i>дані до закриття дня {last.strftime('%d.%m')} (UTC)</i>\n"
    gn = plan["g_now"]
    bpx = btc["price"] if btc else None
    mk = "\n🌍 <b>РИНОК</b>\n"
    if bpx:
        dist = bpx / plan["sma_btc"] - 1
        if gn > 0:
            mk += f"BTC {fp(bpx)} · режим 🟢 <b>УВІМКНЕНО</b> (вище SMA100 на {pc(dist, 1)})\n"
            if plan["g_dn"]: mk += f"вимкнеться при закритті BTC нижче <b>{fp(plan['g_dn'])}</b> ({pc(plan['g_dn'] / bpx - 1, 0)})\n"
        else:
            mk += f"BTC {fp(bpx)} · режим 🔴 <b>ВИМКНЕНО</b> ({pc(dist, 1)} від SMA100)\n"
            if plan["g_up"]: mk += f"увімкнеться при закритті BTC вище <b>{fp(plan['g_up'])}</b> ({pc(plan['g_up'] / bpx - 1, 0)})\n"
            mk += "нові купівлі за системою заборонені (дозволені лише дрібні «відскоки»)\n"
    inpos = [c for c in alts if coins[c].get("inpos")]
    nd = [c for c in alts if coins[c].get("status") != "NODATA"]
    mk += f"Альтів у системі: <b>{len(inpos)}</b> з {len(nd)}"
    used = len(inpos)
    if used:
        risk = 0.0
        for c in inpos:
            d = coins[c]
            if d["stop"]: risk += (d["price"] - d["stop"]) / d["price"] * slot / cap
        mk += f" · зайнято слотів {used}/{len(nd)} (≈{pc(used * slot / cap, 0, False)} капіталу), ризик до стопів ≈{pc(risk, 1, False)} капіталу"
    mk += "\n"
    if fng:
        v = fng[-1][1]
        mk += f"Fear &amp; Greed: {v} ({fng_label(v)}) — лише контекст, на рішення не впливає\n"
    msgs.append(head + mk)

    # ── 2. зміни ──
    buys = [c for c in coins if coins[c].get("status") == "BUY" and c != "BTC"]
    exits = [c for c in coins if coins[c].get("status") == "EXIT" and c != "BTC"]
    ch = ""
    if buys:
        ch += "\n🟢 <b>КУПУВАТИ (новий сигнал)</b>\n"
        for c in buys:
            d = coins[c]; stop = d["stop"]
            dist = (d["price"] - stop) / d["price"] if stop else None
            ch += f"<b>{c}</b> · закриття {fp(d['price'])} · бал {d['score']}/8\n"
            ch += f"   купити 1 слот = <b>{slot:,.0f} USDT</b> на відкритті дня ({nxt.strftime('%d.%m')} 00:00 UTC і далі)\n".replace(",", " ")
            if stop:
                ch += f"   стоп (закриття нижче): <b>{fp(stop)}</b> ({pc(-dist, 0)}, {stop_kind(d)}); ризик ≈ {pc(dist * slot / cap, 2, False)} капіталу\n"
            ch += ctx_line(d)
    if exits:
        ch += "\n🔴 <b>ВИЙТИ (сигнал виходу)</b>\n"
        for c in exits:
            d = coins[c]
            hold = positions.get(c)
            ch += f"<b>{c}</b> · закриття {fp(d['price'])} · причина: {esc(d['exit_reason'])}\n"
            if hold and hold.get("qty"):
                pl = d["price"] / hold["avg"] - 1 if hold.get("avg") else None
                ch += f"   у тебе {fq(hold['qty'])} шт" + (f", середня {fp(hold['avg'])} → {pc(pl, 1)}" if pl is not None else "") + " → продати за системою\n"
    if not buys and not exits:
        ch += "\n<i>Нових сигналів входу/виходу немає.</i>\n"
    mrb = [c for c in alts if coins[c].get("mr") == "BUY"]
    if mrb:
        ch += "\n⚡ <b>ВІДСКІК (дрібно, режим BTC вимкнено)</b> — менш надійний сигнал\n"
        for c in mrb:
            d = coins[c]; st = d["mr_state"]
            ch += f"<b>{c}</b> RSI {d['rsi']:.0f} (&lt;25) · закриття {fp(d['price'])} → 0.5 слота ≈ {slot / 2:,.0f} USDT".replace(",", " ")
            ch += f" · стоп (закриття нижче) {fp(st['stop'])} · вихід: RSI ≥ 50, або через 10 днів\n"
        n_mr = len([c for c in alts if coins[c].get("mr") in ("BUY", "HOLD")])
        ch += f"   Усього відскок-позицій (з відкритими): {n_mr} ≈ {pc(n_mr * 0.5 * slot / cap, 0, False)} капіталу. Під час обвалу сигнали з'являються в багатьох монет одразу — не перевищуй цей бюджет.\n"
    mre = [c for c in alts if coins[c].get("mr") == "EXIT"]
    if mre:
        ch += "\n⚡ <b>ВІДСКІК — вийти</b> (малі позиції, відкриті при вимкненому режимі)\n"
        for c in mre:
            d = coins[c]
            ch += f"<b>{c}</b> · закриття {fp(d['price'])} · причина: {esc(d.get('mr_reason') or '')}\n"
            h = positions.get(c)
            if h and h.get("qty"): ch += f"   у тебе {fq(h['qty'])} шт — якщо це відскок-позиція, закривай\n"
    msgs.append(ch)

    # ── 3. тримаємо ──
    holds = [c for c in alts if coins[c].get("status") == "HOLD"]
    if holds:
        s = "\n✅ <b>СИСТЕМА В ПОЗИЦІЇ</b> — тримати, якщо купував (стоп перераховується щодня, може рухатись в обидва боки; рівень орієнтовний ±3%)\n"
        for c in sorted(holds, key=lambda x: -(coins[x]["days_in"])):
            s += coin_line_hold(c, coins[c], slot) + "\n"
        s += "<i>Це стан системи, а не твого рахунку. Якщо монету ще не купував — вхід уже відбувся, наздоганяти не обов'язково; чекай наступного сигналу.</i>\n"
        mrh = [c for c in alts if coins[c].get("mr") == "HOLD"]
        if mrh:
            s += "\n⚡ відскок-позиції ще відкриті: " + ", ".join(f"{c} (стоп {fp(coins[c]['mr_state']['stop'])})" for c in mrh) + "\n"
        msgs.append(s)
    else:
        mrh = [c for c in alts if coins[c].get("mr") == "HOLD"]
        if mrh:
            msgs.append("\n⚡ відскок-позиції ще відкриті: " + ", ".join(f"{c} (стоп {fp(coins[c]['mr_state']['stop'])})" for c in mrh) + "\n")

    # ── 4. чекаємо ──
    waits = [c for c in alts if coins[c].get("status") in ("WAIT", "BLOCKED")]
    if waits:
        s = "\n⏳ <b>ЧЕКАЄМО</b> — купівля лише після закриття вище рівня\n"
        if plan["g_now"] <= 0:
            s = "\n🧊 <b>ПОЗА РИНКОМ</b> — режим BTC вимкнено; рівні нижче діють, коли режим увімкнеться\n"
        rows = []
        for c in waits:
            d = coins[c]
            if d.get("entry"):
                rows.append((d["entry"] / d["price"] - 1, c))
            else:
                rows.append((9.99, c))
        rows.sort()
        for dist, c in rows[:14]:
            d = coins[c]
            if d.get("entry"):
                s += f"• {c} {fp(d['price'])} · бал {d['score']}/8 · вхід вище <b>{fp(d['entry'])}</b> ({pc(dist, 0)})" + (" · після стопу: лише пробій 20-ден. максимуму" if d.get("after_stop") and d["score"] >= 6 else "") + "\n"
            else:
                s += f"• {c} {fp(d['price'])} · бал {d['score']}/8 · рівень входу далеко (&gt;+300%)\n"
        if len(rows) > 14:
            s += f"… ще {len(rows) - 14}: " + ", ".join(c for _, c in rows[14:]) + "\n"
        msgs.append(s)

    # ── BTC окремо ──
    if btc and btc.get("status") != "NODATA":
        d = btc
        s = f"\n₿ <b>BTC за системою:</b> {'у лонгу' if d['inpos'] else 'поза ринком'}, бал {d['score']}/8"
        if d["stop"]: s += f", стоп {fp(d['stop'])}"
        s += "\n<i>Для самого BTC тест не показав переваги над «купи і тримай» (менша просадка, але й менший прибуток) — BTC тримай за власним рішенням.</i>\n"
        msgs.append(s)

    # ── твої позиції і «фокус» (детально) ──
    held = [c for c in positions if c in coins and coins[c].get("status") != "NODATA"]
    focus = [c for c in cfg.get("focus", []) if c in coins and coins[c].get("status") != "NODATA" and c not in held]
    if held or focus:
        s = "\n💼 <b>ТВОЇ ПОЗИЦІЇ І ФОКУС</b>\n"
        tot_v = tot_c = 0.0
        for c in held + focus:
            d = coins[c]
            s += detail_card(c, d, slot, positions, nxt) + "\n"
            if c in positions:
                h = positions[c]; tot_v += h["qty"] * d["price"]
                if h.get("avg"): tot_c += h["qty"] * h["avg"]
        if held:
            s += f"Разом у позиціях: {tot_v:,.0f} USDT".replace(",", " ") + (f" ({pc(tot_v / tot_c - 1, 1)} до вкладеного)" if tot_c else "") + "\n"
            s += "<i>Позиції поза системою не продаються автоматично: рішення за тобою.</i>\n"
        msgs.append(s)

    nod = [c for c in cfg["coins"] if c not in coins or coins[c].get("status") == "NODATA"]
    foot = ""
    if nod:
        foot += f"\n⚠️ Немає достатньо даних: {', '.join(nod)}\n"
    foot += ("\n<i>Це не фінансова порада. Правила перевірені на історії 2020–2026 і не гарантують прибутку. "
             "Усі рішення — за закритою денною свічкою; стопи в плані — рівні закриття дня (не біржові стоп-ордери), орієнтовні ±3%. "
             "Орієнтири (підтримка/опір, цілі, хвилі) тестом переваги не підтверджено, вони не є сигналами.</i>")
    msgs.append(foot)
    return msgs



def detail_card(c, d, slot, positions, nxt):
    """Детальна картка монети (позиція / «фокус»)."""
    h = positions.get(c)
    lab = {"BUY": "🟢 КУПУВАТИ" if not h else "🟢 система купила", "HOLD": "✅ ТРИМАТИ" if h else "✅ система в позиції (вхід уже був)",
           "EXIT": "🔴 ВИЙТИ" if h else "🔴 система вийшла", "WAIT": "⚠ у тебе є, система поза ринком" if h else "⏳ система поза ринком",
           "BLOCKED": "⚠ у тебе є, режим BTC вимкнено" if h else "🧊 поза ринком (режим BTC вимкнено)"}[d["status"]]
    s = f"<b>{c}</b> {fp(d['price'])} · {lab} · бал {d['score']}/8"
    if d["status"] == "EXIT" and d.get("exit_reason"): s += f" · {esc(d['exit_reason'])}"
    s += "\n"
    if h:
        v = h["qty"] * d["price"]
        s += f"   у тебе: {fq(h['qty'])} шт ≈ {v:,.0f} USDT".replace(",", " ")
        if h.get("avg"): s += f" · {pc(d['price'] / h['avg'] - 1, 1)} від середньої {fp(h['avg'])}"
        s += "\n"
    if d.get("stop"): s += f"   стоп системи (закриття нижче): <b>{fp(d['stop'])}</b> ({pc(d['stop'] / d['price'] - 1, 0)}, {stop_kind(d)})\n"
    if d.get("entry"): s += f"   вхід системи (закриття вище): <b>{fp(d['entry'])}</b> ({pc(d['entry'] / d['price'] - 1, 0)})" + (" · після стопу: лише пробій 20-ден. максимуму" if d.get("after_stop") and d["score"] >= 6 else "") + "\n"
    bits = [f"RSI {d['rsi']:.0f}", f"ATR {pc(d['atr_pct'], 1, False)}", f"від SMA50 {pc(d['price'] / d['sm50'] - 1, 0)}" if d.get("sm50") else None]
    if d.get("r30") is not None: bits.append(f"30д {pc(d['r30'], 0)}")
    if d.get("rs60") is not None: bits.append(f"проти BTC (60д) {pc(d['rs60'], 0)}")
    s += "   " + " · ".join(b for b in bits if b) + "\n"
    s += ctx_line(d)
    return s


def ctx_line(d):
    """Орієнтири (контекст, тестом переваги не підтверджено)."""
    sw = d["swing"]; bits = []
    if sw.get("support"): bits.append(f"підтримка ~{fp(sw['support'][0])}")
    if sw.get("resistance"): bits.append(f"опір ~{fp(sw['resistance'][0])}")
    if sw.get("targets"):
        bits.append("цілі «рівна нога» " + " / ".join(fp(t) for _, t in sw["targets"][:2]))
    line = ""
    if bits:
        line += "   орієнтири: " + "; ".join(bits) + "\n"
    w = d.get("wave")
    if w and w.get("status"):
        s = f"   хвилі (орієнтир, не тестовано, може перемалюватись): {esc(w['status'])}"
        if w.get("inval"): s += f"; сценарій скасується {'нижче' if w.get('inval_below', True) else 'вище'} {fp(w['inval'])}"
        line += s + "\n"
    return line


# ───────────────────────── Telegram ─────────────────────────
def tg_api(token, method, fields=None, files=None):
    url = f"{os.environ.get('TELEGRAM_API', 'https://api.telegram.org')}/bot{token}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        for k, v in (fields or {}).items():
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for k, (fn, data, ct) in files.items():
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\nContent-Type: {ct}\r\n\r\n".encode())
            body.write(data); body.write(b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, data=body.getvalue(), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())


def _strip_html(s):
    import re
    s = re.sub(r"</?(b|i)>", "", s)
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def split_text(text, limit=3800):
    """Ділимо за рядками (теги не рвуться: вони не переходять через рядок), кожен блок ≤ limit."""
    parts, cur = [], ""
    for ln in text.split("\n"):
        while len(ln) > limit:                       # аварійно: дуже довгий рядок
            cut = ln.rfind(" ", 0, limit); cut = cut if cut > 0 else limit
            if cur: parts.append(cur); cur = ""
            parts.append(ln[:cut]); ln = ln[cut:].lstrip()
        if len(cur) + len(ln) + 1 > limit and cur:
            parts.append(cur); cur = ln
        else:
            cur = (cur + "\n" + ln) if cur else ln
    if cur: parts.append(cur)
    return [p for p in parts if p.strip()]


def tg_send_text(token, chat, text):
    fails = 0
    for p in split_text(text):
        try:
            tg_api(token, "sendMessage", dict(chat_id=chat, text=p, parse_mode="HTML", disable_web_page_preview="true"))
        except Exception as e:
            try:                                      # HTML не прийнято → той самий блок звичайним текстом
                tg_api(token, "sendMessage", dict(chat_id=chat, text=_strip_html(p), disable_web_page_preview="true"))
                print("HTML-блок надіслано звичайним текстом:", str(e)[:100])
            except Exception as e2:
                fails += 1; print("блок не надіслано:", str(e2)[:150])
        time.sleep(0.5)
    return fails


# ───────────────────────── графік ─────────────────────────
def make_chart(coin, ts, A, d, G, bars=160):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    a = A[coin]; n = len(ts); lo = max(0, n - bars)
    x = np.arange(lo, n)
    o, h, l, c = a["o"][lo:], a["h"][lo:], a["l"][lo:], a["c"][lo:]
    fig, ax = plt.subplots(figsize=(9, 5), dpi=110)
    up = c >= o
    ax.vlines(x, l, h, color=np.where(up, "#26a269", "#c01c28"), lw=0.8)
    ax.bar(x, np.abs(c - o), bottom=np.minimum(o, c), width=0.7, color=np.where(up, "#26a269", "#c01c28"))
    cs = np.array(a["c"])
    for k, col in ((50, "#1c71d8"), (100, "#9141ac")):
        sm = E.sma_arr(cs, k)[lo:]
        ax.plot(x, sm, color=col, lw=1.1, label=f"SMA{k}")
    w = d["w"][lo:]
    # позиція системи — заливка
    ymin, ymax = np.nanmin(l), np.nanmax(h)
    ax.fill_between(x, ymin, ymax, where=w > 0, color="#26a269", alpha=0.07, step="mid")
    g = G[lo:]
    ax.fill_between(x, ymin, ymax, where=g <= 0, color="#c01c28", alpha=0.05, step="mid")
    ch = np.diff(np.concatenate([[0], w]))
    ei = np.where(ch > 0)[0]; xi = np.where(ch < 0)[0]
    ax.scatter(x[ei], l[ei] * 0.985, marker="^", color="#26a269", s=60, zorder=5, label="вхід системи")
    ax.scatter(x[xi], h[xi] * 1.015, marker="v", color="#c01c28", s=60, zorder=5, label="вихід системи")
    if d.get("stop"): ax.axhline(d["stop"], color="#c01c28", ls="--", lw=1.2); ax.text(x[0], d["stop"], f" стоп {fp(d['stop'])}", va="bottom", color="#c01c28", fontsize=8)
    if d.get("entry"): ax.axhline(d["entry"], color="#26a269", ls="--", lw=1.2); ax.text(x[0], d["entry"], f" вхід вище {fp(d['entry'])}", va="bottom", color="#26a269", fontsize=8)
    sw = d["swing"]
    if sw.get("support"): ax.axhline(sw["support"][0], color="#888", ls=":", lw=0.9); ax.text(x[-1], sw["support"][0], f"підтримка {fp(sw['support'][0])} ", ha="right", va="bottom", color="#666", fontsize=7)
    if sw.get("resistance"): ax.axhline(sw["resistance"][0], color="#888", ls=":", lw=0.9); ax.text(x[-1], sw["resistance"][0], f"опір {fp(sw['resistance'][0])} ", ha="right", va="bottom", color="#666", fontsize=7)
    lab = {"BUY": "КУПУВАТИ", "HOLD": "ТРИМАТИ", "EXIT": "ВИЙТИ", "WAIT": "ЧЕКАТИ", "BLOCKED": "ПОЗА РИНКОМ"}[d["status"]]
    dtv = dt.datetime.fromtimestamp(ts[-1] / 1000, dt.timezone.utc).strftime("%d.%m.%Y")
    ax.set_title(f"{coin}/USDT 1D · {lab} · бал {d['score']}/8 · закриття {fp(d['price'])} ({dtv})", fontsize=10)
    ax.set_xticks(x[::20]); ax.set_xticklabels([dt.datetime.fromtimestamp(ts[i] / 1000, dt.timezone.utc).strftime("%d.%m") for i in x[::20]], fontsize=8)
    ax.set_xlim(x[0] - 1, x[-1] + 2)
    ax.legend(loc="upper left", fontsize=7, ncol=4, frameon=False)
    ax.grid(alpha=0.15)
    fig.text(0.01, 0.005, "зелена заливка = система в позиції · червона = режим BTC вимкнено · не фінансова порада", fontsize=6.5, color="#666")
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png"); plt.close(fig)
    return buf.getvalue()


# ───────────────────────── головна ─────────────────────────
def load_cfg():
    cfg = dict(coins=DEFAULT_COINS, capital_usdt=1000.0, charts=True, max_charts=6, send_when_no_changes=True)
    p = os.path.join(HERE, "plan_config.json")
    if os.path.exists(p):
        try:
            cfg.update({k: v for k, v in json.load(open(p)).items() if not k.startswith("_")})
        except Exception as e:
            print("plan_config.json не прочитано:", e)
    dep = os.environ.get("DEPOSIT", "").strip()
    if dep:
        try: cfg["capital_usdt"] = float(dep)
        except ValueError: pass
    cfg["coins"] = [c.upper() for c in cfg["coins"]]
    if "BTC" not in cfg["coins"]:
        cfg["coins"].insert(0, "BTC")
    return cfg


def load_positions():
    p = os.path.join(HERE, "positions.json")
    out = {}
    if os.path.exists(p):
        try:
            raw = json.load(open(p))
            for k, v in raw.items():
                if k.startswith("_") or not isinstance(v, dict):
                    continue
                if v.get("qty"):
                    out[k.upper()] = dict(qty=float(v["qty"]), avg=float(v["avg"]) if v.get("avg") else None)
        except Exception as e:
            print("positions.json не прочитано:", e)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true"); ap.add_argument("--csv"); ap.add_argument("--asof")
    ap.add_argument("--mock", action="store_true"); ap.add_argument("--no-charts", action="store_true"); ap.add_argument("--out", help="папка для PNG графіків у --dry")
    args = ap.parse_args(argv)
    cfg = load_cfg(); positions = load_positions()
    srcs = {}
    if args.csv:
        data, fng = load_csv(args.csv, cfg["coins"], args.asof)
    else:
        data, fng, srcs = load_live(cfg["coins"])
    if "BTC" not in data:
        print("Немає даних BTC — план неможливий:", srcs.get("BTC"))
        return 2
    ts, A = align(data, cfg["coins"])
    age = (time.time() * 1000 - (ts[-1] + DAY)) / DAY
    if not args.csv and age > 1.5:
        print(f"⚠ остання закрита свічка BTC старіша за {age:.1f} дн. — дані можуть бути неповні")
    plan = build_plan(ts, A, cfg, positions, fng)
    msgs = build_messages(plan, cfg, positions, fng, srcs)
    miss = [c for c in cfg["coins"] if c not in data]
    if miss and not args.csv:
        msgs.append("\n⚠️ Дані не завантажились для: " + ", ".join(f"{c} ({srcs.get(c, '?')})" for c in miss))
    text = "\n".join(msgs)
    # що змінилось
    changed = any(plan["coins"][c].get("status") in ("BUY", "EXIT") for c in plan["coins"] if c != "BTC") or \
        (plan["G"][-1] != plan["G"][-2]) or any(plan["coins"][c].get("mr") == "BUY" for c in plan["coins"])
    token = os.environ.get("TELEGRAM_TOKEN", ""); chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    print(text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    # графіки: монети з новим сигналом + ті, що у твоїх позиціях і поза системою
    chart_list = []
    if cfg["charts"] and not args.no_charts:
        for c, d in plan["coins"].items():
            if d.get("status") in ("BUY", "EXIT"): chart_list.append(c)
        for c in list(positions) + list(cfg.get("focus", [])):
            if c in plan["coins"] and plan["coins"][c].get("status") not in ("NODATA", None) and c not in chart_list: chart_list.append(c)
        chart_list = chart_list[:cfg["max_charts"]]
    pngs = {}
    for c in chart_list:
        png = make_chart(c, ts, A, plan["coins"][c], plan["G"])
        if png: pngs[c] = png
    if args.dry or not (token and chat):
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            for c, b in pngs.items():
                open(os.path.join(args.out, f"{c}.png"), "wb").write(b)
            open(os.path.join(args.out, "plan.txt"), "w").write(text)
        if not args.dry:
            print("\n(TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не задані — повідомлення не надіслано)")
        return 0
    if not changed and not cfg.get("send_when_no_changes", True):
        print("змін немає — повідомлення пропущено (send_when_no_changes=false)")
        return 0
    fails = tg_send_text(token, chat, text)
    for c, b in pngs.items():
        d = plan["coins"][c]
        cap_ = {"BUY": "🟢 купувати", "EXIT": "🔴 вийти", "HOLD": "✅ тримати", "WAIT": "⏳ чекати", "BLOCKED": "🧊 поза ринком"}.get(d["status"], "")
        try:
            tg_api(token, "sendPhoto", dict(chat_id=chat, caption=f"{c}: {cap_}"), files={"photo": (f"{c}.png", b, "image/png")})
        except Exception as e:
            print("графік не надіслано:", c, str(e)[:100])
        time.sleep(0.7)
    print("надіслано в Telegram:", len(msgs), "блоків,", len(pngs), "графіків", f"(помилок: {fails})" if fails else "")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
