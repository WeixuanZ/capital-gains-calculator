"""Exchange-traded option support for Interactive Brokers exports.

The Transaction History export lists an option trade as a plain Buy or Sell of
its OCC symbol: the underlying padded to six characters, the expiry's year,
month and day in six digits, a C for a call or a P for a put, and the strike
times 1000 in eight digits. It does not say whether a row
opened or closed a position, and it has no row at all for an option that
expired. This module reads each series' rows in date order, works out what
each one did from the running position, and hands the calculator what UK tax
treats them as:

- A purchased option is an asset like any other: buying it is an acquisition
  and selling it a disposal, pooled per series. One that expires without being
  exercised is abandoned, a disposal for nothing, so its cost becomes an allowable loss
  (TCGA 1992 s144(4)(b); a traded option is not a wasting asset, s146(1)(b)).
  Cash-settled index options are treated the same way.
- Writing an option is the grant of an asset, a disposal on the day it was
  written whose proceeds are the premium (s144(1)). Buying it back is an
  allowable cost of that grant (s148), and an expiry leaves the premium as the
  gain. These become ``OPTION_GRANT`` and ``OPTION_CLOSE`` rows, which the
  calculator already handles for Schwab.

An expiry is inferred only where the export runs at least
``OPTION_SETTLEMENT_MATCH_DAYS`` past it and its rows add up to the change in
cash its summary states, so no settlement can be missing. Exercise and
assignment are not supported: where a trade in the underlying could be one,
the parser stops.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import datetime
from decimal import Decimal
import re
from typing import TYPE_CHECKING, Final

from cgt_calc.exceptions import ParsingError
from cgt_calc.logging import LOGGER
from cgt_calc.model import (
    ActionType,
    BrokerTransaction,
    DatedAmount,
    OptionContract,
    OptionType,
    WrittenOptionTaxData,
)
from cgt_calc.parsers.schwab_options import (
    CASH_SETTLED_ROOTS,
    OPTION_ASSIGNMENT_SHARES,
    OPTION_SETTLEMENT_MATCH_DAYS,
)
from cgt_calc.ticker_renames import current_ticker
from cgt_calc.util import strip_zeros

if TYPE_CHECKING:
    from pathlib import Path

_OCC_SYMBOL: Final = re.compile(
    r"(?P<underlying>[A-Z][A-Z.\-]{0,5})\s+(?P<expiry>\d{6})"
    r"(?P<option_type>[CP])(?P<strike>\d{8})"
)
# The export prints amounts to many places, so a penny is a generous margin.
CASH_RECONCILIATION_TOLERANCE: Final = Decimal("0.01")


def parse_ibkr_option(symbol: str, date: datetime.date) -> OptionContract | None:
    """Return the contract an IBKR OCC option symbol names, if it names one."""
    match = _OCC_SYMBOL.fullmatch(symbol)
    if match is None:
        return None
    return OptionContract(
        underlying=current_ticker(match["underlying"], date),
        expiry=datetime.datetime.strptime(match["expiry"], "%y%m%d").date(),
        strike=Decimal(match["strike"]) / 1000,
        option_type=OptionType.CALL if match["option_type"] == "C" else OptionType.PUT,
        multiplier=OPTION_ASSIGNMENT_SHARES,
    )


@dataclass(frozen=True)
class StatementSummary:
    """What an export's header says about the period and cash it covers."""

    period_end: datetime.date | None = None
    starting_cash: Decimal | None = None
    ending_cash: Decimal | None = None

    def reconciles(self, transactions: list[BrokerTransaction]) -> bool:
        """Whether the rows add up to the change in cash the summary states."""
        if self.starting_cash is None or self.ending_cash is None:
            return False
        total = sum((t.amount or Decimal(0) for t in transactions), Decimal(0))
        change = self.ending_cash - self.starting_cash
        return abs(total - change) <= CASH_RECONCILIATION_TOLERANCE


