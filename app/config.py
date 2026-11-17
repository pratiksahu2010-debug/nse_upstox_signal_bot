"""All settings and every strict threshold live here.

Every number can be overridden with an environment variable of the same name
(upper-case), so thresholds can be tuned on Render without touching code.
"""
import os
from dataclasses import dataclass
from datetime import time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def _env(name, default):
    v = os.getenv(name)
    return default if v is None or v == "" else v


def _f(name, default):
    return float(_env(name, default))


def _i(name, default):
    return int(float(_env(name, default)))


def _b(name, default):
    return str(_env(name, default)).strip().lower() in ("1", "true", "yes", "on")


def _t(name, default):
    h, m = str(_env(name, default)).split(":")
    return time(int(h), int(m))


def _pair(name, default):
    a, b = str(_env(name, default)).split(",")
    return float(a), float(b)


# --------------------------------------------------------------------------- #
# Secrets / infrastructure
# --------------------------------------------------------------------------- #
UPSTOX_TOKEN = _env("UPSTOX_ACCESS_TOKEN", "")          # Analytics token works (1 year)
TELEGRAM_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")
DATABASE_URL = _env("DATABASE_URL", "")                  # Render Postgres internal URL
ADMIN_TOKEN = _env("ADMIN_TOKEN", "change-me")           # protects /force /stats /diag
KEEPALIVE = _b("KEEPALIVE", "1")                         # self-ping so the free dyno stays awake
RUN_SCHEDULER = _b("RUN_SCHEDULER", "1")
DRY_RUN = _b("DRY_RUN", "0")                             # 1 = log signals, do not send Telegram

# Modules on/off
ENABLE_NORMAL = _b("ENABLE_NORMAL", "1")
ENABLE_ORB = _b("ENABLE_ORB", "1")
ENABLE_MCX = _b("ENABLE_MCX", "1")


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Engine:
    candle_min: int = 5
    warm_sessions: int = 6                 # prior sessions kept for indicator warm-up
    fetch_delay_sec: int = _i("FETCH_DELAY_SEC", 6)   # wait after candle close before fetching
    stale_max_bars: int = _i("STALE_MAX_BARS", 1)     # signal bar must be newest or 1 bar old
    workers: int = _i("WORKERS", 6)
    retry_passes: int = 2
    max_bars_kept: int = 900
    # Upstox standard limits: 50/s, 500/min, 2000/30min. We stay well below.
    rate_per_sec: int = _i("RATE_PER_SEC", 8)
    rate_per_min: int = _i("RATE_PER_MIN", 400)
    rate_per_30min: int = _i("RATE_PER_30MIN", 1700)
    prewarm_nse: time = _t("PREWARM_NSE", "08:40")
    prewarm_mcx: time = _t("PREWARM_MCX", "08:30")
    heartbeat_min: int = _i("HEARTBEAT_MIN", 60)      # 0 disables diagnostics heartbeat
    eod_summary: bool = _b("EOD_SUMMARY", "1")


ENGINE = Engine()


# --------------------------------------------------------------------------- #
# Shared indicator parameters
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Ind:
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_n: int = 14
    adx_n: int = 14
    atr_n: int = 14


IND = Ind()


