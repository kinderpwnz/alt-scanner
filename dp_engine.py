# -*- coding: utf-8 -*-
"""Денний план · ядро рішень (DP-1). Один код і для бектесту, і для живого плану.

Рішення приймаються лише за ЗАКРИТИМИ денними свічками (UTC), виконання — з відкриття наступного дня.

Що перевірено бектестом і використовується в рішеннях:
  Ринок-гейт G : BTC > SMA100×1.03 → ON; BTC < SMA100×0.97 → OFF; між ними — попередній стан.
  Рушій A «Тренд»: бал = частка з 8 моделей «у лонгу» (ціна>SMA20/50/100; Donchian 20/10, 40/20, 60/30; моментум 30д і 60д > 0).
        вхід: бал ≥ 0.75 і G=ON (після стопу — лише на новому 20-денному максимумі);
        вихід: бал ≤ 0.5, або G=OFF, або закриття < найвище закриття з входу − 2×ATR14.
  «Відскок» (лише коли G=OFF, малий розмір): RSI14<25 → купити; вихід RSI14≥50, або 10 днів, або закриття < вхід − 3×ATR14.
  Вага слота = max(A, 0.5×Відскок): 0, ½ або 1 слот. Слот = капітал системи / кількість альтів у списку.
НЕ використовується в рішеннях (клас ZZ / run_B залишені лише для орієнтирів на графіку): механічний «свінг» (купити підтверджене дно, продати підтверджений хай)
  у тесті дав просадку −70% і гірший результат за трендове ядро.
"""
import math
import numpy as np

# ───────────────────────── параметри (змінювати лише разом із бектестом) ─────────────────────────
P = dict(
    btc_sma=100, gate_band=0.03,
    sma=(20, 50, 100), don=((20, 10), (40, 20), (60, 30)), mom=(30, 60),
    hi=0.75, lo=0.5, chand_k=2.0, reentry=20, atr_n=14,
    zz_k=3.0, zz_min=0.15,
    mr_rsi_in=25.0, mr_rsi_out=50.0, mr_tmax=10, mr_stop_k=3.0,
    warm=60,
)


# ───────────────────────── допоміжні (numpy, без pandas) ─────────────────────────
def sma_arr(x, n):
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    cs = np.cumsum(np.nan_to_num(x, nan=0.0))
    bad = np.cumsum(np.isnan(x).astype(int))
    for t in range(n - 1, len(x)):
        lo = t - n
        s = cs[t] - (cs[lo] if lo >= 0 else 0.0)
        b = bad[t] - (bad[lo] if lo >= 0 else 0)
        out[t] = s / n if b == 0 else np.nan
    return out


def roll_max_prev(x, n):
    """max(x[t-n .. t-1]) (без поточної), NaN якщо немає n повних значень."""
    out = np.full(len(x), np.nan)
    for t in range(n, len(x)):
        w = x[t - n:t]
        if not np.isnan(w).any():
            out[t] = w.max()
    return out


def roll_min_prev(x, n):
    out = np.full(len(x), np.nan)
    for t in range(n, len(x)):
        w = x[t - n:t]
        if not np.isnan(w).any():
            out[t] = w.min()
    return out


def true_range(h, l, c):
    pc = np.concatenate([[np.nan], c[:-1]])
    return np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))


def rsi_arr(c, n=14):
    """RSI Вайлдера (EMA alpha=1/n, перше значення з n-ї зміни) — як у бектесті (pandas ewm adjust=False, min_periods=n)."""
    out = np.full(len(c), np.nan)
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    ru = rd = None
    cnt = 0
    for i in range(len(d)):
        if np.isnan(d[i]):
            continue
        if ru is None:
            ru, rd = up[i], dn[i]
        else:
            ru = (1 / n) * up[i] + (1 - 1 / n) * ru
            rd = (1 / n) * dn[i] + (1 - 1 / n) * rd
        cnt += 1
        if cnt >= n:
            out[i + 1] = 100.0 if rd == 0 else 100 - 100 / (1 + ru / rd)
    return out


