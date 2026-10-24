"""Elliott v2 — еталонний рушій (Python), 1:1 прототип для Pine.
Усі відношення рахуються у ЛОГ-шкалі: y = dd * ln(ціна), dd=+1 якщо структура починається з мінімуму, -1 якщо з максимуму.
Перша нога кожної структури в signed-координатах завжди «вгору» (y1 > y0).

Структури (піввузли -> ноги):
  IMP   5 ніг  імпульс 1-2-3-4-5           (в2 не нижче старту 1; в3 >= 1.0 x в1 та не найкоротша; в4 не перекриває в1)
  DIAG  5 ніг  клин (діагональ) 1-5        (в4 перекриває в1, в4 не нижче кінця в2, в3<в1, в5<в3, лінії 1-3 і 2-4 сходяться)
  COR   3 ноги ABC (зигзаг / флет)         (B не глибше початку A; C закінчується ЗА кінцем A)
  WXY   7 ніг  подвійна комбінація W-X-Y   (W та Y = ABC, X не глибше початку W, Y закінчується за кінцем W)
  TRI   5 ніг  скорочувальний трикутник ABCDE (нижчі максимуми, вищі мінімуми; бар'єрний — з допуском)
  IMPT  9 ніг  імпульс, у якому хвиля 4 = трикутник  (1,2,3 + a-b-c-d-e + 5)
  IMPD  9 ніг  імпульс, у якому хвиля 5 = кінцевий клин (1,2,3,4 + клин з 5 ніг)
Ланцюжок: після руху (IMP/DIAG/IMPT/IMPD) -> лише корекція (COR/WXY/TRI); після корекції -> лише рух (IMP/DIAG/IMPT/IMPD).
Корекція не може піти за старт руху, який вона коригує."""
import math

LN = math.log
EX = math.exp
NEG = -1e18

# ───────────── налаштування (як входи в Pine) ─────────────
CFG = {
    "exp_ok": False,     # дозволити розширений флет (B за старт A) — за замовчуванням вимкнено (норма користувача)
    "k3_min": 1.0,       # мін. хвиля3/хвиля1 (лог)
    "base_leg": 0.3, "skip": -1.0, "alt": 1.0,
    "gmin": {"IMP": 5.0, "DIAG": 5.0, "COR": 3.0, "WXY": 8.0, "TRI": 4.0, "IMPT": 9.0, "IMPD": 9.0},
}

# ───────────── ATR, зигзаг ─────────────
def atr_rma(rows, n=14):
    tr = []
    for i, r in enumerate(rows):
        if i == 0: tr.append(r[2] - r[3])
        else:
            pc = rows[i-1][4]
            tr.append(max(r[2] - r[3], abs(r[2] - pc), abs(r[3] - pc)))
    out = [None] * len(rows)
    if len(rows) < n: return out
    s = sum(tr[:n]) / n; out[n-1] = s
    for i in range(n, len(rows)):
        s = (s * (n - 1) + tr[i]) / n; out[i] = s
    return out

def zigzag(rows, atr, mult, minpct):
    """ATR-зигзаг на закритих свічках. -> (confirmed, provisional); pivot=(idx, price, typ[+1 high/-1 low], conf_idx)."""
    piv = []; n = len(rows); dirn = 0
    hi_i, hi_p = 0, rows[0][2]; lo_i, lo_p = 0, rows[0][3]
    ext_i, ext_p = 0, 0.0
    for i in range(n):
        t, o, h, l, c = rows[i]
        a = atr[i] if atr[i] else (h - l)
        dev = max(mult * a, minpct * c)
        if dirn == 0:
            if h > hi_p: hi_p, hi_i = h, i
            if l < lo_p: lo_p, lo_i = l, i
            if hi_p - lo_p >= dev:
                if lo_i < hi_i:
                    piv.append((lo_i, lo_p, -1, i)); dirn = 1; ext_i, ext_p = hi_i, hi_p
                else:
                    piv.append((hi_i, hi_p, 1, i)); dirn = -1; ext_i, ext_p = lo_i, lo_p
        elif dirn == 1:
            if h >= ext_p: ext_i, ext_p = i, h
            elif ext_p - l >= dev:
                piv.append((ext_i, ext_p, 1, i)); dirn = -1; ext_i, ext_p = i, l
        else:
            if l <= ext_p: ext_i, ext_p = i, l
            elif h - ext_p >= dev:
                piv.append((ext_i, ext_p, -1, i)); dirn = 1; ext_i, ext_p = i, h
    prov = (ext_i, ext_p, 1 if dirn == 1 else -1, n - 1) if dirn != 0 else None
    return piv, prov