# --------------------------------------------------------------------------- #
# Module 1 - NORMAL (50 liquid F&O stocks + indices): trend + pullback / fresh cross
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NormalCfg:
    first_signal: time = _t("N_FIRST_SIGNAL", "09:45")   # compared with candle CLOSE time
    last_entry: time = _t("N_LAST_ENTRY", "14:45")
    session_end: time = _t("N_SESSION_END", "15:15")
    chop_start: time = _t("N_CHOP_START", "12:00")
    chop_end: time = _t("N_CHOP_END", "13:15")
    chop_adx_min: float = _f("N_CHOP_ADX_MIN", 20)
    chop_rvol_min: float = _f("N_CHOP_RVOL_MIN", 1.5)

    adx_min: float = _f("N_ADX_MIN", 17)                  # your minimum
    di_spread_min: float = _f("N_DI_SPREAD_MIN", 4)
    rsi_long: tuple = _pair("N_RSI_LONG", "55,75")
    rsi_short: tuple = _pair("N_RSI_SHORT", "25,45")
    rvol_min: float = _f("N_RVOL_MIN", 1.2)
    atr_pct_min: float = _f("N_ATR_PCT_MIN", 0.10)
    atr_pct_max: float = _f("N_ATR_PCT_MAX", 2.0)
    ema_sep_min_atr: float = _f("N_EMA_SEP_MIN_ATR", 0.10)
    max_crosses_20: int = _i("N_MAX_CROSSES_20", 2)
    cross_max_age: int = _i("N_CROSS_MAX_AGE", 3)
    pullback_lookback: int = _i("N_PULLBACK_LOOKBACK", 6)

    body_min: float = _f("N_BODY_MIN", 0.50)              # body / range
    close_pos_min: float = _f("N_CLOSE_POS_MIN", 0.70)    # close in top (or bottom) 30 %
    range_max_atr: float = _f("N_RANGE_MAX_ATR", 2.0)     # reject exhaustion candles
    ext_vwap_max_atr: float = _f("N_EXT_VWAP_MAX_ATR", 1.5)
    ext_ema9_max_atr: float = _f("N_EXT_EMA9_MAX_ATR", 1.2)

    htf_gate: bool = _b("N_HTF_GATE", "1")
    bias_gate: bool = _b("N_BIAS_GATE", "1")
    min_score: int = _i("N_MIN_SCORE", 65)

    sl_swing_bars: int = _i("N_SL_SWING_BARS", 6)
    sl_buffer_atr: float = _f("N_SL_BUFFER_ATR", 0.10)
    sl_min_atr: float = _f("N_SL_MIN_ATR", 0.60)
    sl_max_atr: float = _f("N_SL_MAX_ATR", 1.80)          # wider than this = skip, not widen
    tp1_r: float = _f("N_TP1_R", 1.2)
    tp2_r: float = _f("N_TP2_R", 2.2)

    cooldown_bars: int = _i("N_COOLDOWN_BARS", 12)
    daily_cap: int = _i("N_DAILY_CAP", 12)
    per_cycle_top: int = _i("N_PER_CYCLE_TOP", 4)


# --------------------------------------------------------------------------- #
# Module 2 - ORB (100 separate F&O symbols): opening-range breakout / retest
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OrbCfg:
    or_start: time = _t("O_OR_START", "09:15")
    or_end: time = _t("O_OR_END", "09:30")
    first_signal: time = _t("O_FIRST_SIGNAL", "09:35")
    last_entry: time = _t("O_LAST_ENTRY", "11:15")
    session_end: time = _t("O_SESSION_END", "15:15")

    or_range_atr: tuple = _pair("O_OR_RANGE_ATR", "1.2,5.0")
    buffer_atr: float = _f("O_BUFFER_ATR", 0.10)
    adx_min: float = _f("O_ADX_MIN", 17)
    di_spread_min: float = _f("O_DI_SPREAD_MIN", 3)
    rsi_long: tuple = _pair("O_RSI_LONG", "58,80")
    rsi_short: tuple = _pair("O_RSI_SHORT", "20,42")
    rvol_min: float = _f("O_RVOL_MIN", 1.5)
    rvol_min_retest: float = _f("O_RVOL_MIN_RETEST", 1.0)
    atr_pct_min: float = _f("O_ATR_PCT_MIN", 0.10)
    atr_pct_max: float = _f("O_ATR_PCT_MAX", 2.5)

    body_min: float = _f("O_BODY_MIN", 0.55)
    close_pos_min: float = _f("O_CLOSE_POS_MIN", 0.75)
    wick_max: float = _f("O_WICK_MAX", 0.30)              # wick against the move / range
    range_max_atr: float = _f("O_RANGE_MAX_ATR", 2.4)
    chase_max_atr: float = _f("O_CHASE_MAX_ATR", 1.0)     # close beyond OR level
    retest_touch_atr: float = _f("O_RETEST_TOUCH_ATR", 0.25)
    retest_window: int = _i("O_RETEST_WINDOW", 10)
    counter_gap_pct: float = _f("O_COUNTER_GAP_PCT", 0.5)

    bias_gate: bool = _b("O_BIAS_GATE", "1")
    min_score: int = _i("O_MIN_SCORE", 65)

    sl_swing_bars: int = _i("O_SL_SWING_BARS", 4)
    sl_buffer_atr: float = _f("O_SL_BUFFER_ATR", 0.10)
    sl_min_atr: float = _f("O_SL_MIN_ATR", 0.60)
    sl_max_atr: float = _f("O_SL_MAX_ATR", 2.00)
    tp1_r: float = _f("O_TP1_R", 1.2)
    tp2_r: float = _f("O_TP2_R", 2.4)

    daily_cap: int = _i("O_DAILY_CAP", 10)
    per_cycle_top: int = _i("O_PER_CYCLE_TOP", 4)