class Coin:
    """Усі «важкі» масиви на історії (до t включно); гіпотетичну наступну свічку — через step(..., p=...)."""

    def __init__(self, o, h, l, c, p=None):
        self.p = dict(P if p is None else p)
        self.o, self.h, self.l, self.c = [np.asarray(x, float) for x in (o, h, l, c)]
        n = len(self.c)
        pp = self.p
        self.n = n
        self.sma = {k: sma_arr(self.c, k) for k in pp["sma"]}
        self.don_hh = {L: roll_max_prev(self.h, L) for L, _ in pp["don"]}
        self.don_ll = {Lx: roll_min_prev(self.l, Lx) for _, Lx in pp["don"]}
        self.hh_re = roll_max_prev(self.h, pp["reentry"])
        self.tr = true_range(self.h, self.l, self.c)
        self.atr = sma_arr(self.tr, pp["atr_n"])
        self.rsi = rsi_arr(self.c, 14)

    # ── 8 моделей на барі t (p — гіпотетичне закриття, якщо задано; тоді h=l=p) ──
    def models(self, t, don_state_prev, p=None):
        pp = self.p; c = self.c
        cc = c[t] if p is None else p
        res = []
        for k in pp["sma"]:
            if p is None:
                s = self.sma[k][t]
            else:
                w = c[t - k + 1:t]   # k-1 попередніх закриттів (t — це індекс гіпотетичного бару, тому c[t-k+1..t-1])
                s = (w.sum() + p) / k if len(w) == k - 1 and not np.isnan(w).any() else np.nan
            res.append(bool(not np.isnan(s) and cc > s))
        dstate = []
        for (L, Lx), prev in zip(pp["don"], don_state_prev):
            hv = self.don_hh[L][t] if p is None else _prevmax(self.h, t, L)
            lv = self.don_ll[Lx][t] if p is None else _prevmin(self.l, t, Lx)
            st = prev
            if not np.isnan(hv) and cc > hv:
                st = 1.0
            elif not np.isnan(lv) and cc < lv:
                st = 0.0
            dstate.append(st); res.append(bool(st > 0))
        for m in pp["mom"]:
            ref = c[t - m] if t - m >= 0 else np.nan
            res.append(bool(not np.isnan(ref) and cc > ref))
        return res, dstate

    def atr_at(self, t, p=None):
        if p is None:
            return self.atr[t]
        n = self.p["atr_n"]
        prev = self.tr[t - n + 1:t]
        if len(prev) != n - 1 or np.isnan(prev).any() or np.isnan(self.c[t - 1]):
            return np.nan
        tr_new = abs(p - self.c[t - 1])    # h=l=p → max(0, |p−pc|, |p−pc|)
        return (prev.sum() + tr_new) / n


def _prevmax(x, t, n):
    w = x[t - n:t]
    return np.nan if len(w) < n or np.isnan(w).any() else w.max()


def _prevmin(x, t, n):
    w = x[t - n:t]
    return np.nan if len(w) < n or np.isnan(w).any() else w.min()


# ───────────────────────── гейт ринку (BTC) ─────────────────────────
def btc_gate(btc_c, p=None):
    pp = p or P
    s = sma_arr(btc_c, pp["btc_sma"])
    st = 0.0; g = np.zeros(len(btc_c))
    for t in range(len(btc_c)):
        m = s[t]
        if np.isnan(m) or np.isnan(btc_c[t]):
            g[t] = 0.0 if np.isnan(m) else st
            continue
        if btc_c[t] > m * (1 + pp["gate_band"]): st = 1.0
        elif btc_c[t] < m * (1 - pp["gate_band"]): st = 0.0
        g[t] = st
    return g, s


# ───────────────────────── Рушій A: тренд ─────────────────────────
class MachineA:
    def __init__(self):
        self.inpos = False; self.hiC = 0.0; self.blocked = False
        self.don = [0.0, 0.0, 0.0]

    def clone(self):
        m = MachineA(); m.inpos, m.hiC, m.blocked, m.don = self.inpos, self.hiC, self.blocked, list(self.don); return m

    def step(self, cn: Coin, t, G, p=None):
        """Один крок на барі t. p: гіпотетичне закриття (тоді сам бар t «додається» — індекси історії доступні до t-1)."""
        pp = cn.p
        cc = cn.c[t] if p is None else p
        if np.isnan(cc):
            return 1.0 if self.inpos else 0.0
        m, dstate = cn.models(t, self.don, p)
        self.don = dstate
        score = sum(m) / 8.0
        atr = cn.atr_at(t, p)
        hh = cn.hh_re[t] if p is None else _prevmax(cn.h, t, pp["reentry"])
        if self.inpos:
            self.hiC = max(self.hiC, cc)
            if score <= pp["lo"] or G <= 0:
                self.inpos = False; self.blocked = False
            elif (not np.isnan(atr)) and cc < self.hiC - pp["chand_k"] * atr:
                self.inpos = False; self.blocked = True
        else:
            if score >= pp["hi"] and G > 0:
                if (not self.blocked) or (not np.isnan(hh) and cc > hh):
                    self.inpos = True; self.hiC = cc; self.blocked = False
            if score <= pp["lo"] or G <= 0:
                self.blocked = False
        self.score = score; self.models_now = m; self.atr_now = atr
        return 1.0 if self.inpos else 0.0


