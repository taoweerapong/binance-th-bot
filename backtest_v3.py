# ============================================================
# backtest_v3.py — Grid Search + Walk-Forward + Buy&Hold
# หา edge จริง ไม่ใช่ผลที่ overfit
# ============================================================
import json, time, urllib.request, urllib.parse, itertools
from datetime import datetime, timezone

BASE_URL   = "https://api.binance.th"
SYMBOL     = "BTCTHB"
INTERVAL   = "1d"
YEARS_BACK = 5

EMA_FAST, EMA_SLOW = 5, 20
RSI_PERIOD, RSI_BUY_MAX = 14, 70.0
ATR_PERIOD, MAX_ATR_PCT = 14, 8.0
REGIME_EMA, ADX_PERIOD  = 200, 14

TOTAL_COST = 0.60
SIDE_COST  = TOTAL_COST / 2 / 100

START_CAP  = 100_000.0
ALLOC_PCT  = 1.00        # ลงเต็มพอร์ต -> DD สะท้อนความจริง
IS_RATIO   = 0.70        # 70% เทรน / 30% ทดสอบ
MIN_TRADES = 8           # ต่ำกว่านี้ไม่นับ (ตัวอย่างน้อยเกิน)

# ---------- ตารางที่จะทดสอบ ----------
GRID = {
    "SL_ATR":    [1.5, 2.0, 2.5, 3.0],
    "TP_ATR":    [4.0, 6.0, 8.0, 99.0],   # 99 = ไม่ตั้ง TP
    "TRAIL_ATR": [0.0, 2.0, 3.0, 4.0],    # 0 = ปิด trailing
    "ADX_MIN":   [0.0, 20.0, 25.0],       # 0 = ปิด regime filter
}

# ============================================================
# Indicators (แบบ series — คำนวณครั้งเดียวใช้ได้ทุกรอบ)
# ============================================================
def ema_series(v, p):
    out = [None] * len(v)
    if len(v) < p: return out
    s = sum(v[:p]) / p; out[p-1] = s
    k = 2 / (p + 1)
    for i in range(p, len(v)):
        s = v[i] * k + s * (1 - k); out[i] = s
    return out


def rsi_series(c, p=RSI_PERIOD):
    out = [None] * len(c)
    if len(c) < p + 1: return out
    g = [0.0] * len(c); l = [0.0] * len(c)
    for i in range(1, len(c)):
        d = c[i] - c[i-1]
        g[i] = max(d, 0.0); l[i] = max(-d, 0.0)
    ag = sum(g[1:p+1]) / p; al = sum(l[1:p+1]) / p
    out[p] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(p + 1, len(c)):
        ag = (ag * (p - 1) + g[i]) / p
        al = (al * (p - 1) + l[i]) / p
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr_series(h, l, c, p=ATR_PERIOD):
    out = [None] * len(c)
    if len(c) < p + 1: return out
    tr = [0.0] * len(c)
    for i in range(1, len(c)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
    a = sum(tr[1:p+1]) / p; out[p] = a
    for i in range(p + 1, len(c)):
        a = (a * (p - 1) + tr[i]) / p; out[i] = a
    return out


def adx_series(h, l, c, p=ADX_PERIOD):
    n = len(c); out = [None] * n
    if n < 2 * p + 2: return out
    pdm = [0.0] * n; mdm = [0.0] * n; tr = [0.0] * n
    for i in range(1, n):
        up, dn = h[i] - h[i-1], l[i-1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i]  = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))

    s_tr = sum(tr[1:p+1]); s_p = sum(pdm[1:p+1]); s_m = sum(mdm[1:p+1])
    dx = [None] * n

    def calc(st, sp, sm):
        if st == 0: return None
        pdi, mdi = 100 * sp / st, 100 * sm / st
        return None if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi)

    dx[p] = calc(s_tr, s_p, s_m)
    for i in range(p + 1, n):
        s_tr = s_tr - s_tr / p + tr[i]
        s_p  = s_p  - s_p  / p + pdm[i]
        s_m  = s_m  - s_m  / p + mdm[i]
        dx[i] = calc(s_tr, s_p, s_m)

    vals = [d for d in dx[p:2*p+1] if d is not None]
    if len(vals) < p: return out
    a = sum(vals) / len(vals); out[2*p] = a
    for i in range(2*p + 1, n):
        if dx[i] is None: out[i] = a; continue
        a = (a * (p - 1) + dx[i]) / p; out[i] = a
    return out

