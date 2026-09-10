# ============================================================
# backtest_v2.py — วัดผล Regime Filter (ADX + EMA200)
# รันเทียบ: กรอง ON vs OFF บนข้อมูลชุดเดียวกัน
# ============================================================
import json, time, urllib.request, urllib.parse
from datetime import datetime, timezone

# ---------- ⚙️ ต้องตรงกับ bot.py ----------
BASE_URL      = "https://api.binance.th"
SYMBOL        = "BTCTHB"
INTERVAL      = "1d"

EMA_FAST      = 10
EMA_SLOW      = 30
RSI_PERIOD    = 14
RSI_BUY_MAX   = 70.0
ATR_PERIOD    = 14
MAX_ATR_PCT   = 8.0

SL_ATR        = 1.5      # SL = entry - 1.5*ATR
TP_ATR        = 4.0      # TP = entry + 4.0*ATR
TRAIL_ATR     = 1.5      # trailing stop

TOTAL_COST    = 0.60     # % ไป-กลับ (fee 0.50 + slip 0.10)
SIDE_COST     = TOTAL_COST / 2 / 100   # ต่อขา

REGIME_EMA    = 200
ADX_PERIOD    = 14
ADX_TREND_MIN = 20.0

START_CAP     = 100_000.0
AMOUNT        = 10_000.0
USE_RISK_STEP = True     # ลดไม้เหลือ 0.6 หลังแพ้ 2 ไม้ติด
YEARS_BACK    = 5

# ============================================================
# Indicators (copy มาจาก bot.py — อย่า import ตรง!)
# ============================================================
def ema(vals, period):
    if len(vals) < period: return None
    k = 2 / (period + 1)
    out = [sum(vals[:period]) / period]
    for v in vals[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0: return 100.0
    rs = ag / al
    return 100 - (100 / (1 + rs))


def atr(highs, lows, closes, period=ATR_PERIOD):
    if len(closes) < period + 1: return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i-1]),
                       abs(lows[i] - closes[i-1])))
    a = sum(trs[:period]) / period
    for t in trs[period:]:
        a = (a * (period - 1) + t) / period
    return a


def adx(highs, lows, closes, period=ADX_PERIOD):
    if len(closes) < period * 2 + 1: return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(closes)):
        up, dn = highs[i] - highs[i-1], lows[i-1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i-1]),
                       abs(lows[i] - closes[i-1])))

    def smooth(x):
        s = [sum(x[:period])]
        for v in x[period:]:
            s.append(s[-1] - s[-1] / period + v)
        return s

    str_, sp, sm = smooth(trs), smooth(plus_dm), smooth(minus_dm)
    dxs = []
    for i in range(len(str_)):
        if str_[i] == 0: continue
        pdi, mdi = 100 * sp[i] / str_[i], 100 * sm[i] / str_[i]
        if pdi + mdi > 0:
            dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))
    return sum(dxs[-period:]) / period if len(dxs) >= period else None


def detect_regime(closes, highs, lows):
    e200 = ema(closes, REGIME_EMA)
    a = adx(highs, lows, closes)
    if not e200 or a is None:
        return "UNKNOWN", a, None
    price, line = closes[-1], e200[-1]
    if a < ADX_TREND_MIN:
        return "SIDEWAYS", a, line
    return ("BULL" if price > line else "BEAR"), a, line

# ============================================================
# ดึงแท่งเทียนย้อนหลังหลายปี (pagination)
# ============================================================
def fetch_klines(symbol=SYMBOL, interval=INTERVAL, years=YEARS_BACK):
    MS_DAY = 86_400_000
    end_ms = int(time.time() * 1000)
    cur    = end_ms - years * 365 * MS_DAY
    out    = []

    while cur < end_ms:
        q = urllib.parse.urlencode({
            "symbol": symbol, "interval": interval,
            "startTime": cur, "limit": 1000
        })
        url = f"{BASE_URL}/api/v1/klines?{q}"
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                batch = json.loads(r.read().decode())
        except Exception as e:
            print(f"❌ ดึงข้อมูลล้มเหลว: {e}")
            break
        if not batch:
            break
        out += batch
        nxt = int(batch[-1][0]) + 1
        if nxt <= cur:
            break
        cur = nxt
        if len(batch) < 1000:
            break
        time.sleep(0.3)

    seen, uniq = set(), []
    for k in out:
        if k[0] not in seen:
            seen.add(k[0]); uniq.append(k)
    uniq.sort(key=lambda k: k[0])
    return uniq[:-1]      # ตัดแท่งที่ยังไม่ปิด