def run_A(cn: Coin, G):
    n = cn.n; w = np.zeros(n); sc = np.full(n, np.nan); M = MachineA()
    for t in range(n):
        if np.isnan(cn.c[t]):
            w[t] = 1.0 if M.inpos else 0.0   # (поки монети нема — позиції немає)
            continue
        w[t] = M.step(cn, t, G[t]); sc[t] = M.score
    return w, sc, M


# ───────────────────────── Рушій B: свінг (зигзаг) ─────────────────────────
class ZZ:
    """Каузальний зигзаг по high/low. direction: 0 невідомо, 1 вгору (шукаємо хай), -1 вниз (шукаємо дно)."""
    def __init__(self, k=3.0, min_dev=0.15):
        self.k, self.min_dev = k, min_dev
        self.dir = 0; self.hi_i = 0; self.lo_i = 0
        self.piv = []          # (kind, idx, price, conf_idx)
        self.state = 0.0       # 1 після підтвердженого дна, 0 після підтвердженого хая
        self.started = False

    def clone(self):
        z = ZZ(self.k, self.min_dev); z.dir, z.hi_i, z.lo_i = self.dir, self.hi_i, self.lo_i
        z.piv = list(self.piv); z.state = self.state; z.started = self.started; return z

    def step(self, h, l, c, atr, t):
        if not self.started:
            if np.isnan(h[t]) or np.isnan(l[t]): return
            self.started = True; self.hi_i = t; self.lo_i = t; return
        if np.isnan(h[t]) or np.isnan(l[t]): return
        a = atr[t] if not np.isnan(atr[t]) else 0.0
        dev = max(self.min_dev, self.k * a / c[t]) if a > 0 else self.min_dev
        if self.dir == 0:
            if h[t] > h[self.hi_i]: self.hi_i = t
            if l[t] < l[self.lo_i]: self.lo_i = t
            if l[t] <= h[self.hi_i] * (1 - dev) and self.hi_i <= t:
                self.piv.append(("H", self.hi_i, h[self.hi_i], t)); self.dir = -1; self.lo_i = t; self.state = 0.0
            elif h[t] >= l[self.lo_i] * (1 + dev):
                self.piv.append(("L", self.lo_i, l[self.lo_i], t)); self.dir = 1; self.hi_i = t; self.state = 1.0
        elif self.dir == 1:
            if h[t] > h[self.hi_i]: self.hi_i = t
            if l[t] <= h[self.hi_i] * (1 - dev):
                self.piv.append(("H", self.hi_i, h[self.hi_i], t)); self.dir = -1; self.lo_i = t; self.state = 0.0
        else:
            if l[t] < l[self.lo_i]: self.lo_i = t
            if h[t] >= l[self.lo_i] * (1 + dev):
                self.piv.append(("L", self.lo_i, l[self.lo_i], t)); self.dir = 1; self.hi_i = t; self.state = 1.0


def run_B(cn: Coin, G):
    z = ZZ(cn.p["zz_k"], cn.p["zz_min"]); n = cn.n; w = np.zeros(n)
    for t in range(n):
        z.step(cn.h, cn.l, cn.c, cn.atr_zz if hasattr(cn, "atr_zz") else cn.atr, t)
        w[t] = z.state * (1.0 if G[t] > 0 else 0.0)
    return w, z


# ───────────────────────── «Відскок» (лише G=OFF) ─────────────────────────
def run_MR(cn: Coin, G, with_state=False):
    pp = cn.p; n = cn.n; w = np.zeros(n); inpos = False; ent = 0.0; t0 = 0; stp = 0.0; sig = np.zeros(n)
    for t in range(n):
        c = cn.c[t]; rs = cn.rsi[t]; a = cn.atr[t]
        if np.isnan(c) or np.isnan(rs) or np.isnan(a):
            w[t] = 1.0 if inpos else 0.0; continue
        if inpos:
            if c < stp or rs >= pp["mr_rsi_out"] or t - t0 >= pp["mr_tmax"]:
                inpos = False
        else:
            if rs < pp["mr_rsi_in"] and G[t] == 0:
                inpos = True; ent = c; t0 = t; stp = c - pp["mr_stop_k"] * a; sig[t] = 1.0
        w[t] = 1.0 if inpos else 0.0
    if with_state:
        return w, dict(inpos=inpos, ent=ent, t0=t0, stop=stp, sig=sig)
    return w


# ───────────────────────── тригери наступного закриття ─────────────────────────
def _bisect(f, lo, hi, it=40):
    """f(lo)=False, f(hi)=True (або навпаки); повертає межу з точністю ~1e-9 відносно."""
    flo = f(lo)
    for _ in range(it):
        mid = (lo + hi) / 2
        if f(mid) == flo: lo = mid
        else: hi = mid
    return hi if flo is False or flo == 0 else lo


