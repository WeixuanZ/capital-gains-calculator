"""Find splits, spin-offs and ticker changes that the inputs do not record.

IBKR's Transaction History has no row for a corporate action, and a missing one
often gives no error: a split on a share never sold past it, or a ticker change
on a share still held, goes unnoticed until the share is sold. So each run asks
Yahoo Finance what happened to every share while it was held, and reports:

- a split, or the fractional adjustment Yahoo records for a spin-off, on a day
  the share was held, with no STOCK_SPLIT or SPIN_OFF row (or broker split row)
  within a few days of it;
- a share still held that Yahoo has stopped quoting under its ticker, which
  usually means it was renamed, merged or taken off the exchange.

A share Yahoo cannot be asked about is a warning, not a pass: it was not checked.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from cgt_calc.ticker_renames import current_ticker

if TYPE_CHECKING:
    from collections.abc import Callable

    from .cross_check import Inputs

# A broker or RAW row may be dated a day or two off the exchange's ex-date.
TOLERANCE_DAYS: Final = 3
RECORDED_KINDS: Final = {"STOCK_SPLIT", "SPIN_OFF"}

type Period = tuple[dt.date, dt.date | None]


class MarketDataError(Exception):
    """The market data for a share could not be fetched."""


@dataclass(frozen=True)
class MarketHistory:
    """What the market data says about one ticker."""

    splits: dict[dt.date, Decimal]
    quoted: bool


def yahoo(symbol: str) -> MarketHistory:
    """Ask Yahoo Finance for a ticker's splits and whether it is still quoted."""
    # Imported here because it loads pandas, and only this check needs it.
    import yfinance as yf  # type: ignore[import-untyped]  # noqa: PLC0415

    try:
        ticker = yf.Ticker(symbol)
        recent = ticker.history(period="1mo", auto_adjust=False)
        splits = ticker.splits
    except Exception as err:
        raise MarketDataError(f"{symbol}: {err}") from err
    if splits is None:
        raise MarketDataError(f"{symbol}: no split history")
    return MarketHistory(
        # Each index entry is a timestamp whose text starts with its date.
        {
            dt.date.fromisoformat(str(day)[:10]): Decimal(str(ratio))
            for day, ratio in splits.items()
        },
        not recent.empty,
    )


def held_periods(data: Inputs) -> dict[str, list[Period]]:
    """Return when each share was held: from a purchase until it was all sold."""
    change: dict[str, dict[dt.date, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for day, side, symbol, quantity, _ in data.trades:
        change[symbol][day] += quantity if side == "B" else -quantity
    for day, kind, symbol, quantity, _ in data.events:
        if kind in RECORDED_KINDS:
            change[symbol][day] += quantity
    periods: dict[str, list[Period]] = {}
    for symbol, by_day in change.items():
        held, start = Decimal(0), None
        spans: list[Period] = []
        for day in sorted(by_day):
            # Selling more than the rows hold means something is missing, such
            # as the split this check looks for. Count the holding as gone, so
            # a later purchase still starts a period to check.
            held = max(held + by_day[day], Decimal(0))
            if held > 0 and start is None:
                start = day
            elif held <= 0 and start is not None:
                spans.append((start, day))
                start = None
        if start is not None:
            spans.append((start, None))
        if spans:
            periods[symbol] = spans
    return periods


@dataclass
class Findings:
    """Corporate actions to record, and shares that could not be checked."""

    missing: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)


def _held_at_open(periods: list[Period], day: dt.date) -> bool:
    """Whether shares bought before `day` were still held when it began."""
    return any(start < day and (end is None or end >= day) for start, end in periods)


def _describe(ratio: Decimal) -> str:
    whole = (
        ratio == ratio.to_integral_value()
        or (1 / ratio) == (1 / ratio).to_integral_value()
    )
    return f"split of {ratio}" if whole else f"{ratio} adjustment (usually a spin-off)"


def check(
    data: Inputs,
    spin_off_sources: dict[str, str],
    *,
    lookup: Callable[[str], MarketHistory] | None = None,
) -> Findings:
    """Compare every held share's market history with what the inputs record."""
    recorded = {
        (day, symbol) for day, kind, symbol, *_ in data.events if kind in RECORDED_KINDS
    }
    recorded |= data.broker_splits
    # A spin-off happens to the parent, which is where Yahoo records it.
    recorded |= {
        (day, spin_off_sources[symbol])
        for day, kind, symbol, *_ in data.events
        if kind == "SPIN_OFF" and symbol in spin_off_sources
    }
    findings = Findings()
    for symbol, periods in sorted(held_periods(data).items()):
        name = current_ticker(symbol, periods[0][0])
        market_symbol = f"{name}.L" if symbol in data.london else name
        try:
            history = (lookup or yahoo)(market_symbol)
        except MarketDataError as err:
            findings.unchecked.append(str(err))
            continue
        for day, ratio in sorted(history.splits.items()):
            if not _held_at_open(periods, day):
                continue
            if any(
                held == symbol and abs((day - when).days) <= TOLERANCE_DAYS
                for when, held in recorded
            ):
                continue
            findings.missing.append(
                f"{symbol}: {market_symbol} had a {_describe(ratio)} on {day}, while "
                "it was held, and no STOCK_SPLIT or SPIN_OFF row records it"
            )
        if periods[-1][1] is None and not history.quoted:
            findings.missing.append(
                f"{symbol}: still held, but {market_symbol} has no recent price: it may "
                "have been renamed, merged or delisted"
            )
    return findings