# ───────────── допоміжні ─────────────
def pts(x, lo, hi, ilo=None, ihi=None, soft=0.0):
    s = 0.0
    if lo <= x <= hi:
        s = 2.0
        if ilo is not None and ilo <= x <= ihi: s += 1.0
    elif soft and (lo - soft) <= x <= (hi + soft): s = 1.0
    return s

def dd_of(P, i):
    return 1 if P[i][2] == -1 else -1

def yt(P, i, dd, n):
    return [dd * LN(P[i + k][1]) for k in range(n + 1)], [P[i + k][0] for k in range(n + 1)]

# ───────────── ядра оцінки (op=True: остання нога ще триває) ─────────────
def imp_core(P, i, dd, n, op):
    y, t = yt(P, i, dd, n)
    w1 = y[1] - y[0]
    if w1 <= 0: return None
    sc = 0.0; info = {"dd": dd, "n": n, "y": y}
    if n >= 2:
        if y[2] <= y[0]: return None                                   # R1: в2 не нижче старту в1
        r2 = (y[1] - y[2]) / w1; info["r2"] = r2
        sc += pts(r2, 0.382, 0.786, 0.5, 0.618, 0.12)
        if r2 > 0.9: sc -= 1
    if n >= 3:
        w3 = y[3] - y[2]; k3 = w3 / w1; info["k3"] = k3
        if not (op and n == 3):
            if y[3] <= y[1]: return None                               # в3 має пройти кінець в1
            if k3 < CFG["k3_min"]: return None                         # НОРМА: в3 >= 1.0 x в1 (лог)
        if k3 >= 1.618: sc += 2 + (1 if k3 <= 2.618 else 0)
        elif k3 >= 1.0: sc += 1
    if n >= 4:
        if y[4] <= y[1]: return None                                   # R3: в4 не заходить у територію в1
        w3 = y[3] - y[2]
        r4 = (y[3] - y[4]) / w3; info["r4"] = r4
        sc += pts(r4, 0.236, 0.5, 0.236, 0.382, 0.08)
        if r4 > 0.618: sc -= 1
        d2 = t[2] - t[1]; d4 = t[4] - t[3]
        if d2 > 0 and d4 > 0:
            q = d4 / d2
            if q >= 1.5 or q <= 0.67: sc += 1                           # чергування за часом
        if (info["r2"] >= 0.5) != (r4 >= 0.5): sc += 1                  # чергування за глибиною
    if n >= 5:
        w3 = y[3] - y[2]; w5 = y[5] - y[4]
        k5 = w5 / w1; info["k5"] = k5
        if not op:
            if w5 <= 0: return None
            if w3 < min(w1, w5): return None                           # R2: в3 не найкоротша
            if y[5] <= y[3]: sc -= 4                                    # усічена 5-та — дуже підозріло
        if 0.618 <= k5 <= 1.618: sc += 1
        if 0.85 <= k5 <= 1.15: sc += 1
        if w5 > 1.618 * w3: sc -= 1
    return sc, info

