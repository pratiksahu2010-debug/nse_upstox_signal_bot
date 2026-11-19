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
    configured_symbols: list = field(default_factory=list)
    ready: bool = True
    validation_errors: list = field(default_factory=list)
    unavailable_symbols: list = field(default_factory=list)


def _pick_future(futs: list, today: datetime, roll_days: int = 2):
    live = sorted((f for f in futs if f[0].date() >= today.date()), key=lambda x: x[0])
    if not live:
        return None
    return next((f for f in live if (f[0].date() - today.date()).days >= roll_days), live[-1])


def resolve_nse(rows: Iterable[dict], today: datetime, stock_candidates: list,
                stock_reserve: list, extra_candidates: list = None, extra_reserve: list = None,
                index_allowlist: list = None, require_options: bool = False) -> Universe:
    """Resolve selected equity F&O underlyings and dynamically discover index derivatives.

    Index futures are identified by Upstox's documented underlying_type=INDEX. Equity
    underlyings are validated against cash EQ, FUT, CE and PE records. require_options
    is enabled by production so every stock signal can map to both option directions.
    """
    extra_candidates = extra_candidates or []
    extra_reserve = extra_reserve or []
    explicit_indices = set(index_allowlist or [])
    want = set(stock_candidates) | set(stock_reserve) | set(extra_candidates) | set(extra_reserve)
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
            is_index = underlying_type == "INDEX" or und in explicit_indices
            if is_index and (not explicit_indices or und in explicit_indices):
                index_futs.setdefault(und, []).append(item)
            elif und in want:
                fut_by_und.setdefault(und, []).append(item)
        elif typ in ("CE", "PE") and exp <= horizon:
            is_index = underlying_type == "INDEX" or und in explicit_indices
            if und in want or (is_index and (not explicit_indices or und in explicit_indices)):
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
    for name in list(stock_candidates) + list(stock_reserve) + list(extra_candidates) + list(extra_reserve):
        if name in seen:
            continue
        seen.add(name)
        if len([x for x in uni.normal if not x.is_index]) >= len(stock_candidates):
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
    S.validate_fixed_universe()
    uni = resolve_nse(
        rows, today, S.FIXED_STOCK_SYMBOLS, [],
        index_allowlist=S.FIXED_INDEX_SYMBOLS, require_options=True,
    )
    uni.configured_symbols = list(S.FIXED_FNO_SYMBOLS)
    stock_count = sum(not item.is_index for item in uni.normal)
    index_count = sum(item.is_index for item in uni.normal)
    log.info(
        "fixed F&O universe: configured=%d active=%d stocks=%d indices=%d invalid=%s",
        len(uni.configured_symbols), len(uni.normal), stock_count, index_count, uni.dropped,
    )
    configured = set(uni.configured_symbols)
    validated = {item.symbol for item in uni.normal}
    missing = sorted(configured - validated)
    unexpected = sorted(validated - configured)
    validation_errors = []
    if len(uni.configured_symbols) != S.EXPECTED_UNIVERSE_SIZE:
        validation_errors.append(
            f"configured universe count {len(uni.configured_symbols)} != {S.EXPECTED_UNIVERSE_SIZE}"
        )
    # Missing contracts are reported but are not silently replaced. A valid
    # configured list can still scan the remaining validated instruments.
    uni.unavailable_symbols = missing
    if unexpected:
        validation_errors.append(f"unexpected underlyings: {unexpected}")
    uni.validation_errors = validation_errors
    uni.ready = not validation_errors
    if not uni.ready:
        log.error("fixed F&O universe is incomplete: %s", validation_errors)
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