# --------------------------------------------------------------------------- #
# Module 3 - MCX (auto universe, up to 100 contracts): momentum breakout
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class McxCfg:
    first_signal: time = _t("M_FIRST_SIGNAL", "09:30")
    last_entry: time = _t("M_LAST_ENTRY", "22:45")
    session_end: time = _t("M_SESSION_END", "23:25")      # tracker closes open trades here
    market_open: time = _t("M_MARKET_OPEN", "09:00")
    market_close: time = _t("M_MARKET_CLOSE", "23:55")    # winter close; use 23:30 in US-DST summer
    max_symbols: int = _i("M_MAX_SYMBOLS", 100)
    roll_days: int = _i("M_ROLL_DAYS", 3)                 # roll to next expiry inside this many days
    min_median_bar_vol: float = _f("M_MIN_MEDIAN_BAR_VOL", 3)
    min_active_bars_day: int = _i("M_MIN_ACTIVE_BARS_DAY", 60)

    adx_min: float = _f("M_ADX_MIN", 20)                  # stricter than equities: commodities chop more
    di_spread_min: float = _f("M_DI_SPREAD_MIN", 5)
    rsi_long: tuple = _pair("M_RSI_LONG", "56,78")
    rsi_short: tuple = _pair("M_RSI_SHORT", "22,44")
    rvol_min: float = _f("M_RVOL_MIN", 1.4)
    atr_pct_min: float = _f("M_ATR_PCT_MIN", 0.05)
    atr_pct_max: float = _f("M_ATR_PCT_MAX", 3.0)
    donchian_n: int = _i("M_DONCHIAN_N", 20)
    expansion_min_atr: float = _f("M_EXPANSION_MIN_ATR", 1.0)   # breakout bar range / ATR
    pullback_adx_min: float = _f("M_PULLBACK_ADX_MIN", 25)
    pullback_lookback: int = _i("M_PULLBACK_LOOKBACK", 6)

    body_min: float = _f("M_BODY_MIN", 0.55)
    close_pos_min: float = _f("M_CLOSE_POS_MIN", 0.72)
    range_max_atr: float = _f("M_RANGE_MAX_ATR", 2.6)
    chase_max_atr: float = _f("M_CHASE_MAX_ATR", 1.2)
    ext_vwap_max_atr: float = _f("M_EXT_VWAP_MAX_ATR", 2.5)
    ema_sep_min_atr: float = _f("M_EMA_SEP_MIN_ATR", 0.10)
    max_crosses_20: int = _i("M_MAX_CROSSES_20", 2)

    htf_gate: bool = _b("M_HTF_GATE", "1")
    min_score: int = _i("M_MIN_SCORE", 65)

    sl_swing_bars: int = _i("M_SL_SWING_BARS", 5)
    sl_buffer_atr: float = _f("M_SL_BUFFER_ATR", 0.10)
    sl_min_atr: float = _f("M_SL_MIN_ATR", 0.60)
    sl_max_atr: float = _f("M_SL_MAX_ATR", 2.00)
    tp1_r: float = _f("M_TP1_R", 1.2)
    tp2_r: float = _f("M_TP2_R", 2.4)

    cooldown_bars: int = _i("M_COOLDOWN_BARS", 10)
    daily_cap: int = _i("M_DAILY_CAP", 12)
    per_cycle_top: int = _i("M_PER_CYCLE_TOP", 4)


NORMAL = NormalCfg()
ORB = OrbCfg()
MCX = McxCfg()

# Option suggestion (NSE modules)
OPTION_MIN_DAYS = _i("OPTION_MIN_DAYS", 1)      # skip expiries closer than this (no 0-DTE)
OPTION_QUOTE = _b("OPTION_QUOTE", "1")           # best-effort premium lookup at signal time