# ============================================================
# Backtest Engine
# ============================================================
def run_backtest(kl, use_regime=True):
    times  = [int(k[0]) for k in kl]
    highs  = [float(k[2]) for k in kl]
    lows   = [float(k[3]) for k in kl]
    closes = [float(k[4]) for k in kl]

    cash, pos = START_CAP, None
    trades, equity = [], []
    loss_streak, risk = 0, 1.0
    skipped = {"BEAR": 0, "SIDEWAYS": 0, "UNKNOWN": 0, "ATR": 0}

    warmup = max(REGIME_EMA, EMA_SLOW, ADX_PERIOD * 2) + 5

    for i in range(warmup, len(closes)):
        c, h, l = closes[i], highs[i], lows[i]

        # ---------- ถือของอยู่: เช็ค SL/TP ----------
        if pos:
            exit_px, reason = None, None
            if l <= pos["sl"]:
                exit_px, reason = pos["sl"], "SL"
            elif h >= pos["tp"]:
                exit_px, reason = pos["tp"], "TP"

            if exit_px:
                gross = pos["qty"] * exit_px
                cash += gross * (1 - SIDE_COST)
                pnl = cash - pos["cash_before"]
                trades.append({
                    "in": pos["time"], "out": times[i],
                    "entry": pos["entry"], "exit": exit_px,
                    "pnl": pnl, "pct": pnl / pos["cost"] * 100,
                    "reason": reason
                })
                loss_streak = loss_streak + 1 if pnl < 0 else 0
                risk = 0.6 if (USE_RISK_STEP and loss_streak >= 2) else 1.0
                pos = None
            else:
                a_now = atr(highs[:i+1], lows[:i+1], closes[:i+1])
                if a_now:
                    new_sl = c - TRAIL_ATR * a_now
                    if new_sl > pos["sl"]:
                        pos["sl"] = new_sl

        # ---------- ไม่มีของ: หาจังหวะเข้า ----------
        else:
            sub_c = closes[:i+1]; sub_h = highs[:i+1]; sub_l = lows[:i+1]

            if use_regime:
                regime, _, _ = detect_regime(sub_c, sub_h, sub_l)
                if regime != "BULL":
                    skipped[regime] = skipped.get(regime, 0) + 1
                    equity.append(cash); continue

            ef = ema(sub_c, EMA_FAST); es = ema(sub_c, EMA_SLOW)
            r  = rsi(sub_c); a = atr(sub_h, sub_l, sub_c)
            if not ef or not es or r is None or not a:
                equity.append(cash); continue

            if a / c * 100 > MAX_ATR_PCT:
                skipped["ATR"] += 1
                equity.append(cash); continue

            f_now, s_now = ef[-1], es[-1]
            golden = (len(ef) > 1 and len(es) > 1 and
                      ef[-2] <= es[-2] and f_now > s_now)
            uptrend = f_now > s_now
            signal = (golden or (uptrend and c > f_now)) and r < RSI_BUY_MAX

            if signal:
                cost = AMOUNT * risk
                if cost <= cash:
                    entry = c * (1 + SIDE_COST)
                    pos = {
                        "entry": entry, "qty": cost / entry,
                        "sl": c - SL_ATR * a, "tp": c + TP_ATR * a,
                        "time": times[i], "cost": cost,
                        "cash_before": cash
                    }
                    cash -= cost

        mtm = cash + (pos["qty"] * c if pos else 0)
        equity.append(mtm)

    return {"trades": trades, "equity": equity,
            "final": equity[-1] if equity else START_CAP,
            "skipped": skipped}

