# -*- coding: utf-8 -*-
"""
backtest.py — ทดสอบกลยุทธ์ย้อนหลังด้วยตรรกะเดียวกับ bot.py
รันในเครื่อง: python backtest.py
"""
import requests, itertools
from bot import (ema, rsi, atr, FEE_RATE, SLIPPAGE, TOTAL_COST, MIN_EDGE,
                 EMA_FAST, EMA_SLOW, RSI_PERIOD, ATR_PERIOD,
                 RSI_BUY_MAX, RSI_EXIT, MAX_ATR_PCT, SYMBOL)

BASE_URL = "https://api.binance.th"


def fetch_klines(symbol="BTCTHB", interval="1d", limit=1000):
    """ดึงแท่งเทียนย้อนหลังจาก Binance TH"""
    r = requests.get(f"{BASE_URL}/api/v1/klines",
                     params={"symbol": symbol, "interval": interval, "limit": limit},
                     timeout=30)
    r.raise_for_status()
    raw = r.json()[:-1]                      # ตัดแท่งยังไม่ปิด
    return {
        "high":  [float(k[2]) for k in raw],
        "low":   [float(k[3]) for k in raw],
        "close": [float(k[4]) for k in raw],
        "time":  [int(k[0]) for k in raw],
    }


def run(d, sl_mult, tp_mult, trail_mult, min_rr=1.5, verbose=False):
    """จำลองการเทรดทีละแท่ง ใช้ตรรกะเดียวกับ bot.py เป๊ะ"""
    closes, highs, lows = d["close"], d["high"], d["low"]
    n = len(closes)
    warmup = max(EMA_SLOW, RSI_PERIOD, ATR_PERIOD) + 5

    cash, pos, entry, sl, tp, peak = 10000.0, 0.0, 0.0, 0.0, 0.0, 0.0
    trades, equity, skipped = [], [], 0

    for i in range(warmup, n):
        c  = closes[:i + 1]
        price = c[-1]
        ef, es = ema(c, EMA_FAST), ema(c, EMA_SLOW)
        r = rsi(c, RSI_PERIOD)
        a = atr(highs[:i + 1], lows[:i + 1], c, ATR_PERIOD)
        if not ef or not es or r is None or not a or len(ef) < 2 or len(es) < 2:
            continue

        f_now, s_now, f_prev, s_prev = ef[-1], es[-1], ef[-2], es[-2]
        death   = f_prev >= s_prev and f_now < s_now
        golden  = f_prev <= s_prev and f_now > s_now
        uptrend = f_now > s_now
        atr_pct = a / price * 100

        # ---------- ถือของอยู่ ----------
        if pos > 0:
            peak = max(peak, price)
            sl = max(sl, peak - a * trail_mult)          # trailing stop
            reason = ("STOP_LOSS"   if price <= sl else
                      "TAKE_PROFIT" if price >= tp else
                      "DEATH_CROSS" if death else
                      "RSI_HIGH"    if r >= RSI_EXIT else None)
            if reason:
                cash += price * pos * (1 - FEE_RATE - SLIPPAGE)
                net = (price - entry) / entry - TOTAL_COST
                trades.append({"pct": net * 100, "reason": reason})
                if verbose:
                    print(f"  [{reason:12}] {entry:>12,.0f} -> {price:>12,.0f} = {net*100:+6.2f}%")
                pos = 0.0

        # ---------- ยังไม่มีของ ----------
        else:
            signal = (golden or (uptrend and price > f_now)) and r < RSI_BUY_MAX
            if signal and atr_pct <= MAX_ATR_PCT:
                plan_sl, plan_tp = price - a * sl_mult, price + a * tp_mult
                net_win  = (plan_tp - price) / price - TOTAL_COST
                net_risk = (price - plan_sl) / price + TOTAL_COST
                rr = net_win / net_risk if net_risk > 0 else 0
                min_tp = price * (1 + TOTAL_COST + MIN_EDGE)

                if plan_tp >= min_tp and rr >= min_rr:
                    pos = cash / (price * (1 + FEE_RATE + SLIPPAGE))
                    cash = 0.0
                    entry, sl, tp, peak = price, plan_sl, plan_tp, price
                else:
                    skipped += 1

        equity.append(cash + pos * price)

    # ---------- สรุปผล ----------
    if not trades:
        return {"trades": 0, "skipped": skipped}

    pcts   = [t["pct"] for t in trades]
    wins   = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p <= 0]

    peak_eq, max_dd = equity[0], 0.0
    for e in equity:
        peak_eq = max(peak_eq, e)
        max_dd = max(max_dd, (peak_eq - e) / peak_eq)

    return {
        "trades":        len(trades),
        "skipped":       skipped,
        "winrate":       round(len(wins) / len(trades) * 100, 1),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else 99.0,
        "avg_trade":     round(sum(pcts) / len(pcts), 3),
        "best":          round(max(pcts), 2),
        "worst":         round(min(pcts), 2),
        "max_dd":        round(max_dd * 100, 1),
        "total_return":  round((equity[-1] / 10000 - 1) * 100, 1),
        "reasons":       {r: sum(1 for t in trades if t["reason"] == r)
                          for r in set(t["reason"] for t in trades)},
    }


