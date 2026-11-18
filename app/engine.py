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

from . import mod_normal
from .common import Ctx, Signal, SymbolInfo
from .config import ENGINE, IST, NORMAL, OPTION_QUOTE
from .indicators import add_indicators, compute_features
from .telegram import format_signal, send
from .tracker import simulate
from .universe import Universe, build_universe, pick_option

log = logging.getLogger("engine")
MODULES = {"normal": (mod_normal, NORMAL)}
NEAR_MISS = {"htf_15m", "adx_min", "di_direction", "rsi_direction", "ema_vwap"}
OHLCV = ["open", "high", "low", "close", "volume"]


def floor5(now: datetime) -> datetime:
    return now.replace(minute=now.minute - now.minute % ENGINE.candle_min, second=0, microsecond=0)


def latest_eligible_bar(now: datetime) -> datetime:
    """Latest candle start whose close plus the configured fetch delay has passed."""
    eligible_close = floor5(now - timedelta(seconds=ENGINE.fetch_delay_sec))
    return eligible_close - timedelta(minutes=ENGINE.candle_min)


@dataclass
class SymbolState:
    info: SymbolInfo
    hist: Optional[pd.DataFrame] = None
    warm_day: Optional[date] = None
    disabled: str = ""
    err: str = ""
    warm_retry_at: Optional[datetime] = None
    # Strict Normal setup state. The first observed closed bar after startup is
    # a baseline only; live setups require two consecutive completed 5m bars.
    last_normal_bar: Optional[pd.Timestamp] = None
    pending_normal_signal: Optional[Signal] = None


