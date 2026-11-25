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
# Prefer the dedicated long-lived read-only Analytics Token. Keep the old variable
# as a backwards-compatible fallback so existing deployments do not break.
UPSTOX_ANALYTICS_TOKEN = _env("UPSTOX_ANALYTICS_TOKEN", "").strip()
UPSTOX_LEGACY_ACCESS_TOKEN = _env("UPSTOX_ACCESS_TOKEN", "").strip()
UPSTOX_TOKEN = UPSTOX_ANALYTICS_TOKEN or UPSTOX_LEGACY_ACCESS_TOKEN
UPSTOX_TOKEN_SOURCE = (
    "analytics_token" if UPSTOX_ANALYTICS_TOKEN
    else "legacy_access_token" if UPSTOX_LEGACY_ACCESS_TOKEN
    else "missing"
)
TELEGRAM_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")
DATABASE_URL = _env("NEON_DATABASE_URL", _env("DATABASE_URL", ""))  # Neon PostgreSQL; DATABASE_URL remains a compatibility alias
REQUIRE_PERSISTENT_DB = _b("REQUIRE_PERSISTENT_DB", "1")
ADMIN_TOKEN = _env("ADMIN_TOKEN", "").strip()            # required for /force /stats /diag; fail closed
KEEPALIVE = _b("KEEPALIVE", "1")                         # self-ping so the free dyno stays awake
RUN_SCHEDULER = _b("RUN_SCHEDULER", "1")
UPSTOX_STREAM_ENABLED = _b("UPSTOX_STREAM_ENABLED", "1")
# The signal engine consumes completed REST candles. WebSocket quotes are
# supplemental; require them for readiness only when explicitly configured.
UPSTOX_STREAM_REQUIRED = _b("UPSTOX_STREAM_REQUIRED", "0")
DRY_RUN = _b("DRY_RUN", "0")                             # 1 = log signals, do not send Telegram
TRAINING_COST_R = max(0.0, _f("TRAINING_COST_R", 0.0))  # configured round-trip cost expressed in underlying R

# Modules on/off
ENABLE_NORMAL = _b("ENABLE_NORMAL", "1")
# This production bot intentionally runs a single signal module: Normal only.
# Kept as a constant so a stale Render environment variable cannot re-enable Momentum.
ENABLE_MOMENTUM = False


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Engine:
    candle_min: int = 5
    warm_sessions: int = 6                 # prior sessions kept for indicator warm-up
    fetch_delay_sec: int = _i("FETCH_DELAY_SEC", 6)   # wait after candle close before fetching
    stale_max_bars: int = _i("STALE_MAX_BARS", 1)     # signal bar must be newest or 1 bar old
    # Keep enough concurrent I/O workers to hide Upstox network latency across
    # the 150-symbol universe. The shared Budget remains the hard request-rate
    # ceiling, so raising concurrency does not raise provider request volume.
    workers: int = max(8, min(24, _i("WORKERS", 24)))
    retry_passes: int = 1
    # Six warm sessions (~450 5m bars) plus today's bars fit within this bound.
    max_bars_kept: int = 600
    # Upstox standard limits: 50/s, 500/min, 2000/30min. We stay well below.
    rate_per_sec: int = _i("RATE_PER_SEC", 15)
    rate_per_min: int = _i("RATE_PER_MIN", 450)
    rate_per_30min: int = _i("RATE_PER_30MIN", 1800)
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
    # ADX 20 is the current approved hard floor; stale Render values cannot weaken it.
    # Operators may still raise N_ADX_MIN above 20.
    adx_min: float = max(20.0, _f("N_ADX_MIN", 20))
    # One final quality gate; individual indicator thresholds above remain unchanged.
    score_min: int = max(0, min(100, _i("N_SCORE_MIN", 72)))
    sl_atr: float = _f("N_SL_ATR", 1.2)
    tp1_r: float = _f("N_TP1_R", 1.5)
    tp2_r: float = _f("N_TP2_R", 2.0)
    # Per-symbol lifecycle enforces the four-bar cooldown; disable legacy dispatch cooldown.
    cooldown_bars: int = 0
    daily_cap: int = _i("N_DAILY_CAP", 1000)
    per_cycle_top: int = _i("N_PER_CYCLE_TOP", 1000)


NORMAL = NormalCfg()


@dataclass(frozen=True)
class MomentumCfg:
    first_signal: time = _t("M_FIRST_SIGNAL", "09:20")
    last_entry: time = _t("M_LAST_ENTRY", "15:00")
    session_end: time = _t("M_SESSION_END", "15:15")
    score_min: int = max(0, min(100, _i("M_SCORE_MIN", 78)))
    adx_min: float = NORMAL.adx_min
    sl_atr: float = NORMAL.sl_atr
    tp1_r: float = NORMAL.tp1_r
    tp2_r: float = NORMAL.tp2_r
    cooldown_bars: int = _i("M_COOLDOWN_BARS", 2)
    daily_cap: int = _i("M_DAILY_CAP", 1000)
    per_cycle_top: int = _i("M_PER_CYCLE_TOP", 1000)


MOMENTUM = MomentumCfg()


OPTION_MIN_DAYS = _i("OPTION_MIN_DAYS", 1)
# Option LTP is display-only. Keep it off by default because some valid
# candle-data tokens receive 401 from the optional v2 quote endpoint.
# Set OPTION_QUOTE=1 only after verifying quote-endpoint access for the token.
OPTION_QUOTE = _b("OPTION_QUOTE", "0")
# Full option quote snapshots support descriptive tracking. Strict liquidity
# validation is optional and disabled by default; when enabled it fails closed.
OPTION_MARKET_SNAPSHOT = _b("OPTION_MARKET_SNAPSHOT", "1")
OPTION_LIQUIDITY_CHECKS = _b("OPTION_LIQUIDITY_CHECKS", "0")
OPTION_MIN_VOLUME = _i("OPTION_MIN_VOLUME", 100)
OPTION_MIN_OI = _i("OPTION_MIN_OI", 500)
OPTION_MAX_SPREAD_PCT = _f("OPTION_MAX_SPREAD_PCT", 3.0)
OPTION_MAX_QUOTE_AGE_SEC = _i("OPTION_MAX_QUOTE_AGE_SEC", 20)
# Descriptive option-premium tracking only; it never gates or changes signals.
OPTION_PREMIUM_TRACKING = _b("OPTION_PREMIUM_TRACKING", "1")
# User-configured estimates; both default to zero and are never called actual charges.
OPTION_EST_ROUND_TRIP_FEE_PCT = max(0.0, _f("OPTION_EST_ROUND_TRIP_FEE_PCT", 0.0))
OPTION_EST_SLIPPAGE_BPS_PER_SIDE = max(0.0, _f("OPTION_EST_SLIPPAGE_BPS_PER_SIDE", 0.0))
