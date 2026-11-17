"""Engine: data loading, evaluation, de-duplication, caps, alerts, tracking, diagnostics.

One `cycle(module, now)` = fetch -> indicators -> module.evaluate -> cooldown/cap/top-N
-> store (UNIQUE dedupe) -> Telegram. Every symbol that is NOT signalled is counted
under the first gate that rejected it (see /diag) so a silent bot is never a mystery.
"""
import logging
import threading
import time as _time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

import numpy as np
import pandas as pd

from . import mod_mcx, mod_normal, mod_orb
from . import symbols as S
from .common import Ctx, Signal, SymbolInfo, market_bias
from .config import ENGINE, IST, MCX, NORMAL, OPTION_QUOTE, ORB
from .indicators import add_indicators, compute_features
from .telegram import format_signal, send
from .tracker import simulate
from .universe import Universe, build_universe, pick_option

log = logging.getLogger("engine")
MODULES = {"normal": (mod_normal, NORMAL), "orb": (mod_orb, ORB), "mcx": (mod_mcx, MCX)}
NEAR_MISS = {"score", "htf_15m", "market_bias", "no_trigger", "sl_too_wide", "chasing", "extended_vwap"}
OHLCV = ["open", "high", "low", "close", "volume"]


def floor5(now: datetime) -> datetime:
    return now.replace(minute=now.minute - now.minute % ENGINE.candle_min, second=0, microsecond=0)


@dataclass
class SymbolState:
    info: SymbolInfo
    hist: Optional[pd.DataFrame] = None
    warm_day: Optional[date] = None
    disabled: str = ""
    err: str = ""