def diag_core(P, i, dd, n, op):
    """Клин: потрібна n>=4 (до цього неможливо відрізнити від імпульсу). Скорочувальний."""
    if n < 4: return None
    y, t = yt(P, i, dd, n)
    w1 = y[1] - y[0]
    if w1 <= 0: return None
    if y[2] <= y[0]: return None
    w3 = y[3] - y[2]
    if y[3] <= y[1]: return None
    if w3 >= w1: return None                                            # скорочувальний: в3 < в1
    if y[4] > y[1]: return None                                          # клин: в4 ПЕРЕКРИВАЄ в1
    if y[4] <= y[2]: return None                                         # в4 не нижче кінця в2
    sc = 2.0; info = {"dd": dd, "n": n, "y": y}
    r2 = (y[1] - y[2]) / w1; info["r2"] = r2
    k3 = w3 / w1; info["k3"] = k3
    w2 = y[1] - y[2]; w4 = y[3] - y[4]
    if t[3] > t[1] and t[4] > t[2]:
        s13 = (y[3] - y[1]) / (t[3] - t[1]); s24 = (y[4] - y[2]) / (t[4] - t[2])
        if not (op and n == 4):
            if s24 <= s13: return None                                   # лінії 1-3 і 2-4 мусять сходитись
    if 0.5 <= r2 <= 0.9: sc += 1
    if 0.5 <= k3 <= 0.95: sc += 1
    if w4 < w2: sc += 1
    if n >= 5:
        w5 = y[5] - y[4]; k5 = w5 / w3; info["k5"] = k5
        if not op:
            if y[5] <= y[3]: return None                                 # в5 виходить за кінець в3
            if w5 >= w3: return None                                     # в5 < в3
        if 0.5 <= k5 <= 0.95: sc += 1
    return sc, info

def cor_core(P, i, dd, n, op, exp_ok=None):
    """ABC (зигзаг / флет, опційно розширений флет). B не глибше початку A; C закінчується ЗА кінцем A."""
    if exp_ok is None: exp_ok = CFG["exp_ok"]
    y, t = yt(P, i, dd, n)
    A = y[1] - y[0]
    if A <= 0: return None
    sc = 0.0; info = {"dd": dd, "n": n, "y": y, "kind": ""}
    if n >= 2:
        rb = (y[1] - y[2]) / A; info["rb"] = rb
        if rb >= 1.0 and (not exp_ok or rb > 1.382): return None        # НОРМА: B не перевищує початок A
        if not (op and n == 2) and rb < 0.236: return None
        if rb < 0.886:
            info["kind"] = "zigzag"
            sc += (2 + (1 if 0.5 <= rb <= 0.786 else 0)) if rb >= 0.382 else (1 if rb >= 0.236 else 0)
        elif rb < 1.0:
            info["kind"] = "flat"; sc += 2
        else:
            info["kind"] = "exp.flat"; sc += 1
    if n >= 3:
        kc = (y[3] - y[2]) / A; info["kc"] = kc
        if not (op and n == 3) and y[3] <= y[1]: return None            # НОРМА: C нижче/за кінцем A
        kind = info["kind"]
        if y[3] > y[1]:
            if kind == "zigzag":
                sc += pts(kc, 0.618, 1.618, 1.0, 1.0, 0.15) + (1 if abs(kc - 1.0) < 0.12 or abs(kc - 1.618) < 0.15 or abs(kc - 0.618) < 0.1 else 0)
            elif kind == "flat":
                sc += pts(kc, 0.9, 1.618, 1.0, 1.3, 0.1)
            else:
                sc += pts(kc, 1.0, 2.618, 1.272, 1.618, 0.1)
    return sc, info

def tri_core(P, i, dd, n, op):
    """Скорочувальний трикутник ABCDE, n=4..5. Допуск бар'єру 4% від a."""
    if n < 4 or n > 5: return None
    y, t = yt(P, i, dd, n)
    a = y[1] - y[0]
    if a <= 0: return None
    tol = 0.04 * a
    if y[2] <= y[0]: return None                                          # b не глибше початку a
    if y[3] <= y[2] or y[3] > y[1] + tol: return None                     # c не виходить за кінець a
    if y[4] >= y[3]: return None
    if y[4] < y[2] - tol: return None                                     # d не нижче кінця b
    b = y[1] - y[2]; c = y[3] - y[2]; d = y[3] - y[4]
    if b <= 0 or c >= a * (1 + 0.04): return None
    sc = 2.0
    if 0.5 <= b / a <= 0.9: sc += 1
    if 0.5 <= c / a <= 0.95: sc += 1
    if 0.5 <= d / b <= 0.9: sc += 1
    info = {"dd": dd, "n": n, "y": y}
    if n >= 5:
        e = y[5] - y[4]
        if not op:
            if e <= 0: return None
            if y[5] > y[3] + tol: return None                             # e не виходить за кінець c
        if e > 0 and 0.5 <= e / c <= 0.9: sc += 1
    return sc, info

