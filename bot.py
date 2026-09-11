# -*- coding: utf-8 -*-
"""
bot.py — Adaptive Spot Bot สำหรับ Binance TH (REST ตรง /api/v1) รันบน GitHub Actions
"""
import os, json, time, hmac, hashlib, logging, traceback
from urllib.parse import urlencode
from datetime import datetime, timezone
import requests

# ================= CONFIG =================
API_KEY    = os.getenv("BINANCE_TH_API_KEY", "")
API_SECRET = os.getenv("BINANCE_TH_API_SECRET", "")
BASE_URL   = "https://api.binance.th"

SYMBOL     = os.getenv("SYMBOL", "BTCTHB")
AMOUNT     = float(os.getenv("AMOUNT", "500"))
INTERVAL   = os.getenv("INTERVAL", "1h")
DRY_RUN    = os.getenv("DRY_RUN", "true").lower() == "true"

# ---------- ค่าธรรมเนียม (ขั้นที่ 1) ----------
FEE_RATE   = float(os.getenv("FEE_RATE", "0.0025"))
SLIPPAGE   = float(os.getenv("SLIPPAGE", "0.0005"))
ROUND_TRIP = FEE_RATE * 2
TOTAL_COST = (FEE_RATE + SLIPPAGE) * 2
MIN_EDGE   = float(os.getenv("MIN_EDGE", "0.005"))
MIN_RR     = float(os.getenv("MIN_RR", "1.5"))

EMA_FAST, EMA_SLOW, RSI_PERIOD, ATR_PERIOD = 10, 30, 14, 14
RSI_BUY_MAX, RSI_EXIT = 70.0, 78.0
ATR_SL_MULT, ATR_TP_MULT, TRAIL_ATR_MULT = 2.0, 8.0, 4.0
MAX_CONSEC_LOSS, COOLDOWN_ROUNDS, MIN_RISK = 4, 6, 0.25
MAX_ATR_PCT = 8.0


REGIME_EMA    = 200
ADX_PERIOD    = 14
ADX_TREND_MIN = 0

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
LOG_FILE   = os.path.join(BASE_DIR, "bot_log.txt")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger("bot")

# ================= FEE HELPERS =================
def net_pct(entry, exit_price):
    """% กำไร/ขาดทุนสุทธิ หลังหักค่าธรรมเนียม+slippage ทั้ง 2 ข้าง"""
    if not entry: return 0.0
    return ((exit_price - entry) / entry - TOTAL_COST) * 100

def net_pnl(entry, exit_price, qty):
    """กำไร/ขาดทุนสุทธิเป็นเงินบาทจริง"""
    cost_in  = entry * qty * (1 + FEE_RATE + SLIPPAGE)
    cash_out = exit_price * qty * (1 - FEE_RATE - SLIPPAGE)
    return cash_out - cost_in

def is_worth_trading(entry, take_profit, stop_loss):
    """เช็คก่อนเข้าไม้ว่าคุ้มค่าธรรมเนียมไหม -> (ผ่านไหม, เหตุผล)"""
    min_tp = entry * (1 + TOTAL_COST + MIN_EDGE)
    if take_profit < min_tp:
        return False, (f"TP {take_profit:,.0f} ต่ำกว่าจุดคุ้มทุน {min_tp:,.0f} "
                       f"(ต้นทุนรอบละ {TOTAL_COST*100:.2f}%)")
    net_win  = (take_profit - entry) / entry - TOTAL_COST
    net_risk = (entry - stop_loss) / entry + TOTAL_COST
    rr = net_win / net_risk if net_risk > 0 else 0
    if rr < MIN_RR:
        return False, f"R:R สุทธิ {rr:.2f} ต่ำเกินไป (ต้อง >= {MIN_RR})"
    return True, f"ผ่าน — R:R สุทธิ {rr:.2f}"

