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
ADMIN_TOKEN = _env("ADMIN_TOKEN", "").strip()            # required for /force /stats /diag; fail closed
KEEPALIVE = _b("KEEPALIVE", "1")                         # self-ping so the free dyno stays awake
RUN_SCHEDULER = _b("RUN_SCHEDULER", "1")
DRY_RUN = _b("DRY_RUN", "0")                             # 1 = log signals, do not send Telegram

# Modules on/off
ENABLE_NORMAL = _b("ENABLE_NORMAL", "1")


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
# Single NSE CALL/PUT strategy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NormalCfg:
    first_signal: time = _t("N_FIRST_SIGNAL", "09:20")
    last_entry: time = _t("N_LAST_ENTRY", "15:00")
    session_end: time = _t("N_SESSION_END", "15:15")
    adx_min: float = _f("N_ADX_MIN", 18)
    sl_atr: float = _f("N_SL_ATR", 1.2)
    tp1_r: float = _f("N_TP1_R", 1.5)
    tp2_r: float = _f("N_TP2_R", 2.0)
    cooldown_bars: int = _i("N_COOLDOWN_BARS", 3)
    daily_cap: int = _i("N_DAILY_CAP", 1000)
    per_cycle_top: int = _i("N_PER_CYCLE_TOP", 1000)


NORMAL = NormalCfg()