def wxy_core(P, i, dd, n, op):
    """Подвійна комбінація: W=ABC (3 ноги), X (1), Y=ABC (3). n=4..7."""
    if n < 4 or n > 7: return None
    rw = cor_core(P, i, dd, 3, False)
    if rw is None or rw[0] < CFG["gmin"]["COR"]: return None
    y, t = yt(P, i, dd, n)
    Wt = y[3] - y[0]
    sc = rw[0]
    info = {"dd": dd, "n": n, "y": y, "W": Wt}
    if y[4] <= y[0]: return None                                          # X не глибше початку W
    rx = (y[3] - y[4]) / Wt; info["rx"] = rx
    if not (op and n == 4) and rx < 0.236: return None
    sc += pts(rx, 0.382, 0.886, 0.5, 0.786, 0.1)
    if n >= 5:
        ry = cor_core(P, i + 4, dd, n - 4, op)
        if ry is None: return None
        sc += ry[0]
        if n == 7:
            if y[7] <= y[3]: return None                                  # Y закінчується за кінцем W
            Yt = y[7] - y[4]; info["ky"] = Yt / Wt
            if 0.618 <= Yt / Wt <= 1.618: sc += 1
    return sc, info

def impt_core(P, i, dd, n, op):
    """Імпульс із трикутником у хвилі 4: 1,2,3 + a,b,c,d,e + 5 (n=7..9)."""
    if n < 7 or n > 9: return None
    r3 = imp_core(P, i, dd, 3, False)
    if r3 is None: return None
    tn = min(n, 8) - 3
    top = op if n <= 8 else False
    rt = tri_core(P, i + 3, -dd, tn, top)
    if rt is None: return None
    y, t = yt(P, i, dd, n)
    for k in range(4, min(n, 8) + 1):
        if y[k] <= y[1]: return None                                      # трикутник-в4 не заходить у територію в1
    sc = r3[0] + rt[0]
    info = {"dd": dd, "n": n, "y": y, "a": y[3] - y[4], "r2": r3[1]["r2"], "k3": r3[1]["k3"]}
    if r3[1]["r2"] >= 0.5: sc += 1                                        # чергування: глибока в2 / бічна в4
    if n >= 9:
        if not op:
            if y[9] <= y[3]: return None                                  # в5 нова вершина після трикутника
        w5 = y[9] - y[8]; atri = y[3] - y[4]
        if atri > 0 and 0.618 <= w5 / atri <= 1.618: sc += 1
        k5 = w5 / (y[1] - y[0]); info["k5"] = k5
        if 0.618 <= k5 <= 1.618: sc += 1
    return sc, info

def impd_core(P, i, dd, n, op):
    """Імпульс із кінцевим клином у хвилі 5: 1,2,3,4 + клин (n=8..9)."""
    if n < 8 or n > 9: return None
    r4 = imp_core(P, i, dd, 4, False)
    if r4 is None: return None
    rd = diag_core(P, i + 4, dd, n - 4, op)
    if rd is None: return None
    y, t = yt(P, i, dd, n)
    sc = r4[0] + rd[0]
    if n >= 9 and not op and y[9] <= y[3]: return None                    # клин у в5 закінчується вище кінця в3
    info = {"dd": dd, "n": n, "y": y, "r2": r4[1]["r2"], "k3": r4[1]["k3"], "r4": r4[1]["r4"]}
    return sc, info

CORES = {"IMP": imp_core, "DIAG": diag_core, "COR": cor_core, "WXY": wxy_core, "TRI": tri_core, "IMPT": impt_core, "IMPD": impd_core}