# ================= STATE =================
DEFAULT_STATE = {
    "in_position": False, "entry_price": 0.0, "position_amount": 0.0,
    "stop_loss": 0.0, "take_profit": 0.0, "highest_since_entry": 0.0,
    "entry_time": None, "wins": 0, "losses": 0, "consecutive_losses": 0,
    "consecutive_wins": 0, "risk_factor": 1.0, "cooldown_left": 0,
    "total_pnl": 0.0, "history": [], "last_run": None,
}

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
        s = dict(DEFAULT_STATE); s.update({k: v for k, v in data.items() if k in DEFAULT_STATE})
        return s
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log.warning("อ่าน state ไม่ได้ (%s) -> เริ่มใหม่", e)
        return dict(DEFAULT_STATE)

def save_state(s):
    s["last_run"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

# ================= CLIENT =================
class BinanceTH:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret
        self.s = requests.Session()
        self.s.headers.update({"X-MBX-APIKEY": key, "User-Agent": "gh-bot/1.0"})
        self.offset = 0

    def _sign(self, params):
        q = urlencode(params, doseq=True)
        sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        return q + "&signature=" + sig

    def _req(self, method, path, params=None, signed=False, retries=3):
        params = dict(params or {})
        for attempt in range(1, retries + 1):
            try:
                if signed:
                    params["timestamp"] = int(time.time() * 1000) + self.offset
                    params["recvWindow"] = 10000
                    url = f"{BASE_URL}{path}?{self._sign(params)}"
                    r = self.s.request(method, url, timeout=20)
                else:
                    r = self.s.request(method, BASE_URL + path, params=params, timeout=20)

                if r.status_code in (429, 418):
                    wait = int(r.headers.get("Retry-After", 5 * attempt))
                    log.warning("Rate limit %s -> รอ %ds", r.status_code, wait)
                    time.sleep(wait); continue
                if r.status_code >= 400:
                    log.error("HTTP %s %s -> %s", r.status_code, path, r.text[:300])
                    return None
                return r.json()
            except (requests.Timeout, requests.ConnectionError) as e:
                log.warning("Network error (%d/%d): %s", attempt, retries, e)
                time.sleep(2 * attempt)
            except ValueError as e:
                log.error("JSON ผิดรูปแบบ: %s", e); return None
        log.error("ล้มเหลวครบ %d ครั้ง: %s", retries, path)
        return None

    def sync_time(self):
        d = self._req("GET", "/api/v1/time")
        if d and "serverTime" in d:
            self.offset = int(d["serverTime"]) - int(time.time() * 1000)
            log.info("Time offset = %d ms", self.offset)
            return True
        return False

    def klines(self, symbol, interval, limit=400):
        return self._req("GET", "/api/v1/klines",
                         {"symbol": symbol, "interval": interval, "limit": limit})

    def symbol_info(self, symbol):
        d = self._req("GET", "/api/v1/exchangeInfo", {"symbol": symbol})
        if not d: return None
        syms = d.get("symbols") or []
        for s in syms:
            if s.get("symbol") == symbol: return s
        return syms[0] if syms else None

    def balance(self, asset):
        d = self._req("GET", "/api/v1/account", signed=True)
        if not d: return None
        for b in d.get("balances", []):
            if b.get("asset") == asset: return float(b.get("free", 0))
        return 0.0

    def market_order(self, symbol, side, quantity=None, quote_qty=None):
        p = {"symbol": symbol, "side": side, "type": "MARKET"}
        if quote_qty is not None: p["quoteOrderQty"] = quote_qty
        else: p["quantity"] = quantity
        return self._req("POST", "/api/v1/order", p, signed=True)

# ================= INDICATORS =================
def ema(v, n):
    if len(v) < n: return []
    k = 2 / (n + 1); out = [sum(v[:n]) / n]
    for x in v[n:]: out.append(x * k + out[-1] * (1 - k))
    return out

def rsi(c, n=14):
    if len(c) < n + 1: return None
    g = [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    l = [max(c[i-1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n-1) + g[i]) / n; al = (al * (n-1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

def atr(h, lo, c, n=14):
    if len(c) < n + 1: return None
    tr = [max(h[i]-lo[i], abs(h[i]-c[i-1]), abs(lo[i]-c[i-1])) for i in range(1, len(c))]
    v = sum(tr[:n]) / n
    for t in tr[n:]: v = (v * (n-1) + t) / n
    return v

def adx(highs, lows, closes, period=ADX_PERIOD):
    """ADX วัดความแรงเทรนด์ (ไม่สนทิศทาง) — <20 ไซด์เวย์, >25 เทรนด์ชัด"""
    if len(closes) < period * 2 + 1:
        return None
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
        if str_[i] == 0:
            continue
        pdi, mdi = 100 * sp[i] / str_[i], 100 * sm[i] / str_[i]
        if pdi + mdi > 0:
            dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))
    return sum(dxs[-period:]) / period if len(dxs) >= period else None


def detect_regime(closes, highs, lows):
    """คืนค่า (regime, adx_value, ema200) — BULL / BEAR / SIDEWAYS / UNKNOWN"""
    e200 = ema(closes, REGIME_EMA)
    a = adx(highs, lows, closes)
    if not e200 or a is None:
        return "UNKNOWN", a, None
    price, line = closes[-1], e200[-1]
    if a < ADX_TREND_MIN:
        return "SIDEWAYS", a, line
    return ("BULL" if price > line else "BEAR"), a, line

# ================= HELPERS =================
def step_round(qty, step):
    if not step: return qty
    prec = max(0, str(step).rstrip("0")[::-1].find(".")) if "." in str(step) else 0
    return float(f"{(int(qty / step) * step):.{prec}f}")

def get_filters(info):
    f = {"step": 0.0, "min_qty": 0.0, "min_notional": 0.0}
    for flt in (info or {}).get("filters", []):
        t = flt.get("filterType")
        if t == "LOT_SIZE":
            f["step"] = float(flt.get("stepSize", 0)); f["min_qty"] = float(flt.get("minQty", 0))
        elif t in ("MIN_NOTIONAL", "NOTIONAL"):
            f["min_notional"] = float(flt.get("minNotional", 0))
    return f

def update_risk(s):
    cl, cw = s["consecutive_losses"], s["consecutive_wins"]
    rf = {0: min(1.0, s["risk_factor"] + 0.15 * cw), 1: 0.8, 2: 0.6}.get(cl, 0.4)
    s["risk_factor"] = round(max(MIN_RISK, min(1.0, rf)), 3)
    if cl >= MAX_CONSEC_LOSS and s["cooldown_left"] == 0:
        s["cooldown_left"] = COOLDOWN_ROUNDS
        log.warning("CIRCUIT BREAKER: แพ้ติด %d ไม้ -> พัก %d รอบ", cl, COOLDOWN_ROUNDS)

def record_trade(s, entry, exit_p, qty, reason):
    pnl       = net_pnl(entry, exit_p, qty)
    pct       = net_pct(entry, exit_p)
    gross_pct = (exit_p / entry - 1) * 100 if entry else 0.0

    won = pct > 0
    s["wins" if won else "losses"] += 1
    if won: s["consecutive_wins"] += 1; s["consecutive_losses"] = 0
    else:   s["consecutive_losses"] += 1; s["consecutive_wins"] = 0

    s["total_pnl"] = round(s["total_pnl"] + pnl, 2)
    s["history"] = (s["history"] + [{
        "time": datetime.now(timezone.utc).isoformat(),
        "entry": entry, "exit": exit_p, "qty": qty,
        "pnl": round(pnl, 2),
        "pct": round(pct, 3),
        "gross_pct": round(gross_pct, 3),
        "fee_pct": round(TOTAL_COST * 100, 3),
        "reason": reason}])[-50:]

    update_risk(s)
    log.info("ปิดไม้ [%s] PnL=%.2f | สุทธิ %.3f%% (ก่อนหักค่าธรรมเนียม %.3f%%) | W/L=%d/%d | risk=%.2f",
             reason, pnl, pct, gross_pct, s["wins"], s["losses"], s["risk_factor"])

# ================= MAIN =================
# ============================================================
# 🛡️ Kill Switch — หยุดเข้าไม้ใหม่เมื่อขาดทุนเกินกำหนด
# ============================================================
def kill_switch(s, price):
    """คืน True = ห้ามเข้าไม้ใหม่ (ไม้เดิมยังดูแล SL/TP ต่อ)"""
    if not KILL_ENABLED:
        return False

    if s.get("halted"):
        log.error("⛔ KILL SWITCH ทำงานอยู่ | %s | หยุดเมื่อ %s",
                  s.get("halt_reason", "-"), s.get("halt_time", "-"))
        log.error("   ปลดล็อก: แก้ bot_state.json -> halted = false")
        return True

    realized = float(s.get("total_pnl", 0.0))
    equity   = START_EQUITY + realized

    if s.get("in_position") and price:
        e = float(s.get("entry_price", 0) or 0)
        q = float(s.get("position_amount", 0) or 0)
        if e > 0 and q > 0:
            equity += (price - e) * q

    peak = max(float(s.get("peak_equity", START_EQUITY)), equity)
    s["peak_equity"] = round(peak, 2)
    dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
    s["dd_pct"] = round(dd, 2)

    streak = int(s.get("consec_loss", s.get("loss_streak", 0)) or 0)

    reason = None
    if dd >= KILL_MAX_DD:
        reason = f"Drawdown {dd:.2f}% >= {KILL_MAX_DD:.0f}%"
    elif KILL_MAX_LOSS > 0 and realized <= -KILL_MAX_LOSS:
        reason = f"ขาดทุนสะสม {realized:,.0f} บาท"
    elif streak >= KILL_MAX_STREAK:
        reason = f"แพ้ติดกัน {streak} ไม้"

    if reason:
        s["halted"]      = True
        s["halt_reason"] = reason
        s["halt_time"]   = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log.error("=" * 52)
        log.error("⛔ KILL SWITCH ทำงาน: %s", reason)
        log.error("   Equity %s | Peak %s | DD %.2f%%",
                  f"{equity:,.0f}", f"{peak:,.0f}", dd)
        log.error("   หยุดเข้าไม้ใหม่ (ไม้เดิมยังดูแลต่อ)")
        log.error("=" * 52)
        return True

    log.info("🛡️ Equity=%s | Peak=%s | DD=%.2f%% (ลิมิต %.0f%%) | แพ้ติด %d",
             f"{equity:,.0f}", f"{peak:,.0f}", dd, KILL_MAX_DD, streak)
    return False


def main():
    log.info("=" * 60)
    log.info("เริ่มรอบ | %s | TF=%s | DRY_RUN=%s", SYMBOL, INTERVAL, DRY_RUN)
    log.info("ต้นทุนต่อรอบ: %.2f%% (fee %.2f%% + slip %.2f%%) | TP ขั้นต่ำต้อง +%.2f%%",
             TOTAL_COST * 100, ROUND_TRIP * 100, SLIPPAGE * 2 * 100,
             (TOTAL_COST + MIN_EDGE) * 100)
    
    s = load_state()

    if not API_KEY or not API_SECRET:
        if not DRY_RUN:
            log.error("ไม่พบ API Key/Secret -> หยุด"); return
        log.warning("ไม่มี key แต่ DRY_RUN=true -> รันเฉพาะข้อมูลสาธารณะ")

    if s["cooldown_left"] > 0 and not s["in_position"]:
        s["cooldown_left"] -= 1
        log.warning("พักเทรด เหลือ %d รอบ", s["cooldown_left"])
        save_state(s); return

    api = BinanceTH(API_KEY, API_SECRET)
    if API_KEY: api.sync_time()

    info = api.symbol_info(SYMBOL)
    if not info:
        log.error("ดึง exchangeInfo ไม่ได้ -> ข้ามรอบ"); save_state(s); return
    filt = get_filters(info)

    raw = api.klines(SYMBOL, INTERVAL, 400)
    if not raw or len(raw) < 50:
        log.error("แท่งเทียนไม่พอ -> ข้ามรอบ"); save_state(s); return

    raw = raw[:-1]
    highs  = [float(k[2]) for k in raw]
    lows   = [float(k[3]) for k in raw]
    closes = [float(k[4]) for k in raw]

    regime, adx_val, ema200 = detect_regime(closes, highs, lows)
    log.info("สภาวะตลาด: %s | ADX=%s | EMA200=%s",
             regime,
             f"{adx_val:.1f}" if adx_val else "N/A",
             f"{ema200:,.2f}" if ema200 else "N/A")

    price  = closes[-1]

    ef, es = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    r, a = rsi(closes, RSI_PERIOD), atr(highs, lows, closes, ATR_PERIOD)
    if not ef or not es or r is None or not a:
        log.error("คำนวณอินดิเคเตอร์ไม่ได้ -> ข้ามรอบ"); save_state(s); return

    f_now, s_now, f_prev, s_prev = ef[-1], es[-1], ef[-2], es[-2]
    atr_pct = a / price * 100
    log.info("ราคา=%.2f | EMA%d=%.2f EMA%d=%.2f | RSI=%.1f | ATR=%.2f (%.2f%%) | risk=%.2f",
             price, EMA_FAST, f_now, EMA_SLOW, s_now, r, a, atr_pct, s["risk_factor"])

    golden = f_prev <= s_prev and f_now > s_now
    death  = f_prev >= s_prev and f_now < s_now

    # ---------- ถือของอยู่ ----------
    if s["in_position"]:
        entry, qty = s["entry_price"], s["position_amount"]
        s["highest_since_entry"] = max(s["highest_since_entry"], price)
        new_sl = s["highest_since_entry"] - a * TRAIL_ATR_MULT
        if new_sl > s["stop_loss"]:
            log.info("เลื่อน SL %.2f -> %.2f", s["stop_loss"], new_sl); s["stop_loss"] = new_sl

        reason = ("STOP_LOSS"   if price <= s["stop_loss"]   else
                  "TAKE_PROFIT" if price >= s["take_profit"] else
                  "DEATH_CROSS" if death else
                  "RSI_HIGH"    if r >= RSI_EXIT else None)

        if reason:
            q = step_round(qty, filt["step"])
            if DRY_RUN:
                log.info("[DRY] SELL %s qty=%.8f @ %.2f (%s)", SYMBOL, q, price, reason)
                record_trade(s, entry, price, q, reason)
            else:
                free = api.balance(info.get("baseAsset", SYMBOL.replace("THB", "")))
                if free is not None: q = step_round(min(q, free), filt["step"])
                o = api.market_order(SYMBOL, "SELL", quantity=q)
                if o:
                    ex_p = float(o.get("cummulativeQuoteQty", 0)) / float(o.get("executedQty", 1) or 1) or price
                    log.info("SELL สำเร็จ id=%s @ %.2f", o.get("orderId"), ex_p)
                    record_trade(s, entry, ex_p, float(o.get("executedQty", q)), reason)
                else:
                    log.error("ส่งคำสั่งขายไม่สำเร็จ -> คงสถานะเดิม"); save_state(s); return
            s.update({"in_position": False, "entry_price": 0.0, "position_amount": 0.0,
                      "stop_loss": 0.0, "take_profit": 0.0, "highest_since_entry": 0.0,
                      "entry_time": None})
        else:
            u_pct = net_pct(entry, price)
            log.info("ถือต่อ | entry=%.2f SL=%.2f TP=%.2f | uPnL=%.2f (สุทธิ %.2f%%)",
                     entry, s["stop_loss"], s["take_profit"],
                     net_pnl(entry, price, qty), u_pct)

    # ---------- ยังไม่มีของ ----------
    else:
        if kill_switch(s, price):
            save_state(s); return

        uptrend = f_now > s_now

        if ADX_TREND_MIN > 0 and regime == "BEAR":
            log.info("🔴 ขาลง (ราคาต่ำกว่า EMA200) → ถือเงินสด")
            save_state(s); return
        if ADX_TREND_MIN > 0 and regime == "SIDEWAYS":
            log.info("🔵 ไซด์เวย์ (ADX<%.0f) → รอเทรนด์ชัด", ADX_TREND_MIN)
            save_state(s); return
        if ADX_TREND_MIN > 0 and regime == "UNKNOWN":
            log.info("❓ ข้อมูลไม่พอคำนวณ regime → ข้ามรอบนี้")
            save_state(s); return

        signal = (golden or (uptrend and price > f_now)) and r < RSI_BUY_MAX
        cost = round(AMOUNT * s["risk_factor"], 2)

        plan_sl = price - a * ATR_SL_MULT
        plan_tp = price + a * ATR_TP_MULT

        if not signal:
            log.info("ไม่มีสัญญาณเข้า (golden=%s uptrend=%s RSI=%.1f)", golden, uptrend, r)
        elif atr_pct > MAX_ATR_PCT:
            log.warning("ผันผวนสูง ATR %.2f%% -> งดเข้า", atr_pct)
        elif filt["min_notional"] and cost < filt["min_notional"]:
            log.warning("มูลค่า %.2f < ขั้นต่ำ %.2f -> ข้าม", cost, filt["min_notional"])
        else:
            ok, why = is_worth_trading(price, plan_tp, plan_sl)
            if not ok:
                log.info("ข้ามไม้นี้: %s", why)
                save_state(s)
                tot = s["wins"] + s["losses"]
                log.info("สรุป: %d ไม้ | Winrate %.1f%% | PnL %.2f บาท",
                         tot, (s["wins"] / tot * 100 if tot else 0), s["total_pnl"])
                return
            log.info("เข้าไม้ได้: %s", why)

            qty_est = step_round(cost / price, filt["step"])
            if DRY_RUN:
                log.info("[DRY] BUY %s %.2f THB @ %.2f (qty≈%.8f)", SYMBOL, cost, price, qty_est)
                entry_p, got = price, qty_est
            else:
                o = api.market_order(SYMBOL, "BUY", quote_qty=cost)
                if not o:
                    log.warning("quoteOrderQty ไม่ผ่าน -> ลองใช้ quantity")
                    o = api.market_order(SYMBOL, "BUY", quantity=qty_est)
                if not o:
                    log.error("ซื้อไม่สำเร็จ -> ข้ามรอบ"); save_state(s); return
                got = float(o.get("executedQty", qty_est))
                spent = float(o.get("cummulativeQuoteQty", cost))
                entry_p = spent / got if got else price
                log.info("BUY สำเร็จ id=%s qty=%.8f @ %.2f", o.get("orderId"), got, entry_p)

            s.update({"in_position": True, "entry_price": entry_p, "position_amount": got,
                      "stop_loss": entry_p - a * ATR_SL_MULT,
                      "take_profit": entry_p + a * ATR_TP_MULT,
                      "highest_since_entry": entry_p,
                      "entry_time": datetime.now(timezone.utc).isoformat()})
            log.info("เปิดไม้ | entry=%.2f SL=%.2f TP=%.2f", entry_p, s["stop_loss"], s["take_profit"])

    save_state(s)
    tot = s["wins"] + s["losses"]
    log.info("สรุป: %d ไม้ | Winrate %.1f%% | PnL %.2f บาท",
             tot, (s["wins"] / tot * 100 if tot else 0), s["total_pnl"])

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.critical("ผิดพลาดร้ายแรง: %s\n%s", e, traceback.format_exc())
    finally:
        log.info("จบรอบ\n")