def btc_gate_levels(btc_c, p=None):
    """Рівні закриття BTC, при яких режим перемкнеться: (стан зараз, рівень_вмикання, рівень_вимикання)."""
    g, s = btc_gate(btc_c, p)
    now = g[-1]
    def state_at(x):
        return btc_gate(np.append(btc_c, x), p)[0][-1]
    c = btc_c[-1]
    up = dn = None
    if now == 0:
        hi = c
        for _ in range(60):
            hi *= 1.03
            if state_at(hi) == 1.0:
                lo = hi / 1.03
                for _ in range(45):
                    mid = (lo + hi) / 2
                    if state_at(mid) == 1.0: hi = mid
                    else: lo = mid
                up = hi; break
    else:
        lo = c
        for _ in range(60):
            lo /= 1.03
            if state_at(lo) == 0.0:
                hi = lo * 1.03
                for _ in range(45):
                    mid = (lo + hi) / 2
                    if state_at(mid) == 0.0: lo = mid
                    else: hi = mid
                dn = lo; break
    return now, up, dn


def triggers_A(cn: Coin, M: MachineA, G_now, max_up=3.0, max_dn=0.7):
    """Для рушія A: якщо зараз у позиції — найвища ціна закриття наступного дня, при якій A вийде; якщо ні — найнижча, при якій увійде.
    (гіпотетична свічка h=l=p; режим BTC вважаємо незмінним.)"""
    t = cn.n; c = cn.c[-1]
    def pos_at(p, g=None):
        m = M.clone(); m.step(cn, t, G_now if g is None else g, p=p); return m.inpos, m
    out = dict(inpos=M.inpos, entry=None, exit=None, exit_kind=None)
    if M.inpos:
        if not pos_at(c)[0]:
            out["exit"] = c; out["exit_kind"] = "вже"; return out
        lo = c * (1 - max_dn)
        if pos_at(lo)[0]:
            return out   # навіть −70% не виходить (малоймовірно)
        hi = c
        for _ in range(50):
            mid = (lo + hi) / 2
            if pos_at(mid)[0]: hi = mid
            else: lo = mid
        out["exit"] = lo
        # що саме спрацьовує: чандельє чи тренд
        m = M.clone(); m.step(cn, t, G_now, p=lo)
        out["exit_kind"] = "trail" if (m.blocked) else "тренд"
        out["hiC"] = M.hiC
    else:
        g_eff = G_now
        if G_now <= 0:
            out["blocked_by_gate"] = True
            g_eff = 1.0          # рівень, який діятиме, щойно режим BTC увімкнеться
        hi = c * (1 + max_up)
        if not pos_at(hi, g_eff)[0]:
            return out
        lo = c
        if pos_at(lo, g_eff)[0]:
            out["entry"] = lo; return out
        for _ in range(50):
            mid = (lo + hi) / 2
            if pos_at(mid, g_eff)[0]: hi = mid
            else: lo = mid
        out["entry"] = hi
    return out


# ───────────────────────── структура (зигзаг) і орієнтири ─────────────────────────
def swing_info(cn: Coin, zz_k=2.0, zz_min=0.12, lookback=150):
    """Орієнтири структури (НЕ рішення): дрібніший зигзаг, найближча підтримка/опір, цілі «рівна нога» (AB=CD)."""
    z = ZZ(zz_k, zz_min)
    for t in range(cn.n):
        z.step(cn.h, cn.l, cn.c, cn.atr, t)
    c = cn.c[-1]; n = cn.n
    piv = z.piv
    lows = [p for p in piv if p[0] == "L"]; highs = [p for p in piv if p[0] == "H"]
    sup = None; res = None
    for k, i, pr, ci in reversed(lows):
        if pr < c: sup = (pr, i); break
    for k, i, pr, ci in reversed(highs):
        if pr > c: res = (pr, i); break
    if z.dir == 1 and (res is None or cn.h[z.hi_i] > res[0]):
        res = (cn.h[z.hi_i], z.hi_i)
    # найбільша висхідна нога (L→H) за lookback барів і її проєкція від останнього підтвердженого дна
    legs = []
    for a in range(1, len(piv)):
        if piv[a][0] == "H" and piv[a - 1][0] == "L" and piv[a][1] >= n - lookback:
            legs.append((piv[a][2] - piv[a - 1][2], piv[a - 1][2], piv[a][2]))
    targets = []
    base = lows[-1][2] if lows else None
    if legs and base:
        leg = max(legs)[0]
        for m_ in (1.0, 1.272, 1.618):
            tp = base + m_ * leg
            if tp > c * 1.01: targets.append((m_, tp))
    # структура: чи вищі мінімуми (останні два підтверджені L)
    hl = None
    if len(lows) >= 2:
        hl = lows[-1][2] > lows[-2][2]
    return dict(dir=z.dir, piv=piv, support=sup, resistance=res, targets=targets, hl=hl, base=base)