# ───────────── ланцюжок станів ─────────────
# стани: 0 після руху, 1 після ABC/WXY, 2 після трикутника, 3 старт/пропуск
TYPES = [
    ("IMP",  (5,), (1, 2, 3), 0),
    ("DIAG", (5,), (1, 2, 3), 0),
    ("COR",  (3,), (0,), 1),
    ("WXY",  (7,), (0,), 1),
    ("TRI",  (5,), (0,), 2),
    ("IMPT", (9,), (1, 2, 3), 0),
    ("IMPD", (9,), (1, 2, 3), 0),
]
CORRECTIVE = ("COR", "WXY", "TRI")
TAIL_RULES = {   # тип: (мін. ніг, макс. ніг, потрібний бал по кількості ніг)
    "IMP":  (1, 5, {1: 0.0, 2: 1.0, 3: 2.5, 4: 4.0, 5: 6.0}),
    "DIAG": (4, 5, {4: 4.0, 5: 5.0}),
    "COR":  (1, 3, {1: 0.0, 2: 1.0, 3: 4.0}),
    "WXY":  (4, 7, {4: 5.0, 5: 5.5, 6: 6.5, 7: 8.0}),
    "TRI":  (4, 5, {4: 5.0, 5: 5.0}),
    "IMPT": (7, 9, {7: 9.0, 8: 9.5, 9: 10.0}),
    "IMPD": (8, 9, {8: 9.0, 9: 10.0}),
}
NS = {"IMP": 0, "DIAG": 0, "COR": 1, "WXY": 1, "TRI": 2, "IMPT": 0, "IMPD": 0}
ALLOWED = {t: a for t, _, a, _ in TYPES}

def evalt(typ, P, i, L, op=False):
    return CORES[typ](P, i, dd_of(P, i), L, op)

def ctx_check(P, i, j, dd, s_back):
    """Корекція (стан 0) не може піти за старт руху, який вона коригує. -> (ok, бонус глибини)."""
    if s_back is None: return True, 0.0
    i0 = s_back[0]
    yj = dd * LN(P[j][1]); yi = dd * LN(P[i][1]); y0 = dd * LN(P[i0][1])
    if yj > y0: return False, 0.0
    span = y0 - yi
    if span <= 0: return True, 0.0
    dep = (yj - yi) / span
    return True, (1.0 if 0.236 <= dep <= 0.886 else 0.0)

def parse(P):
    N = len(P)
    V = [[NEG] * 4 for _ in range(N)]
    back = [[None] * 4 for _ in range(N)]
    V[0][3] = 0.0
    for j in range(N):
        if j > 0:
            bs = max(range(4), key=lambda s: V[j-1][s])
            if V[j-1][bs] > NEG / 2 and V[j-1][bs] + CFG["skip"] > V[j][3]:
                V[j][3] = V[j-1][bs] + CFG["skip"]; back[j][3] = (j - 1, bs, "SKIP", 0.0, {})
        for typ, lens, allowed, ns in TYPES:
            for L in lens:
                i = j - L
                if i < 0: continue
                r = CORES[typ](P, i, dd_of(P, i), L, False)
                if r is None: continue
                g, info = r
                if g < CFG["gmin"][typ]: continue
                for s in allowed:
                    if V[i][s] <= NEG / 2: continue
                    bonus = 0.0
                    if typ in CORRECTIVE:
                        ok, bonus = ctx_check(P, i, j, dd_of(P, i), back[i][0])
                        if not ok: continue
                        bonus += CFG["alt"]
                    tot = V[i][s] + g + CFG["base_leg"] * L + bonus
                    if tot > V[j][ns]:
                        V[j][ns] = tot; back[j][ns] = (i, s, typ, g, info)
    return V, back

def backtrack(back, j, s):
    segs = []
    while j > 0 and back[j][s] is not None:
        i, ps, typ, g, info = back[j][s]
        segs.append({"typ": typ, "i": i, "j": j, "g": g, "info": info, "s": ps})
        j, s = i, ps
    return list(reversed(segs))