@dataclass
class _Lot:
    """A grant and how many of its contracts are still written."""

    grant: BrokerTransaction
    remaining: Decimal


@dataclass
class _Series:
    """One option series: its signed position and its open written lots."""

    first_date: datetime.date
    last_row: BrokerTransaction
    position: Decimal = Decimal(0)
    written: list[_Lot] = field(default_factory=list)


def _copy(row: BrokerTransaction, **changes: object) -> BrokerTransaction:
    """Copy a row with some fields changed.

    The parser's own row class is built from a CSV row, so it cannot be
    rebuilt field by field the way `dataclasses.replace` would. A shallow
    copy shares the row's lists and dictionaries, which nothing here changes.
    """
    new = copy.copy(row)
    for name, value in changes.items():
        setattr(new, name, value)
    return new


def _part(
    row: BrokerTransaction, quantity: Decimal, **changes: object
) -> BrokerTransaction:
    """Return the share of a row that `quantity` of its contracts make up."""
    assert row.quantity is not None
    assert row.amount is not None
    # Multiplied before dividing, so a whole part of a whole amount stays exact.
    return _copy(
        row,
        quantity=quantity,
        fees=row.fees * quantity / row.quantity,
        amount=row.amount * quantity / row.quantity,
        **changes,
    )


def _apply_trade(
    row: BrokerTransaction, contract: OptionContract, series: _Series
) -> list[BrokerTransaction]:
    """Split a trade into the part closing the position and the part opening one."""
    assert row.quantity is not None
    assert row.price is not None
    buying = row.action is ActionType.BUY
    held = -series.position if buying else series.position
    closing = min(row.quantity, max(held, Decimal(0)))
    opening = row.quantity - closing
    # Grant and close rows state the premium per share, as Schwab's do.
    per_share = row.price / contract.multiplier
    held_as = {"symbol": contract.series, "option_contract": None}
    out = []
    if closing and buying:
        close = _part(row, closing, action=ActionType.OPTION_CLOSE, price=per_share)
        _charge_close(close, series)
        out.append(close)
    elif closing:
        out.append(_part(row, closing, **held_as))
    if opening and buying:
        out.append(_part(row, opening, **held_as))
    elif opening:
        grant = _part(
            row,
            opening,
            action=ActionType.OPTION_GRANT,
            price=per_share,
            written_option_tax=WrittenOptionTaxData(opening),
        )
        series.written.append(_Lot(grant, opening))
        out.append(grant)
    series.position += row.quantity if buying else -row.quantity
    return out


def _charge_close(close: BrokerTransaction, series: _Series) -> None:
    """Charge a buy-back to the grants it closes, oldest first (s148)."""
    assert close.quantity is not None
    assert close.amount is not None
    needed = close.quantity
    for lot in series.written:
        taken = min(needed, lot.remaining)
        if not taken:
            continue
        lot.remaining -= taken
        needed -= taken
        tax = lot.grant.written_option_tax
        assert tax is not None
        tax.close_costs.append(
            DatedAmount(
                close.date, -close.amount * taken / close.quantity, close.currency
            )
        )
        if not needed:
            return
    raise AssertionError("a close was charged beyond the contracts written")


def _refuse_delivery(
    contract: OptionContract,
    series: _Series,
    transactions: list[BrokerTransaction],
    file_path: Path,
) -> None:
    """Stop where a trade in the underlying could be this option's exercise.

    A share row's price is in pounds by now, so the price compared with the
    strike is the one the row was exported with.
    """
    if contract.underlying in CASH_SETTLED_ROOTS:
        return
    last_day = contract.expiry + datetime.timedelta(days=OPTION_SETTLEMENT_MATCH_DAYS)
    for t in transactions:
        if (
            t.option_contract is None
            and t.action in {ActionType.BUY, ActionType.SELL}
            and t.symbol == contract.underlying
            and series.first_date <= t.date <= last_day
            and t.quantity is not None
            and t.quantity % contract.multiplier == 0
            and getattr(t, "price_as_exported", None) == contract.strike
        ):
            raise ParsingError(
                file_path,
                f"The {t.action.name.lower()} of {strip_zeros(t.quantity)} "
                f"{contract.underlying} at {strip_zeros(contract.strike)} on {t.date} "
                f"looks like the exercise or assignment of the {contract}, which is "
                "not supported for Interactive Brokers. Work the option and the "
                "shares out by hand.",
            )