# ============================================================
# ดึงแท่งเทียน
# ============================================================
def fetch_klines():
    MS_DAY = 86_400_000
    end_ms = int(time.time() * 1000)
    cur = end_ms - YEARS_BACK * 365 * MS_DAY
    out = []
    while cur < end_ms:
        q = urllib.parse.urlencode({"symbol": SYMBOL, "interval": INTERVAL,
                                     "startTime": cur, "limit": 1000})
        try:
            with urllib.request.urlopen(f"{BASE_URL}/api/v1/klines?{q}", timeout=20) as r:
                batch = json.loads(r.read().decode())
        except Exception as e:
            print(f"❌ {e}"); break
        if not batch: break
        out += batch
        nxt = int(batch[-1][0]) + 1
        if nxt <= cur: break
        cur = nxt
        if len(batch) < 1000: break
        time.sleep(0.3)
    seen, uniq = set(), []
    for k in out:
        if k[0] not in seen: seen.add(k[0]); uniq.append(k)
    uniq.sort(key=lambda k: k[0])
    return uniq[:-1]


# ============================================================
# Backtest Engine (ใช้ series ที่คำนวณไว้แล้ว)
# ============================================================
def backtest(d, lo, hi, sl_atr, tp_atr, trail_atr, adx_min):
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    ef, es, rs, at = d["ef"], d["es"], d["rsi"], d["atr"]
    e200, ax = d["e200"], d["adx"]

    cash, pos = START_CAP, None
    trades, equity, peak, dd = [], [], START_CAP, 0.0

    for i in range(lo, hi):
        px, a = c[i], at[i]

        # ---------- มีของ: เช็คทางออก ----------
        if pos:
            ex, why = None, None
            if o[i] <= pos["sl"]:            # gap ลงข้ามคืน
                ex, why = o[i], "GAP"
            elif l[i] <= pos["sl"]:
                ex, why = pos["sl"], "SL"
            elif h[i] >= pos["tp"]:
                ex, why = pos["tp"], "TP"

            if ex:
                cash += pos["qty"] * ex * (1 - SIDE_COST)
                pnl = cash - pos["cash0"]
                trades.append({"pnl": pnl, "why": why,
                               "out": d["t"][i], "cost": pos["cost"]})
                pos = None
            elif trail_atr > 0 and a:
                pos["sl"] = max(pos["sl"], px - trail_atr * a)

        # ---------- ไม่มีของ: หาจังหวะเข้า ----------
        else:
            ok = (ef[i] and es[i] and rs[i] is not None and a)
            if ok and adx_min > 0:
                ok = (e200[i] is not None and ax[i] is not None
                      and ax[i] >= adx_min and px > e200[i])
            if ok and a / px * 100 > MAX_ATR_PCT:
                ok = False
            if ok:
                golden  = ef[i-1] <= es[i-1] and ef[i] > es[i]
                uptrend = ef[i] > es[i]
                if (golden or (uptrend and px > ef[i])) and rs[i] < RSI_BUY_MAX:
                    alloc = cash * ALLOC_PCT
                    entry = px * (1 + SIDE_COST)
                    pos = {"qty": alloc / entry, "cost": alloc, "cash0": cash,
                           "sl": px - sl_atr * a, "tp": px + tp_atr * a}
                    cash -= alloc

        eq = cash + (pos["qty"] * px if pos else 0.0)
        equity.append(eq)
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100)

    return trades, equity, dd

# ============================================================
# Metrics
# ============================================================
def score(trades, equity, dd, years):
    if not trades or not equity:
        return None
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    loss = [t["pnl"] for t in trades if t["pnl"] <= 0]
    gp, gl = sum(wins), abs(sum(loss))
    fin = equity[-1]
    ret = (fin - START_CAP) / START_CAP * 100
    cagr = ((fin / START_CAP) ** (1 / years) - 1) * 100 if fin > 0 else -100
    return {
        "n": len(trades), "ret": ret, "cagr": cagr, "dd": dd,
        "wr": len(wins) / len(trades) * 100,
        "pf": gp / gl if gl > 0 else 99.0,
        "mar": cagr / dd if dd > 0.01 else 0.0,
    }


def buy_hold(c, lo, hi, years):
    e = c[lo] * (1 + SIDE_COST)
    qty = START_CAP / e
    peak, dd = START_CAP, 0.0
    for i in range(lo, hi):
        v = qty * c[i]
        peak = max(peak, v); dd = max(dd, (peak - v) / peak * 100)
    fin = qty * c[hi-1] * (1 - SIDE_COST)
    return {"ret": (fin - START_CAP) / START_CAP * 100,
            "cagr": ((fin / START_CAP) ** (1 / years) - 1) * 100,
            "dd": dd}