def tail_hyps(P, prov, V, back, maxback=11):
    N = len(P)
    Q = list(P) + ([prov] if prov else [])
    M = len(Q); out = []
    for b in range(max(0, N - maxback), N):
        for s in range(4):
            if V[b][s] <= NEG / 2: continue
            legs = M - 1 - b
            if legs < 1: continue
            for typ in ("IMP", "DIAG", "COR", "WXY", "TRI", "IMPT", "IMPD"):
                lo, hi, need = TAIL_RULES[typ]
                if s not in ALLOWED[typ]: continue
                if legs < lo or legs > hi: continue
                r = CORES[typ](Q, b, dd_of(Q, b), legs, True)
                if r is None: continue
                g, info = r
                if g < need[legs]: continue
                bonus = 0.0
                if typ in CORRECTIVE:
                    ok, bonus = ctx_check(Q, b, M - 1, dd_of(Q, b), back[b][0])
                    if not ok: continue
                    bonus += CFG["alt"]
                out.append((V[b][s] + g + CFG["base_leg"] * legs + bonus, b, s, typ, g, info, legs))
    out.sort(key=lambda h: -h[0])
    return out, Q

# ───────────── підписи ─────────────
LAB = {
    "IMP": ["1", "2", "3", "4", "5"], "DIAG": ["1", "2", "3", "4", "5"],
    "COR": ["A", "B", "C"],
    "WXY": ["a", "b", "W", "X", "a", "b", "Y"],
    "TRI": ["a", "b", "c", "d", "e"],
    "IMPT": ["1", "2", "3", "a", "b", "c", "d", "e(4)", "5"],
    "IMPD": ["1", "2", "3", "4", "i", "ii", "iii", "iv", "v(5)"],
}

def labels_for(typ, i, L):
    return [(i + k + 1, LAB[typ][k]) for k in range(L)]

def last_motive(back, i):
    """Попередній рух (старт-індекс), що передує корекції, яка починається у піввузлі i."""
    bk = back[i][0] if i < len(back) else None
    return bk[0] if bk is not None else None