def _expire(
    contract: OptionContract,
    series: _Series,
    summary: StatementSummary,
    file_path: Path,
    *,
    reconciles: bool,
) -> BrokerTransaction | None:
    """Return the disposal an expired purchased option makes, if it has expired."""
    settled_by = contract.expiry + datetime.timedelta(days=OPTION_SETTLEMENT_MATCH_DAYS)
    if summary.period_end is None or summary.period_end < settled_by:
        LOGGER.info(
            "%s contract(s) of the %s are open at the end of the export",
            strip_zeros(abs(series.position)),
            contract,
        )
        return None
    if not reconciles:
        raise ParsingError(
            file_path,
            f"{strip_zeros(abs(series.position))} contract(s) of the {contract} "
            f"are still open after it expired on {contract.expiry}, and the "
            "export has no row for what happened to them. Its rows do not add up "
            "to the change in cash its summary states, so a settlement may be "
            "missing: export the full, unfiltered Transaction History through at "
            f"least {settled_by}.",
        )
    LOGGER.info(
        "%s contract(s) of the %s expired unexercised on %s",
        strip_zeros(abs(series.position)),
        contract,
        contract.expiry,
    )
    if series.position < 0:
        return None  # A written option's grant keeps its premium as the gain.
    name = contract.series
    return _copy(
        series.last_row,
        date=contract.expiry,
        action=ActionType.SELL,
        symbol=name,
        description=f"{name} expired worthless",
        quantity=series.position,
        price=Decimal(0),
        fees=Decimal(0),
        amount=Decimal(0),
        option_contract=None,
    )


def reconcile_ibkr_options(
    transactions: list[BrokerTransaction],
    summary: StatementSummary,
    file_path: Path,
) -> list[BrokerTransaction]:
    """Replace option trades with what UK tax treats them as."""
    trades = [t for t in transactions if t.option_contract is not None]
    if not trades:
        return transactions

    series: dict[OptionContract, _Series] = {}
    replaced: dict[int, list[BrokerTransaction]] = {}

    def reduces(t: BrokerTransaction) -> bool:
        assert t.option_contract is not None
        s = series.get(t.option_contract)
        position = s.position if s else Decimal(0)
        return position < 0 if t.action is ActionType.BUY else position > 0

    for date in sorted({t.date for t in trades}):
        # A day's trades that reduce a position go first, so that a day which
        # closes and reopens is not read as running through zero the wrong way.
        day = sorted(
            (t for t in trades if t.date == date), key=lambda t: not reduces(t)
        )
        for row in day:
            contract = row.option_contract
            assert contract is not None
            if (
                row.action not in {ActionType.BUY, ActionType.SELL}
                or row.quantity is None
                or row.quantity <= 0
                or row.quantity != row.quantity.to_integral_value()
                or row.amount is None
                or row.price is None
            ):
                raise ParsingError(
                    file_path,
                    f"Invalid option trade for {contract} on {row.date}: "
                    f"{row.action.name} of {row.quantity} at {row.price}, "
                    f"amount {row.amount}",
                )
            s = series.setdefault(contract, _Series(first_date=date, last_row=row))
            s.last_row = row
            replaced[id(row)] = _apply_trade(row, contract, s)

    reconciles = summary.reconciles(transactions)
    expiries = []
    for contract, s in series.items():
        _refuse_delivery(contract, s, transactions, file_path)
        if s.position and (
            expiry := _expire(contract, s, summary, file_path, reconciles=reconciles)
        ):
            expiries.append(expiry)

    return [out for t in transactions for out in replaced.get(id(t), [t])] + expiries