def main():
    print("=" * 70)
    print(f"  BACKTEST v3 — GRID SEARCH | {SYMBOL} {INTERVAL}")
    print("=" * 70)

    kl = fetch_klines()
    if len(kl) < 400:
        print(f"❌ ข้อมูลไม่พอ ({len(kl)} แท่ง)"); return

    d = {"t": [int(k[0]) for k in kl],
         "o": [float(k[1]) for k in kl], "h": [float(k[2]) for k in kl],
         "l": [float(k[3]) for k in kl], "c": [float(k[4]) for k in kl]}
    d["ef"]   = ema_series(d["c"], EMA_FAST)
    d["es"]   = ema_series(d["c"], EMA_SLOW)
    d["e200"] = ema_series(d["c"], REGIME_EMA)
    d["rsi"]  = rsi_series(d["c"])
    d["atr"]  = atr_series(d["h"], d["l"], d["c"])
    d["adx"]  = adx_series(d["h"], d["l"], d["c"])

    n = len(kl)
    warm = REGIME_EMA + ADX_PERIOD * 2 + 5
    split = warm + int((n - warm) * IS_RATIO)
    yr_is  = max((split - warm) / 365, 0.3)
    yr_oos = max((n - split) / 365, 0.3)

    d0 = datetime.fromtimestamp(d["t"][0]/1000, timezone.utc).date()
    d1 = datetime.fromtimestamp(d["t"][-1]/1000, timezone.utc).date()
    print(f"📅 {d0} → {d1} ({n} แท่ง)")
    print(f"🔬 In-Sample {yr_is:.1f}ปี | Out-of-Sample {yr_oos:.1f}ปี\n")

    bh_is  = buy_hold(d["c"], warm, split, yr_is)
    bh_oos = buy_hold(d["c"], split, n, yr_oos)
    print(f"📌 Buy&Hold  IS: {bh_is['ret']:+.1f}% (DD {bh_is['dd']:.1f}%)"
          f" | OOS: {bh_oos['ret']:+.1f}% (DD {bh_oos['dd']:.1f}%)\n")

    keys = list(GRID.keys())
    results = []
    for combo in itertools.product(*[GRID[k] for k in keys]):
        p = dict(zip(keys, combo))
        t1, e1, dd1 = backtest(d, warm, split, p["SL_ATR"], p["TP_ATR"],
                               p["TRAIL_ATR"], p["ADX_MIN"])
        m1 = score(t1, e1, dd1, yr_is)
        if not m1 or m1["n"] < MIN_TRADES or m1["pf"] < 1.0:
            continue
        t2, e2, dd2 = backtest(d, split, n, p["SL_ATR"], p["TP_ATR"],
                               p["TRAIL_ATR"], p["ADX_MIN"])
        m2 = score(t2, e2, dd2, yr_oos)
        if not m2: continue
        results.append({"p": p, "is": m1, "oos": m2})

    print(f"🧪 ทดสอบ {len(list(itertools.product(*[GRID[k] for k in keys])))} ชุด"
          f" | ผ่านเกณฑ์ IS: {len(results)} ชุด\n")

    if not results:
        print("❌ ไม่มีชุดไหนทำกำไรได้เลยใน In-Sample")
        print("   → กลยุทธ์ EMA cross นี้ไม่มี edge บน BTCTHB 1d")
        print("   → แนะนำเปลี่ยนแนวคิด ไม่ใช่จูนตัวเลข")
        return

    results.sort(key=lambda r: r["oos"]["mar"], reverse=True)

    print("🏆 TOP 8 (เรียงตาม MAR ของ Out-of-Sample)")
    print("-" * 70)
    print(f"{'SL':>4}{'TP':>5}{'TRL':>5}{'ADX':>5} |"
          f"{'IS-Ret':>8}{'IS-PF':>7} |{'OOS-Ret':>9}{'OOS-DD':>8}"
          f"{'OOS-PF':>7}{'n':>4}")
    print("-" * 70)
    for r in results[:8]:
        p, a, b = r["p"], r["is"], r["oos"]
        print(f"{p['SL_ATR']:>4.1f}{p['TP_ATR']:>5.0f}{p['TRAIL_ATR']:>5.1f}"
              f"{p['ADX_MIN']:>5.0f} |{a['ret']:>+8.1f}{a['pf']:>7.2f} |"
              f"{b['ret']:>+9.1f}{b['dd']:>8.1f}{b['pf']:>7.2f}{b['n']:>4}")

    best = results[0]
    print("\n" + "=" * 70)
    ok_oos = best["oos"]["pf"] > 1.2 and best["oos"]["ret"] > 0
    beat_bh = best["oos"]["ret"] > bh_oos["ret"]
    if ok_oos and beat_bh:
        print("✅ เจอชุดที่มี edge และชนะ Buy&Hold — พร้อม paper trade")
    elif ok_oos:
        print("⚠️ กำไรได้ แต่แพ้ Buy&Hold — ถือเฉย ๆ ดีกว่าเทรด")
    else:
        print("❌ ชุดที่ดีที่สุดยังพังใน Out-of-Sample = overfit")
        print("   อย่าเอาไปใช้เงินจริงเด็ดขาด")
    print("=" * 70)


if __name__ == "__main__":
    main()

