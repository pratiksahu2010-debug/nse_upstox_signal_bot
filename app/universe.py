"""Resolve symbol lists into Upstox instrument keys using the live instrument master.

Memory discipline: the NSE master has hundreds of thousands of rows, so it is streamed
and only compact tuples for our ~160 underlyings are kept.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

from . import symbols as S
from .common import SymbolInfo
from .config import IST, MCX as MC, OPTION_MIN_DAYS
from .upstox import expiry_to_date, stream_master

log = logging.getLogger("universe")


@dataclass
class OptionRec:
    expiry: datetime
    strike: float
    otype: str          # CE | PE
    key: str
    tsym: str
    lot: int


@dataclass
class Universe:
    normal: list = field(default_factory=list)       # SymbolInfo (indices first)
    orb: list = field(default_factory=list)
    mcx: list = field(default_factory=list)
    options: dict = field(default_factory=dict)      # underlying -> list[OptionRec]
    dropped: dict = field(default_factory=dict)      # module -> list of names not resolvable
    built_on: str = ""

    def by_module(self, m: str) -> list:
        return {"normal": self.normal, "orb": self.orb, "mcx": self.mcx}[m]


def _pick_future(futs: list, today: datetime, roll_days: int):
    """futs: list of (expiry_dt, key, lot, tsym, tick). Nearest expiry that is not about to expire."""
    futs = sorted(futs, key=lambda x: x[0])
    live = [f for f in futs if f[0].date() >= today.date()]
    if not live:
        return None
    for f in live:
        if (f[0].date() - today.date()).days >= roll_days:
            return f
    return live[-1]


def resolve_nse(rows: Iterable[dict], today: datetime, normal_names: list, normal_reserve: list,
                orb_names: list, orb_reserve: list, indices: list) -> Universe:
    want = set(normal_names) | set(normal_reserve) | set(orb_names) | set(orb_reserve)
    want_idx = set(indices)
    eq: dict = {}
    fut_by_und: dict = {}
    fo_und: set = set()
    opts: dict = {}
    horizon = today + timedelta(days=45)
    for r in rows:
        seg = r.get("segment")
        if seg == "NSE_EQ":
            ts = r.get("trading_symbol")
            if ts in want and r.get("instrument_type") == "EQ":
                eq[ts] = r
        elif seg == "NSE_FO":
            und = r.get("underlying_symbol")
            if und not in want and und not in want_idx:
                continue
            exp = expiry_to_date(r.get("expiry"))
            if exp is None or exp.date() < today.date():
                continue
            typ = r.get("instrument_type")
            if typ == "FUT":
                fo_und.add(und)
                fut_by_und.setdefault(und, []).append(
                    (exp, r["instrument_key"], int(r.get("lot_size") or 0), r.get("trading_symbol", ""),
                     float(r.get("tick_size") or 0.05)))
            elif typ in ("CE", "PE") and exp <= horizon:
                opts.setdefault(und, []).append(OptionRec(
                    exp, float(r.get("strike_price") or 0), typ, r["instrument_key"],
                    r.get("trading_symbol", ""), int(r.get("lot_size") or 0)))

    uni = Universe(built_on=today.strftime("%Y-%m-%d"))
    uni.options = opts
    dropped_n, dropped_o = [], []

    def mk(name, module):
        r = eq.get(name)
        futs = fut_by_und.get(name)
        if not r or not futs:
            return None
        lot = sorted(futs, key=lambda x: x[0])[0][2]
        # NSE cash tick is Rs 0.05; the master's tick_size unit is not documented, so do not trust it
        return SymbolInfo(name, r["instrument_key"], module, lot=lot, tick=0.05, underlying=name)

    # indices via near-month futures (volume + VWAP are meaningful; cash index has no volume)
    for ix in indices:
        f = _pick_future(fut_by_und.get(ix, []), today, 2)
        if f:
            uni.normal.append(SymbolInfo(ix, f[1], "normal", lot=f[2], tick=0.05, is_index=True,
                                         expiry=f[0].strftime("%d%b%y").upper(), underlying=ix))
        else:
            dropped_n.append(ix)

    def fill(names, reserve, module, target, out, dropped, used):
        for n in list(names) + list(reserve):
            if len([x for x in out if not x.is_index]) >= target:
                break
            if n in used:
                continue
            info = mk(n, module)
            if info:
                out.append(info)
                used.add(n)
            elif n in names:
                dropped.append(n)

    used: set = set()
    fill(normal_names, normal_reserve, "normal", 50, uni.normal, dropped_n, used)
    fill(orb_names, orb_reserve, "orb", 100, uni.orb, dropped_o, used)
    uni.dropped = {"normal": dropped_n, "orb": dropped_o}
    return uni


def resolve_mcx(rows: Iterable[dict], today: datetime) -> list:
    futs: dict = {}
    for r in rows:
        if r.get("segment") != "MCX_FO" or r.get("instrument_type") != "FUT":
            continue
        exp = expiry_to_date(r.get("expiry"))
        if exp is None or exp.date() < today.date():
            continue
        und = r.get("underlying_symbol") or r.get("name")
        futs.setdefault(und, []).append(
            (exp, r["instrument_key"], int(r.get("lot_size") or 0), r.get("trading_symbol", ""),
             float(r.get("tick_size") or 0.05)))
    out = []
    for und, lst in sorted(futs.items()):
        f = _pick_future(lst, today, MC.roll_days)
        if f:
            out.append(SymbolInfo(und, f[1], "mcx", lot=f[2], tick=f[4],
                                  expiry=f[0].strftime("%d%b%y").upper(), underlying=und))
    return out[:MC.max_symbols]


def build_universe(today: Optional[datetime] = None, nse_rows=None, mcx_rows=None) -> Universe:
    today = today or datetime.now(IST)
    uni = resolve_nse(nse_rows if nse_rows is not None else stream_master("NSE"), today,
                      S.NORMAL_50, S.NORMAL_RESERVE, S.ORB_100, S.ORB_RESERVE, S.INDICES)
    try:
        uni.mcx = resolve_mcx(mcx_rows if mcx_rows is not None else stream_master("MCX"), today)
    except Exception as e:
        log.error("MCX master failed: %s", e)
        uni.mcx = []
    log.info("universe: normal=%d (incl %d idx) orb=%d mcx=%d | dropped %s",
             len(uni.normal), sum(1 for x in uni.normal if x.is_index), len(uni.orb), len(uni.mcx), uni.dropped)
    return uni


def pick_option(uni: Universe, underlying: str, spot: float, side: str, today: datetime,
                is_index: bool) -> Optional[OptionRec]:
    """ATM CE for LONG / ATM PE for SHORT on the nearest expiry with >= OPTION_MIN_DAYS left."""
    recs = uni.options.get(underlying) or []
    want = "CE" if side == "LONG" else "PE"
    recs = [r for r in recs if r.otype == want and (r.expiry.date() - today.date()).days >= OPTION_MIN_DAYS]
    if not recs:
        return None
    nearest = min(r.expiry for r in recs)
    recs = [r for r in recs if r.expiry == nearest]
    return min(recs, key=lambda r: abs(r.strike - spot))