def verdict(res):
    """ตัดสินว่าผ่านเกณฑ์ขั้นต่ำไหม"""
    checks = [
        ("จำนวนเทรด >= 30",   res.get("trades", 0) >= 30),
        ("Profit Factor > 1.5", res.get("profit_factor", 0) > 1.5),
        ("Max DD < 25%",       res.get("max_dd", 99) < 25),
        ("Avg trade > 0.5%",   res.get("avg_trade", 0) > 0.5),
    ]
    print("\n  เกณฑ์ตัดสิน:")
    for name, ok in checks:
        print(f"    {'✅' if ok else '❌'} {name}")
    return all(ok for _, ok in checks)


if __name__ == "__main__":
    print(f"กำลังดึงข้อมูล {SYMBOL} TF=1d ...")
    d = fetch_klines(SYMBOL, "1d", 1000)
    print(f"ได้ {len(d['close'])} แท่ง | ต้นทุนต่อรอบ {TOTAL_COST*100:.2f}%\n")

    # ---- 1) ทดสอบค่าปัจจุบัน ----
    print("=" * 62)
    print("ค่าปัจจุบัน: SL=1.5  TP=4.0  TRAIL=2.0")
    print("=" * 62)
    base = run(d, 1.5, 4.0, 2.0, verbose=True)
    for k, v in base.items():
        print(f"  {k:15}: {v}")
    verdict(base)

    # ---- 2) ไล่หาค่าที่ดีกว่า ----
    print("\n" + "=" * 62)
    print("ทดสอบพารามิเตอร์หลายชุด (เรียงตาม Profit Factor)")
    print("=" * 62)
    print(f"{'SL':>4} {'TP':>4} {'TR':>4} | {'ไม้':>4} {'WR%':>6} {'PF':>6} {'Avg%':>7} {'DD%':>6} {'Ret%':>8}")
    print("-" * 62)

    results = []
    for sl_m, tp_m, tr_m in itertools.product([1.0, 1.5, 2.0, 2.5],
                                              [3.0, 4.0, 5.0, 6.0],
                                              [1.5, 2.0, 3.0]):
        res = run(d, sl_m, tp_m, tr_m)
        if res["trades"] >= 10:
            results.append((sl_m, tp_m, tr_m, res))

    results.sort(key=lambda x: x[3]["profit_factor"], reverse=True)
    for sl_m, tp_m, tr_m, r in results[:15]:
        print(f"{sl_m:>4} {tp_m:>4} {tr_m:>4} | {r['trades']:>4} {r['winrate']:>6} "
              f"{r['profit_factor']:>6} {r['avg_trade']:>7} {r['max_dd']:>6} {r['total_return']:>8}")

    if results:
        best = results[0]
        print(f"\n🏆 ชุดที่ดีที่สุด: ATR_SL_MULT={best[0]}, ATR_TP_MULT={best[1]}, TRAIL_ATR_MULT={best[2]}")
        verdict(best[3])