# ───────────── план ─────────────
def build_plan(Q, b, typ, legs, info, s, back):
    dd = info["dd"]; y = info["y"]
    pr = lambda v: EX(dd * v)
    plan = {"typ": typ, "legs": legs, "dd": dd, "targets": [], "zone": None, "inval": None, "inval_below": True,
            "status": "", "next": "", "kind": info.get("kind", ""), "note": ""}
    last_px = Q[-1][1]
    inv_set = False
    w1 = y[1] - y[0]
    if typ == "IMP":
        if legs == 1:
            plan["status"] = "хвиля 1 (або A) триває"; plan["next"] = "відкат 0.382–0.618 цієї ноги (лог)"
            plan["zone"] = (pr(y[1] - 0.618 * w1), pr(y[1] - 0.382 * w1)); plan["inval"] = pr(y[0])
        elif legs == 2:
            plan["status"] = "хвиля 2 завершується/завершена"; plan["next"] = "далі хвиля 3: мін. 1.0×в1, зазвичай 1.618×в1"
            plan["zone"] = (pr(y[1] - 0.786 * w1), pr(y[1] - 0.5 * w1))
            plan["targets"] = [("в3 мін 1.0×в1", pr(y[2] + 1.0 * w1)), ("1.618×в1", pr(y[2] + 1.618 * w1)), ("2.618×в1", pr(y[2] + 2.618 * w1))]
            plan["inval"] = pr(y[0])
        elif legs == 3:
            w3 = y[3] - y[2]
            plan["status"] = "хвиля 3 (триває або закінчилась)"; plan["next"] = "відкат у хвилю 4 (0.236–0.5 від хвилі 3), не нижче вершини в1"
            lo = max(y[3] - 0.5 * w3, y[1] + 1e-9)
            plan["zone"] = (pr(lo), pr(y[3] - 0.236 * w3))
            t3 = [("в3 мін 1.0×в1", y[2] + 1.0 * w1), ("1.618×в1", y[2] + 1.618 * w1), ("2.618×в1", y[2] + 2.618 * w1)]
            plan["targets"] = [(n_, pr(v)) for n_, v in t3 if v > y[3]][:3]
            plan["inval"] = pr(y[1])
        elif legs == 4:
            w3 = y[3] - y[2]
            plan["status"] = "хвиля 4 завершується/завершена"; plan["next"] = "далі хвиля 5"
            plan["targets"] = [("0.618×в1", pr(y[4] + 0.618 * w1)), ("1.0×в1", pr(y[4] + 1.0 * w1)), ("0.618×(0→3)", pr(y[4] + 0.618 * (y[3] - y[0])))]
            plan["zone"] = (pr(max(y[3] - 0.5 * w3, y[1] + 1e-9)), pr(y[3] - 0.236 * w3))
            plan["inval"] = pr(y[1])
        else:
            tot = y[5] - y[0]
            plan["status"] = "імпульс 1‑5 завершений (або в5 ще триває)"; plan["next"] = "очікуємо корекцію ABC/WXY/трикутник"
            plan["zone"] = (pr(y[5] - 0.618 * tot), pr(y[5] - 0.382 * tot))
            plan["targets"] = [("низ хв.4", pr(y[4])), ("0.5 імп.", pr(y[5] - 0.5 * tot))]
            plan["inval"] = pr(y[5]); plan["inval_below"] = dd < 0; inv_set = True
            plan["note"] = "ABC після 5: B не вище вершини 5, C нижче A"
    elif typ == "DIAG":
        lead = (s == 3)
        nm = "початковий клин (хв.1/A)" if lead else "кінцевий клин (хв.5/C)"
        plan["kind"] = "LD" if lead else "ED"
        if legs == 4:
            plan["status"] = nm + ": хв.5 клина триває"; plan["inval"] = pr(y[2])
        else:
            plan["status"] = nm + " завершується/завершений"; plan["inval"] = pr(y[5]); plan["inval_below"] = dd < 0; inv_set = True
        if lead:
            plan["next"] = "глибокий відкат (хв.2: 0.5–0.886 клина), далі хвиля 3"
            plan["zone"] = (pr(y[min(legs, 5)] - 0.886 * (y[min(legs, 5)] - y[0])), pr(y[min(legs, 5)] - 0.5 * (y[min(legs, 5)] - y[0])))
        else:
            plan["next"] = "після кінцевого клина — різкий відкат до його початку"
            plan["targets"] = [("початок клина", pr(y[0])), ("кінець хв.2", pr(y[2]))]
    elif typ == "COR":
        if legs == 1:
            plan["status"] = "хвиля A триває"; plan["next"] = "B: відкат 0.5–0.786 від A (не глибше початку A), потім C за кінець A"
            plan["zone"] = (pr(y[1] - 0.786 * w1), pr(y[1] - 0.5 * w1)); plan["inval"] = pr(y[0])
        elif legs == 2:
            plan["status"] = "B завершується/завершена"; plan["next"] = "далі C — обов'язково за кінець A"
            plan["targets"] = [("кінець A (мін. для C)", pr(y[1])), ("C=1.0×A", pr(y[2] + 1.0 * w1)), ("C=1.618×A", pr(y[2] + 1.618 * w1))]
            plan["zone"] = (pr(max(y[2] + 1.0 * w1, y[1])), pr(y[2] + 1.618 * w1))
            plan["inval"] = pr(y[0])
        else:
            plan["status"] = "корекція ABC завершена"; plan["next"] = "далі хвиля 1/3/5 у напрямку попереднього руху"
            plan["inval"] = pr(y[3]); plan["inval_below"] = dd < 0; inv_set = True
            k = last_motive(back, b)
            if k is not None:
                i0 = k
                Mv = dd * LN(Q[b][1]) - dd * LN(Q[i0][1])  # <0 бо рух був у протилежній signed-осі
                Mv = abs(Mv)
                plan["targets"] = [("0.618×попер. рух", pr(y[3] - 0.618 * Mv)), ("1.0×попер. рух", pr(y[3] - 1.0 * Mv))]
    elif typ == "WXY":
        Wt = info["W"]
        if legs == 4:
            plan["status"] = "комбінація W‑X: X триває"; plan["next"] = "далі Y (за кінець W)"
            plan["zone"] = (pr(y[3] - 0.786 * Wt), pr(y[3] - 0.382 * Wt)); plan["inval"] = pr(y[0])
        elif legs < 7:
            plan["status"] = "комбінація W‑X‑Y: Y триває"; plan["next"] = "Y закінчується за кінцем W"
            plan["targets"] = [("кінець W (мін. для Y)", pr(y[3])), ("Y=1.0×W", pr(y[4] + 1.0 * Wt)), ("Y=1.618×W", pr(y[4] + 1.618 * Wt))]
            plan["inval"] = pr(y[4])
        else:
            plan["status"] = "комбінація WXY завершена"; plan["next"] = "далі хвиля 1/3/5 у напрямку попереднього руху"
            plan["inval"] = pr(y[7]); plan["inval_below"] = dd < 0; inv_set = True
    elif typ == "TRI":
        a = y[1] - y[0]
        if legs == 4:
            plan["status"] = "трикутник ABCDE: e триває"; plan["next"] = "після e — вихід (тяга ≈ a) у бік попереднього руху"
            c = y[3] - y[2]
            plan["zone"] = (pr(min(y[4] + 0.618 * c, y[3])), pr(min(y[4] + 0.85 * c, y[3])))
            plan["inval"] = pr(y[0])
        else:
            plan["status"] = "трикутник ABCDE завершується/завершений"; plan["next"] = "різкий вихід (тяга ≈ a) у бік попереднього руху"
            plan["targets"] = [("тяга 0.618×a", pr(y[5] - 0.618 * a)), ("тяга 1.0×a", pr(y[5] - 1.0 * a))]
            plan["inval"] = pr(y[0])
    elif typ == "IMPT":
        a = y[3] - y[4]
        if legs <= 7:
            plan["status"] = "імпульс: хвиля 4 = трикутник (e триває)"; plan["next"] = "після e — хвиля 5 (тяга ≈ a)"
            plan["inval"] = pr(y[1])
        elif legs == 8:
            plan["status"] = "трикутник у хв.4 завершується"; plan["next"] = "далі хвиля 5"
            plan["targets"] = [("в5=0.618×a", pr(y[8] + 0.618 * a)), ("в5=1.0×a", pr(y[8] + 1.0 * a)), ("в5=0.618×в1", pr(y[8] + 0.618 * w1))]
            plan["inval"] = pr(y[1])
        else:
            tot = y[9] - y[0]
            plan["status"] = "імпульс (хв.4 = трикутник) завершений"; plan["next"] = "очікуємо корекцію"
            plan["zone"] = (pr(y[9] - 0.618 * tot), pr(y[9] - 0.382 * tot)); plan["inval"] = pr(y[9]); plan["inval_below"] = dd < 0; inv_set = True
    else:  # IMPD
        if legs == 8:
            plan["status"] = "імпульс: хв.5 = кінцевий клин (триває)"; plan["next"] = "після клина — різкий відкат до його початку"
            plan["targets"] = [("початок клина", pr(y[4])), ("кінець хв.2", pr(y[2]))]; plan["inval"] = pr(y[6])
        else:
            plan["status"] = "імпульс із кінцевим клином завершений"; plan["next"] = "різкий відкат до початку клина"
            plan["targets"] = [("початок клина", pr(y[4])), ("кінець хв.2", pr(y[2]))]
            plan["inval"] = pr(y[9]); plan["inval_below"] = dd < 0; inv_set = True
    if plan["inval"] is not None and not inv_set:
        plan["inval_below"] = plan["inval"] < last_px
    return plan

def analyse(rows, mult, minpct, atr_n=14):
    atr = atr_rma(rows, atr_n)
    P, prov = zigzag(rows, atr, mult, minpct)
    if len(P) < 4: return None
    V, back = parse(P)
    hyps, Q = tail_hyps(P, prov, V, back)
    if not hyps: return None
    best = hyps[0]; alt = None
    for h in hyps[1:]:
        if h[3] != best[3] or h[1] != best[1]:
            alt = h; break
    out = {"P": P, "prov": prov, "Q": Q, "atr": atr, "V": V, "back": back}
    for key, h in (("main", best), ("alt", alt)):
        if h is None: out[key] = None; continue
        tot, b, s, typ, g, info, legs = h
        out[key] = {"typ": typ, "b": b, "s": s, "legs": legs, "g": g, "info": info, "score": tot,
                    "segs": backtrack(back, b, s), "plan": build_plan(Q, b, typ, legs, info, s, back)}
    out["segs"] = out["main"]["segs"]; out["tail"] = out["main"]; out["plan"] = out["main"]["plan"]
    return out