class Engine:
    def __init__(self, client, store, notify: Callable = send,
                 universe_builder: Callable = build_universe):
        self.client, self.store, self.notify = client, store, notify
        self.builder = universe_builder
        self.uni: Optional[Universe] = None
        self.states: dict = {}                      # (module, symbol) -> SymbolState
        self.bias, self.bias_at = "neutral", None
        self.diag: dict = {}
        self.closed_days: set = set()               # (segment, date) flagged as holiday
        self.prewarmed: set = set()
        self.last_heartbeat: dict = {}
        self.eod_sent: set = set()
        self.lock = threading.Lock()                # one cycle at a time (scheduler vs /force)
        self.started = datetime.now(IST)
        self.last_error = ""
        self.last_outbox_retry_at = None

    # ------------------------------------------------------------------ universe
    def ensure_universe(self, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if self.uni and self.uni.built_on == today:
            return
        if self.uni and now.time() < time(7, 30):
            return
        try:
            uni = self.builder(now)
        except Exception as e:
            self.last_error = f"universe: {e}"
            log.error("universe build failed: %s", e)
            if self.uni is None:
                raise
            return
        self.uni = uni
        self.states = {}
        for m in ("normal", "orb", "mcx"):
            for info in uni.by_module(m):
                self.states[(m, info.symbol)] = SymbolState(info)
        try:
            self.store.expire_old(now.date())
        except Exception as e:
            log.warning("expire_old failed: %s", e)
        log.info("universe ready: %s", {m: len(uni.by_module(m)) for m in MODULES})

    def _mod_states(self, module: str) -> list:
        return [st for (m, _), st in self.states.items() if m == module and not st.disabled]

    # ------------------------------------------------------------------ data
    def _warm(self, st: SymbolState, now: datetime, module: str):
        to_d = now.date() - timedelta(days=1)
        from_d = now.date() - timedelta(days=14)
        h = self.client.historical(st.info.key, from_d, to_d)
        h = h[OHLCV] if len(h) else pd.DataFrame(columns=OHLCV)
        if len(h):
            days = sorted(set(h.index.date))[-ENGINE.warm_sessions:]
            h = h[np.isin(h.index.date, days)]
        st.hist, st.warm_day = h, now.date()
        if module == "mcx":
            n_days = len(set(h.index.date)) if len(h) else 0
            if n_days < 3:
                st.disabled = "insufficient_history"
                return
            active = h[h["volume"] > 0].groupby(h[h["volume"] > 0].index.date).size()
            med_bars = float(active.median()) if len(active) else 0.0
            med_vol = float(h.loc[h["volume"] > 0, "volume"].median()) if (h["volume"] > 0).any() else 0.0
            if med_bars < MCX.min_active_bars_day or med_vol < MCX.min_median_bar_vol:
                st.disabled = f"illiquid (bars/day {med_bars:.0f}, median vol {med_vol:.1f})"

    def _load(self, st: SymbolState, now: datetime, module: str) -> Optional[pd.DataFrame]:
        if st.warm_day != now.date():
            self._warm(st, now, module)
            if st.disabled:
                return None
        intr = self.client.intraday(st.info.key)
        if len(intr):
            intr = intr[OHLCV]
            intr = intr[intr.index <= now - timedelta(minutes=ENGINE.candle_min)]   # closed bars only
        parts = [p for p in (st.hist, intr) if p is not None and len(p)]
        if not parts:
            return pd.DataFrame(columns=OHLCV)
        df = pd.concat(parts)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df.tail(ENGINE.max_bars_kept)

    def prewarm(self, now: datetime, segment: str):
        key = (segment, now.date())
        if key in self.prewarmed or not self.uni:
            return
        self.prewarmed.add(key)
        mods = ("normal", "orb") if segment == "nse" else ("mcx",)
        states = [(m, st) for m in mods for st in self._mod_states(m)]
        log.info("prewarm %s: %d symbols", segment, len(states))

        def go(item):
            m, st = item
            try:
                if st.warm_day != now.date():
                    self._warm(st, now, m)
            except Exception as e:
                st.err = str(e)
        with ThreadPoolExecutor(ENGINE.workers) as ex:
            list(ex.map(go, states))

    def _load_many(self, module: str, states: list, now: datetime, force: bool) -> dict:
        expected = floor5(now) - timedelta(minutes=ENGINE.candle_min)
        out: dict = {}
        pending = states
        for attempt in range(1 + (0 if force else ENGINE.retry_passes)):
            retry = []
            with ThreadPoolExecutor(ENGINE.workers) as ex:
                futs = {ex.submit(self._load, st, now, module): st for st in pending}
                for fu in as_completed(futs):
                    st = futs[fu]
                    try:
                        df = fu.result()
                        st.err = ""
                    except Exception as e:
                        st.err = str(e)[:120]
                        retry.append(st)
                        continue
                    if df is None:
                        continue
                    out[st.info.symbol] = df
                    if not force and (len(df) == 0 or df.index[-1] < expected):
                        retry.append(st)
            if not retry or getattr(self.client, "auth_failed", False):
                break
            all_empty = all((out.get(s.info.symbol) is None or len(out[s.info.symbol]) == 0) for s in retry)
            if all_empty and len(retry) >= 0.8 * max(1, len(states)):
                break     # holiday / outage: do not hammer the API
            pending = retry
            _time.sleep(5)
        return out

    # ------------------------------------------------------------------ bias
    def _set_bias(self, df: Optional[pd.DataFrame], now: datetime):
        if df is None or len(df) < 50:
            return False
        # Never stamp yesterday's historical close as a fresh live index bias.
        latest = pd.Timestamp(df.index[-1])
        latest = latest.tz_localize(IST) if latest.tzinfo is None else latest.tz_convert(IST)
        expected = floor5(now) - timedelta(minutes=ENGINE.candle_min)
        max_age = timedelta(minutes=ENGINE.candle_min * ENGINE.stale_max_bars)
        if latest.date() != now.date() or latest > expected or expected - latest > max_age:
            self.bias = "neutral"
            self.bias_at = None
            return False
        f = compute_features(add_indicators(df))
        if f is None:
            self.bias = "neutral"
            self.bias_at = None
            return False
        self.bias, self.bias_at = market_bias(f), now
        return True

    def _refresh_bias(self, now: datetime):
        if self.bias_at and (now - self.bias_at) < timedelta(minutes=4):
            return
        st = self.states.get(("normal", S.BIAS_INDEX))
        if not st:
            return
        try:
            self._set_bias(self._load(st, now, "normal"), now)
        except Exception as e:
            self.bias = "neutral"
            log.warning("bias refresh failed: %s", e)

    # ------------------------------------------------------------------ cycle
    def cycle(self, module: str, now: datetime, force: bool = False) -> dict:
        mod, cfg = MODULES[module]
        day = now.date()
        seg = "mcx" if module == "mcx" else "nse"
        d = {"module": module, "at": now.strftime("%H:%M:%S"), "symbols": 0, "loaded": 0, "signals": 0,
             "scope": "", "rejects": Counter(), "near_miss": [], "errors": [], "forced": force}
        self.diag[module] = d
        if (seg, day) in self.closed_days and not force:
            d["scope"] = "market_closed_today"
            return d
        t_close = floor5(now).time()
        full = force or (cfg.first_signal <= t_close <= cfg.last_entry)
        states = self._mod_states(module)
        open_rows: dict = {}
        for r in self.store.open_signals(day, module):
            open_rows.setdefault(r["symbol"], []).append(r)
        if not full:
            states = [s for s in states if s.info.symbol in open_rows]
            if not states:
                d["scope"] = "idle"
                return d
        d["scope"] = "full" if full else "tracking"
        d["symbols"] = len(states)
        if module != "normal" and full:
            self._refresh_bias(now)

        data = self._load_many(module, states, now, force)
        d["loaded"] = sum(1 for v in data.values() if v is not None and len(v))
        d["errors"] = [f"{s.info.symbol}: {s.err}" for s in states if s.err][:5]
        # Empty/stale responses are not proof of an exchange holiday. Keep the
        # scheduler retrying; holiday detection must come from a trusted calendar.
        if module == "normal" and S.BIAS_INDEX in data:
            self._set_bias(data[S.BIAS_INDEX], now)

        expected = floor5(now) - timedelta(minutes=ENGINE.candle_min)
        taken = self.store.symbols_today(module, day) if module == "orb" else set()
        cands: list = []
        for sym, df in data.items():
            st = self.states[(module, sym)]
            if df is None or len(df) < 50:
                d["rejects"]["no_data"] += 1
                continue
            ind = add_indicators(df)
            f = compute_features(ind)
            if f is None:
                d["rejects"]["warmup"] += 1
                continue
            for r in open_rows.get(sym, []):
                status, rr = simulate(r, ind, cfg.session_end)
                if status != r["status"]:
                    self.store.update_outcome(r["id"], status, rr)
            if not full:
                continue
            if not force and f.bar_ts < expected - timedelta(minutes=ENGINE.candle_min * ENGINE.stale_max_bars):
                d["rejects"]["stale_data"] += 1
                continue
            ctx = Ctx(bias=self.bias if module != "mcx" else "neutral", ignore_time=force,
                      signaled_today=lambda s_, taken=taken: s_ in taken)
            try:
                sig, why = mod.evaluate(st.info, ind, f, ctx)
            except Exception as e:  # one bad symbol must never kill the cycle
                log.exception("evaluate %s/%s failed", module, sym)
                d["rejects"]["eval_error"] += 1
                continue
            if sig:
                cands.append(sig)
            else:
                d["rejects"][why] += 1
                if why in NEAR_MISS and len(d["near_miss"]) < 8:
                    d["near_miss"].append(f"{sym}:{why}")
        d["signals"] = self._dispatch(module, cfg, cands, now, force, d)
        return d

    def _dispatch(self, module: str, cfg, cands: list, now: datetime, force: bool, d: dict) -> int:
        day = now.date()
        cands.sort(key=lambda s: -s.score)
        sent = 0
        accepted = 0
        self._deliver_pending(d, now)
        already = self.store.count_today(module, day)
        cd = getattr(cfg, "cooldown_bars", 0)
        for sig in cands:
            if already + accepted >= cfg.daily_cap:
                d["rejects"]["daily_cap"] += 1
                continue
            if accepted >= cfg.per_cycle_top:
                d["rejects"]["cycle_top_n"] += 1
                continue
            if cd:
                last = self.store.last_signal_ts(module, sig.symbol, sig.side)
                if last is not None:
                    lt = pd.Timestamp(last)
                    lt = lt.tz_localize(IST) if lt.tzinfo is None else lt.tz_convert(IST)
                    if abs(sig.bar_ts - lt) < timedelta(minutes=ENGINE.candle_min * cd):
                        d["rejects"]["cooldown"] += 1
                        continue
            sig.forced = force
            if module != "mcx":
                sig.option = self._option_for(sig, now)
            message = format_signal(sig)
            row = {"module": module, "symbol": sig.symbol, "side": sig.side, "bar_ts": sig.bar_ts.to_pydatetime(),
                   "trade_day": sig.bar_ts.date(), "entry": sig.entry, "sl": sig.sl, "tp1": sig.tp1, "tp2": sig.tp2,
                   "score": sig.score, "trig": sig.trigger,
                   "details": {"reasons": sig.reasons, "atr": sig.atr, "forced": force,
                               "telegram_text": message}}
            try:
                sid = self.store.add_signal(row)
            except Exception:
                d["rejects"]["storage_error"] += 1
                log.exception("could not persist signal %s/%s; alert was not sent", module, sig.symbol)
                continue
            if sid is None:
                d["rejects"]["duplicate"] += 1
                continue
            accepted += 1
            log.info("SIGNAL %s %s %s score=%d entry=%.2f sl=%.2f", module, sig.symbol, sig.side, sig.score,
                     sig.entry, sig.sl)
            try:
                result = self.notify(message)
                ok = result is not False
                delivery_error = None if ok else "notify returned false"
            except Exception as e:
                ok = False
                delivery_error = str(e)
                log.exception("Telegram delivery failed for signal id=%s", sid)
            try:
                self.store.mark_notification(sid, ok, delivery_error)
            except Exception:
                # If Telegram succeeded but this write failed, the outbox may retry
                # and produce a duplicate; that is safer than silently losing an alert.
                d["rejects"]["delivery_state_error"] += 1
                log.exception("could not persist Telegram delivery state for signal id=%s", sid)
            if ok:
                sent += 1
            else:
                d["rejects"]["telegram_failed"] += 1
        return sent

    def _deliver_pending(self, d: dict, now: datetime):
        if self.last_outbox_retry_at and now - self.last_outbox_retry_at < timedelta(minutes=5):
            return
        self.last_outbox_retry_at = now
        try:
            pending = self.store.pending_notifications(limit=25)
        except Exception:
            d["rejects"]["outbox_read_error"] += 1
            log.exception("could not read Telegram outbox")
            return
        for item in pending:
            try:
                result = self.notify(item["text"])
                ok = result is not False
                self.store.mark_notification(item["id"], ok, None if ok else "notify returned false")
                if not ok:
                    d["rejects"]["telegram_retry_failed"] += 1
            except Exception as e:
                try:
                    self.store.mark_notification(item["id"], False, str(e))
                except Exception:
                    log.exception("could not persist Telegram retry failure")
                d["rejects"]["telegram_retry_failed"] += 1
                log.exception("Telegram retry failed for signal id=%s", item["id"])

    def _option_for(self, sig: Signal, now: datetime) -> Optional[dict]:
        if not self.uni or not sig.info:
            return None
        rec = pick_option(self.uni, sig.info.underlying, sig.entry, sig.side, now, sig.info.is_index)
        if not rec:
            return None
        out = {"tsym": rec.tsym, "lot": rec.lot, "strike": rec.strike, "ltp": None}
        if OPTION_QUOTE:
            try:
                px = self.client.ltp([rec.key])
                out["ltp"] = px.get(rec.key) or (next(iter(px.values())) if len(px) == 1 else None)
            except Exception:
                pass
        return out

    # ------------------------------------------------------------------ orchestration
    def run_boundary(self, now: datetime, only: Optional[str] = None, force: bool = False) -> dict:
        """Run every module that is active at `now`. Returns {module: diag}."""
        if not self.lock.acquire(blocking=False):
            return {"busy": True}
        try:
            self.ensure_universe(now)
            out = {}
            t = floor5(now).time()
            for m, enabled in (("normal", _on("ENABLE_NORMAL")), ("orb", _on("ENABLE_ORB")), ("mcx", _on("ENABLE_MCX"))):
                if only and only != m:
                    continue
                if not enabled and not force:
                    continue
                cfg = MODULES[m][1]
                active = cfg.first_signal <= t <= (cfg.session_end if m != "normal" else NORMAL.session_end)
                if not (active or force):
                    continue
                try:
                    out[m] = self.cycle(m, now, force)
                except Exception as e:
                    self.last_error = f"{m}: {e}"
                    log.exception("cycle %s failed", m)
                    out[m] = {"error": str(e)}
                    if getattr(self.client, "auth_failed", False):
                        self._alert_once("auth", "⚠️ <b>Upstox token rejected (401/403) - update UPSTOX_ACCESS_TOKEN</b>", now)
            return out
        finally:
            self.lock.release()

    def _alert_once(self, key: str, text: str, now: datetime, every_min: int = 60):
        last = self.last_heartbeat.get("alert:" + key)
        if last is None or now - last > timedelta(minutes=every_min):
            self.last_heartbeat["alert:" + key] = now
            self.notify(text)

    def housekeeping(self, now: datetime):
        """Pre-warm, heartbeat and end-of-day summary. Cheap; called every few seconds."""
        if now.weekday() >= 5:
            return
        self.ensure_universe(now)
        t = now.time()
        if ENGINE.prewarm_nse <= t < time(9, 15):
            self.prewarm(now, "nse")
        if ENGINE.prewarm_mcx <= t < MCX.first_signal and now.time() < time(9, 0):
            self.prewarm(now, "mcx")
        if ENGINE.heartbeat_min:
            for m in ("normal", "orb", "mcx"):
                d = self.diag.get(m)
                if not d or d.get("scope") not in ("full",):
                    continue
                last = self.last_heartbeat.get(m)
                if last is None or now - last >= timedelta(minutes=ENGINE.heartbeat_min):
                    self.last_heartbeat[m] = now
                    self._notify_silent(self.heartbeat_text(m, d))
        if ENGINE.eod_summary:
            for tag, end in (("nse", NORMAL.session_end), ("mcx", MCX.session_end)):
                key = (tag, now.date())
                if key not in self.eod_sent and t >= (datetime.combine(now.date(), end) + timedelta(minutes=20)).time():
                    if tag == "mcx" and t < time(23, 20):
                        continue
                    self.eod_sent.add(key)
                    self._notify_silent(self.eod_text(now.date(), tag))

    def _notify_silent(self, text: str):
        if _accepts_silent(self.notify):
            self.notify(text, silent=True)
        else:
            self.notify(text)

    # ------------------------------------------------------------------ reports
    def heartbeat_text(self, m: str, d: dict) -> str:
        top = ", ".join(f"{k} {v}" for k, v in d["rejects"].most_common(4)) or "-"
        return (f"💓 <b>{m.upper()} scan {d['at']}</b>\n<b>symbols {d['loaded']}/{d['symbols']} · signals {d['signals']}"
                f" · bias {self.bias}</b>\n<b>top rejects: {top}</b>")

    def eod_text(self, day: date, tag: str) -> str:
        mods = ("normal", "orb") if tag == "nse" else ("mcx",)
        rows = [r for r in self.store.stats(day) if r["module"] in mods]
        if not rows:
            return f"📊 <b>EOD {tag.upper()}: no signals today</b>"
        lines = [f"📊 <b>EOD {tag.upper()} {day:%d %b}</b>"]
        for r in rows:
            wr = f"{100 * r['wins'] / r['closed']:.0f}%" if r["closed"] else "n/a"
            lines.append(f"<b>{r['module']}: {r['n']} signals · closed {r['closed']} · win {wr} · net {r['sum_r']:+.1f}R</b>")
        return "\n".join(lines)

    def status(self) -> dict:
        bud = self.client.budget.usage() if hasattr(self.client, "budget") else {}
        return {
            "started": self.started.isoformat(), "store": getattr(self.store, "kind", "?"),
            "universe": ({m: len(self.uni.by_module(m)) for m in MODULES} if self.uni else {}),
            "dropped": self.uni.dropped if self.uni else {},
            "mcx_disabled": {s.info.symbol: s.disabled for (m, _), s in self.states.items() if m == "mcx" and s.disabled},
            "bias": self.bias, "api_budget": bud, "auth_failed": getattr(self.client, "auth_failed", False),
            "closed_days": sorted(f"{a}:{b}" for a, b in self.closed_days), "last_error": self.last_error,
        }

    def diag_json(self) -> dict:
        return {m: dict(d, rejects=dict(d["rejects"].most_common())) for m, d in self.diag.items()}


def _on(name: str) -> bool:
    from . import config
    return bool(getattr(config, name))


def _accepts_silent(fn) -> bool:
    try:
        import inspect
        return "silent" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