# ============================================================
# วัดผล
# ============================================================
def metrics(res, years):
    t = res["trades"]
    eq = res["equity"]
    if not t:
        return {"trades": 0, "ret": 0, "cagr": 0, "dd": 0,
                "wr": 0, "pf": 0, "exp": 0}

    wins = [x["pnl"] for x in t if x["pnl"] > 0]
    loss = [x["pnl"] for x in t if x["pnl"] <= 0]
    gp, gl = sum(wins), abs(sum(loss))

    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak * 100)

    ret = (res["final"] - START_CAP) / START_CAP * 100
    cagr = ((res["final"] / START_CAP) ** (1 / years) - 1) * 100

    return {
        "trades": len(t), "ret": ret, "cagr": cagr, "dd": dd,
        "wr": len(wins) / len(t) * 100,
        "pf": gp / gl if gl > 0 else float("inf"),
        "exp": sum(x["pnl"] for x in t) / len(t),
    }


def yearly(res):
    buckets = {}
    for x in res["trades"]:
        y = datetime.fromtimestamp(x["out"] / 1000, timezone.utc).year
        buckets.setdefault(y, []).append(x["pnl"])
    return {y: (len(v), sum(v),
                sum(1 for p in v if p > 0) / len(v) * 100)
            for y, v in sorted(buckets.items())}


def main():
    print("=" * 62)
    print(f"  BACKTEST v2 — {SYMBOL} {INTERVAL} | ย้อนหลัง {YEARS_BACK} ปี")
    print("=" * 62)

    kl = fetch_klines()
    if len(kl) < 300:
        print(f"❌ ข้อมูลไม่พอ ({len(kl)} แท่ง) — ต้องการอย่างน้อย 300")
        return

    d0 = datetime.fromtimestamp(kl[0][0] / 1000, timezone.utc).date()
    d1 = datetime.fromtimestamp(kl[-1][0] / 1000, timezone.utc).date()
    yrs = max((d1 - d0).days / 365, 0.5)
    print(f"📅 {d0} → {d1}  ({len(kl)} แท่ง / {yrs:.1f} ปี)\n")

    off = run_backtest(kl, use_regime=False)
    on  = run_backtest(kl, use_regime=True)
    m_off, m_on = metrics(off, yrs), metrics(on, yrs)

    print(f"{'ตัวชี้วัด':<18}{'ไม่กรอง':>14}{'กรอง Regime':>16}")
    print("-" * 50)
    rows = [
        ("จำนวนไม้",   f"{m_off['trades']}",        f"{m_on['trades']}"),
        ("ผลตอบแทน %", f"{m_off['ret']:+.2f}",      f"{m_on['ret']:+.2f}"),
        ("CAGR %",     f"{m_off['cagr']:+.2f}",     f"{m_on['cagr']:+.2f}"),
        ("Max DD %",   f"{m_off['dd']:.2f}",        f"{m_on['dd']:.2f}"),
        ("Winrate %",  f"{m_off['wr']:.1f}",        f"{m_on['wr']:.1f}"),
        ("Profit Factor", f"{m_off['pf']:.2f}",     f"{m_on['pf']:.2f}"),
        ("กำไร/ไม้ (บาท)", f"{m_off['exp']:+,.0f}", f"{m_on['exp']:+,.0f}"),
    ]
    for a, b, c in rows:
        print(f"{a:<18}{b:>14}{c:>16}")

    print("\n🚫 ไม้ที่ถูกกรองออก:")
    for k, v in on["skipped"].items():
        if v: print(f"   {k:<10} {v} วัน")

    print("\n📆 ผลรายปี (โหมดกรอง):")
    for y, (n, pnl, wr) in yearly(on).items():
        print(f"   {y}: {n:>2} ไม้ | PnL {pnl:>+10,.0f} | WR {wr:.0f}%")

    verdict = "✅ กรองแล้วดีขึ้น" if (m_on["dd"] < m_off["dd"] and
                                    m_on["cagr"] >= m_off["cagr"] * 0.8) \
              else "⚠️ กรองแล้วแย่ลง — ลองลด ADX_TREND_MIN"
    print(f"\n{verdict}")


if __name__ == "__main__":
    main()