class Engine:
    def __init__(self, client, store, notify: Callable = send,
                 universe_builder: Callable = build_universe):
        self.client, self.store, self.notify = client, store, notify
        self.builder = universe_builder
        self.uni: Optional[Universe] = None
        self.states: dict = {}                      # (module, symbol) -> SymbolState
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
        # Rebuild the complete single universe once per trading day.
        self.states = {}
        for info in uni.normal:
            self.states[("normal", info.symbol)] = SymbolState(info)
        try:
            self.store.expire_old(now.date())
        except Exception as e:
            log.warning("expire_old failed: %s", e)
        log.info("universe ready: stocks=%d indices=%d", sum(not x.is_index for x in uni.normal), sum(x.is_index for x in uni.normal))

    def _mod_states(self, module: str) -> list:
        return [st for (m, _), st in self.states.items() if m == module and not st.disabled]

    # ------------------------------------------------------------------ data
    def _warm(self, st: SymbolState, now: datetime, module: str):
        to_d = now.date() - timedelta(days=1)
        from_d = now.date() - timedelta(days=14)
        try:
            h = self.client.historical(st.info.key, from_d, to_d)
        except Exception:
            st.warm_retry_at = now + timedelta(minutes=5)
            raise
        h = h[OHLCV] if len(h) else pd.DataFrame(columns=OHLCV)
        if len(h):
            days = sorted(set(h.index.date))[-ENGINE.warm_sessions:]
            h = h[np.isin(h.index.date, days)]
        if not len(h):
            # An empty response can be transient. Do not mark this symbol warmed
            # for the day; retry with a per-symbol backoff on a later scan.
            st.hist = pd.DataFrame(columns=OHLCV)
            st.warm_retry_at = now + timedelta(minutes=5)
            st.err = "historical returned no bars; retry scheduled"
            return
        st.hist, st.warm_day = h, now.date()
        st.warm_retry_at = None
        st.err = ""

    def _load(self, st: SymbolState, now: datetime, module: str) -> Optional[pd.DataFrame]:
        if (st.warm_day != now.date()
                and (st.warm_retry_at is None or now >= st.warm_retry_at)):
            try:
                self._warm(st, now, module)
            except Exception as e:
                st.err = str(e)[:120]
                raise
        if st.disabled:
            return None
        intr = self.client.intraday(st.info.key)
        if len(intr):
            intr = intr[OHLCV]
            intr = intr[intr.index <= latest_eligible_bar(now)]   # closed bars after fetch delay
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
        mods = ("normal",) if segment == "nse" else ()
        states = [(m, st) for m in mods for st in self._mod_states(m)]
        log.info("prewarm %s: %d symbols", segment, len(states))

        def go(item):
            m, st = item
            try:
                if (st.warm_day != now.date()
                        and (st.warm_retry_at is None or now >= st.warm_retry_at)):
                    self._warm(st, now, m)
            except Exception as e:
                st.err = str(e)[:120]
                st.warm_retry_at = now + timedelta(minutes=5)
        with ThreadPoolExecutor(ENGINE.workers) as ex:
            list(ex.map(go, states))

    def _load_many(self, module: str, states: list, now: datetime, force: bool) -> dict:
        expected = latest_eligible_bar(now)
        out: dict = {}
        pending = states
        for attempt in range(1 + ENGINE.retry_passes):
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
                    if len(df) == 0 or df.index[-1] < expected:
                        retry.append(st)
            if not retry or getattr(self.client, "auth_failed", False):
                break
            all_empty = all((out.get(s.info.symbol) is None or len(out[s.info.symbol]) == 0) for s in retry)
            if all_empty and len(retry) >= 0.8 * max(1, len(states)):
                break     # holiday / outage: do not hammer the API
            pending = retry
            _time.sleep(5)
        return out

    # ------------------------------------------------------------------ cycle
    def cycle(self, module: str, now: datetime, force: bool = False) -> dict:
        cycle_started = _time.monotonic()
        mod, cfg = MODULES[module]
        day = now.date()
        seg = "nse"
        d = {"module": module, "at": now.strftime("%H:%M:%S"), "symbols": 0, "loaded": 0, "signals": 0,
             "scope": "", "rejects": Counter(), "near_miss": [], "errors": [], "forced": force,
             "fetch_seconds": 0.0, "evaluate_seconds": 0.0, "dispatch_seconds": 0.0,
             "duration_seconds": 0.0}
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
            states = [s for s in states if s.info.symbol in open_rows or s.pending_normal_signal is not None]
            if not states:
                d["scope"] = "idle"
                return d
        d["scope"] = "full" if full else "tracking"
        d["symbols"] = len(states)

        fetch_started = _time.monotonic()
        data = self._load_many(module, states, now, force)
        d["fetch_seconds"] = round(_time.monotonic() - fetch_started, 3)
        d["loaded"] = sum(1 for v in data.values() if v is not None and len(v))
        d["errors"] = [f"{s.info.symbol}: {s.err}" for s in states if s.err][:5]
        # Empty/stale responses are not proof of an exchange holiday. Keep the
        # scheduler retrying; holiday detection must come from a trusted calendar.

        evaluate_started = _time.monotonic()
        expected = latest_eligible_bar(now)
        cands: list = []
        watching = 0
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

            # Symbols with no new closed candle are not re-evaluated. On the
            # first scan after process start, establish a baseline and suppress
            # historical setups so a restart cannot flood Telegram.
            if module == "normal":
                if st.last_normal_bar is None:
                    st.last_normal_bar = f.bar_ts
                    log.info("normal baseline %s bar=%s", sym, f.bar_ts)
                    continue
                if f.bar_ts <= st.last_normal_bar:
                    if st.pending_normal_signal is not None:
                        watching += 1
                    continue

            stale = f.bar_ts < expected - timedelta(minutes=ENGINE.candle_min * ENGINE.stale_max_bars)
            pending = st.pending_normal_signal if module == "normal" else None
            if not full and pending is None:
                continue

            # A pending setup is allowed to complete after the last-entry time;
            # it must receive exactly the immediately following closed candle.
            if stale:
                d["rejects"]["stale_data"] += 1
                if pending is not None and f.bar_ts > pending.bar_ts:
                    st.pending_normal_signal = None
                    d["rejects"]["confirmation_stale_cancel"] += 1
                if st.pending_normal_signal is not None:
                    watching += 1
                if module == "normal":
                    st.last_normal_bar = f.bar_ts
                continue

            # Do not evaluate a Candle-1 setup outside the normal entry window.
            # Pending setups are still evaluated once on their next candle.
            ctx = Ctx(ignore_time=force)
            try:
                sig, why = mod.evaluate(st.info, ind, f, ctx)
            except Exception as e:  # one bad symbol must never kill the cycle
                log.exception("evaluate %s/%s failed", module, sym)
                d["rejects"]["eval_error"] += 1
                if pending is not None:
                    st.pending_normal_signal = None
                    d["rejects"]["confirmation_eval_error"] += 1
                if module == "normal":
                    st.last_normal_bar = f.bar_ts
                continue

            if module == "normal":
                if pending is not None:
                    # Candle 2 must be the immediately next 5m candle and
                    # independently pass every unchanged Normal strategy gate.
                    expected_confirmation = pending.bar_ts + pd.Timedelta(minutes=ENGINE.candle_min)
                    if f.bar_ts != expected_confirmation:
                        st.pending_normal_signal = None
                        d["rejects"]["confirmation_gap_cancel"] += 1
                    elif sig is not None and sig.side == pending.side:
                        cands.append(sig)
                        d["rejects"]["confirmation_passed"] += 1
                        st.pending_normal_signal = None
                    else:
                        st.pending_normal_signal = None
                        d["rejects"]["confirmation_failed"] += 1
                    # Candle 2 is terminal; never reuse it as another setup.
                elif full and sig is not None:
                    st.pending_normal_signal = sig
                    d["rejects"]["setup_armed"] += 1
                elif full and sig is None:
                    d["rejects"][why] += 1
                    if why in NEAR_MISS and len(d["near_miss"]) < 8:
                        d["near_miss"].append(f"{sym}:{why}")
                st.last_normal_bar = f.bar_ts
                if st.pending_normal_signal is not None:
                    watching += 1
            elif sig:
                cands.append(sig)
            else:
                d["rejects"][why] += 1
                if why in NEAR_MISS and len(d["near_miss"]) < 8:
                    d["near_miss"].append(f"{sym}:{why}")
        d["watching"] = watching
        d["evaluate_seconds"] = round(_time.monotonic() - evaluate_started, 3)
        dispatch_started = _time.monotonic()
        d["signals"] = self._dispatch(module, cfg, cands, now, force, d)
        d["dispatch_seconds"] = round(_time.monotonic() - dispatch_started, 3)
        d["duration_seconds"] = round(_time.monotonic() - cycle_started, 3)
        log.info(
            "scan %s scope=%s symbols=%d loaded=%d candidates=%d sent=%d "
            "watching=%d fetch=%.2fs evaluate=%.2fs dispatch=%.2fs total=%.2fs rejects=%s",
            module, d["scope"], d["symbols"], d["loaded"], len(cands), d["signals"],
            d["watching"], d["fetch_seconds"], d["evaluate_seconds"], d["dispatch_seconds"],
            d["duration_seconds"], dict(d["rejects"].most_common(6)),
        )
        return d

    def _dispatch(self, module: str, cfg, cands: list, now: datetime, force: bool, d: dict) -> int:
        """Persist and deliver eligible signals; fetch optional option quotes in one batch."""
        day = now.date()
        cands.sort(key=lambda s: -s.score)
        sent = 0
        self._deliver_pending(d, now)
        already = self.store.count_today(module, day)
        cd = getattr(cfg, "cooldown_bars", 0)

        # Resolve all eligible contracts first. A single batched LTP request avoids
        # one blocking HTTP request per signal and keeps the candle scan responsive.
        prepared = []
        for sig in cands:
            if cd:
                last = self.store.last_signal_ts(module, sig.symbol, sig.side)
                if last is not None:
                    lt = pd.Timestamp(last)
                    lt = lt.tz_localize(IST) if lt.tzinfo is None else lt.tz_convert(IST)
                    if abs(sig.bar_ts - lt) < timedelta(minutes=ENGINE.candle_min * cd):
                        d["rejects"]["cooldown"] += 1
                        continue
            sig.forced = force
            sig.option = self._option_for(sig, now)
            if sig.option is None:
                d["rejects"]["option_unavailable"] += 1
                continue
            prepared.append(sig)

        quotes = {}
        if OPTION_QUOTE and prepared:
            keys = list(dict.fromkeys(
                sig.option.get("_key") for sig in prepared if sig.option.get("_key")
            ))
            if keys:
                try:
                    quotes = self.client.ltp(keys) or {}
                except Exception as e:
                    log.warning("batched optional option quote failed: %s", e)
        for sig in prepared:
            key = sig.option.pop("_key", None)
            if key and OPTION_QUOTE:
                sig.option["ltp"] = quotes.get(key)

        accepted = 0
        for sig in prepared:
            if already + accepted >= cfg.daily_cap:
                d["rejects"]["daily_cap"] += 1
                continue
            if accepted >= cfg.per_cycle_top:
                d["rejects"]["cycle_top_n"] += 1
                continue
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
        # Keep the instrument key internal until all candidate contracts are
        # collected and quoted in one optional batch request.
        return {"tsym": rec.tsym, "lot": rec.lot, "strike": rec.strike,
                "ltp": None, "_key": rec.key}

    # ------------------------------------------------------------------ orchestration
    def run_boundary(self, now: datetime, only: Optional[str] = None, force: bool = False) -> dict:
        """Run every module that is active at `now`. Returns {module: diag}."""
        if not self.lock.acquire(blocking=False):
            return {"busy": True}
        try:
            self.ensure_universe(now)
            out = {}
            t = floor5(now).time()
            for m, enabled in (("normal", _on("ENABLE_NORMAL")),):
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
        if ENGINE.heartbeat_min:
            for m in ("normal",):
                d = self.diag.get(m)
                if not d or d.get("scope") not in ("full",):
                    continue
                last = self.last_heartbeat.get(m)
                if last is None or now - last >= timedelta(minutes=ENGINE.heartbeat_min):
                    self.last_heartbeat[m] = now
                    self._notify_silent(self.heartbeat_text(m, d))
        if ENGINE.eod_summary:
            for tag, end in (("nse", NORMAL.session_end),):
                key = (tag, now.date())
                if key not in self.eod_sent and t >= (datetime.combine(now.date(), end) + timedelta(minutes=20)).time():
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
        return "\n".join([
            f"💓 <b>{m.upper()} scan {d['at']}</b>",
            f"<b>symbols {d['loaded']}/{d['symbols']} · signals {d['signals']}</b>",
            f"<b>watching {d.get('watching', 0)} · top rejects: {top}</b>",
        ])

    def eod_text(self, day: date, tag: str) -> str:
        mods = ("normal",)
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
            "started": self.started.isoformat(),
            "store": getattr(self.store, "kind", "?"),
            "universe": ({
                "stocks": sum(not item.is_index for item in self.uni.normal),
                "indices": sum(item.is_index for item in self.uni.normal),
                "total": len(self.uni.normal),
                "stock_symbols": [item.symbol for item in self.uni.normal if not item.is_index],
                "index_symbols": [item.symbol for item in self.uni.normal if item.is_index],
            } if self.uni else {}),
            "dropped": self.uni.dropped if self.uni else {},
            "api_budget": bud,
            "auth_failed": getattr(self.client, "auth_failed", False),
            "closed_days": sorted(f"{segment}:{day}" for segment, day in self.closed_days),
            "last_error": self.last_error,
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
