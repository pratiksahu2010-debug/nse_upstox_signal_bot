"""Resolve one NSE midcap F&O stock universe plus every supported index derivative."""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from . import symbols as S
from .common import SymbolInfo
from .config import IST, OPTION_MIN_DAYS
from .upstox import expiry_to_date, stream_master

log = logging.getLogger("universe")


@dataclass
class OptionRec:
    expiry: datetime
    strike: float
    otype: str
    key: str
    tsym: str
    lot: int


@dataclass
class Universe:
    normal: list = field(default_factory=list)
    options: dict = field(default_factory=dict)
    dropped: dict = field(default_factory=dict)
    built_on: str = ""


def _pick_future(futs: list, today: datetime, roll_days: int = 2):
    live = sorted((f for f in futs if f[0].date() >= today.date()), key=lambda x: x[0])
    if not live:
        return None
    return next((f for f in live if (f[0].date() - today.date()).days >= roll_days), live[-1])


def resolve_nse(rows: Iterable[dict], today: datetime, normal_names: list,
                normal_reserve: list, orb_names: list = None, orb_reserve: list = None,
                indices: list = None, require_options: bool = False) -> Universe:
    """Resolve selected equity F&O underlyings and dynamically discover index derivatives.

    Index futures are identified by Upstox's documented underlying_type=INDEX. Equity
    underlyings are validated against cash EQ, FUT, CE and PE records. require_options
    is enabled by production so every stock signal can map to both option directions.
    """
    other_names, other_reserve, indices = orb_names or [], orb_reserve or [], indices or []
    want = set(normal_names) | set(normal_reserve) | set(other_names) | set(other_reserve)
    explicit_indices = set(indices)
    eq: dict = {}
    fut_by_und: dict = {}
    index_futs: dict = {}
    opts: dict = {}
    horizon = today + timedelta(days=45)

    for row in rows:
        seg = row.get("segment")
        if seg == "NSE_EQ":
            sym = row.get("trading_symbol")
            if sym in want and row.get("instrument_type") == "EQ":
                eq[sym] = row
            continue
        if seg != "NSE_FO":
            continue
        und = row.get("underlying_symbol")
        exp = expiry_to_date(row.get("expiry"))
        if not und or exp is None or exp.date() < today.date():
            continue
        typ = row.get("instrument_type")
        underlying_type = str(row.get("underlying_type") or "").upper()
        if typ == "FUT":
            item = (exp, row.get("instrument_key"), int(row.get("lot_size") or 0),
                    row.get("trading_symbol", ""), float(row.get("tick_size") or 0.05))
            if underlying_type == "INDEX" or und in explicit_indices:
                index_futs.setdefault(und, []).append(item)
            elif und in want:
                fut_by_und.setdefault(und, []).append(item)
        elif (typ in ("CE", "PE") and exp <= horizon
              and (und in want or underlying_type == "INDEX" or und in explicit_indices)):
            opts.setdefault(und, []).append(OptionRec(
                exp, float(row.get("strike_price") or 0), typ,
                row.get("instrument_key", ""), row.get("trading_symbol", ""),
                int(row.get("lot_size") or 0)))

    uni = Universe(built_on=today.strftime("%Y-%m-%d"), options=opts)
    dropped_stocks = []
    dropped_indices = sorted(explicit_indices - set(index_futs))

    def has_both_sides(name: str) -> bool:
        eligible = [o for o in opts.get(name, [])
                    if (o.expiry.date() - today.date()).days >= OPTION_MIN_DAYS]
        ce_expiries = {o.expiry for o in eligible if o.otype == "CE"}
        pe_expiries = {o.expiry for o in eligible if o.otype == "PE"}
        return bool(ce_expiries & pe_expiries)

    # Every index derivative discovered in the master, not a hard-coded five-index list.
    for name, futs in sorted(index_futs.items()):
        if require_options and not has_both_sides(name):
            dropped_indices.append(name)
            continue
        future = _pick_future(futs, today)
        if future:
            uni.normal.append(SymbolInfo(
                name, future[1], "normal", lot=future[2], tick=0.05,
                is_index=True, expiry=future[0].strftime("%d%b%y").upper(),
                underlying=name))
        else:
            dropped_indices.append(name)

    seen = set()
    for name in list(normal_names) + list(normal_reserve):
        if name in seen:
            continue
        seen.add(name)
        if len([x for x in uni.normal if not x.is_index]) >= 100:
            break
        cash = eq.get(name)
        futs = fut_by_und.get(name)
        if not cash or not futs or (require_options and not has_both_sides(name)):
            dropped_stocks.append(name)
            continue
        future = _pick_future(futs, today)
        if not future:
            dropped_stocks.append(name)
            continue
        uni.normal.append(SymbolInfo(name, cash["instrument_key"], "normal",
                                     lot=future[2], tick=0.05, underlying=name))

    uni.dropped = {
        "normal": dropped_stocks,
        "indices": dropped_indices,
    }
    return uni


def build_universe(today: Optional[datetime] = None, nse_rows=None) -> Universe:
    today = today or datetime.now(IST)
    rows = nse_rows if nse_rows is not None else stream_master("NSE")
    uni = resolve_nse(rows, today, S.MIDCAP_FNO_CANDIDATES, S.MIDCAP_FNO_RESERVE,
                      require_options=True)
    stock_count = sum(not item.is_index for item in uni.normal)
    index_count = sum(item.is_index for item in uni.normal)
    log.info("universe ready: midcap_fno=%d/100 indices=%d | dropped=%s",
             stock_count, index_count, uni.dropped)
    if not uni.normal:
        raise RuntimeError("NSE master resolved no eligible midcap stocks or index derivatives")
    return uni


def pick_option(uni: Universe, underlying: str, spot: float, side: str,
                today: datetime, is_index: bool) -> Optional[OptionRec]:
    """Nearest available expiry, ATM CE for LONG or PE for SHORT."""
    records = uni.options.get(underlying) or []
    wanted = "CE" if side == "LONG" else "PE"
    records = [r for r in records if r.otype == wanted
               and (r.expiry.date() - today.date()).days >= OPTION_MIN_DAYS]
    if not records:
        return None
    expiry = min(r.expiry for r in records)
    same_expiry = [r for r in records if r.expiry == expiry]
    return min(same_expiry, key=lambda r: abs(r.strike - spot))
