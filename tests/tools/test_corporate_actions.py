"""Finding corporate actions the inputs do not record."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from typer.testing import CliRunner

from scripts.cgt_tools import corporate_actions, cross_check
from scripts.cgt_tools.cli import app
from scripts.cgt_tools.corporate_actions import (
    MarketDataError,
    MarketHistory,
    check,
    held_periods,
)
from scripts.cgt_tools.cross_check import Inputs
from tests.tools.ibkr_export import ibkr_export

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

D = dt.date
ZERO = Decimal(0)


def trades(*rows: tuple[str, str, str, int]) -> Inputs:
    """Return inputs holding these (date, side, symbol, quantity) trades."""
    data = Inputs()
    data.trades = [
        (D.fromisoformat(d), s, sym, Decimal(q), ZERO) for d, s, sym, q in rows
    ]
    return data


def market(
    **histories: MarketHistory,
) -> Callable[[str], MarketHistory]:
    """Return a market-data lookup that knows only the given tickers."""

    def lookup(symbol: str) -> MarketHistory:
        if symbol not in histories:
            raise MarketDataError(f"{symbol}: unknown")
        return histories[symbol]

    return lookup


QUOTED = MarketHistory({}, quoted=True)


def test_held_periods_end_when_everything_is_sold() -> None:
    """A share sold down to nothing and bought again has two periods."""
    data = trades(
        ("2025-01-02", "B", "ABC", 10),
        ("2025-02-03", "S", "ABC", 4),
        ("2025-03-03", "S", "ABC", 6),
        ("2025-05-01", "B", "ABC", 1),
    )
    assert held_periods(data) == {
        "ABC": [(D(2025, 1, 2), D(2025, 3, 3)), (D(2025, 5, 1), None)]
    }


def test_overselling_still_lets_a_later_purchase_start_a_period() -> None:
    """Selling more than the rows hold (a missing split) does not hide later holdings."""
    data = trades(
        ("2025-01-02", "B", "ABC", 1),
        ("2025-03-03", "S", "ABC", 10),
        ("2025-05-01", "B", "ABC", 2),
    )
    assert held_periods(data) == {
        "ABC": [(D(2025, 1, 2), D(2025, 3, 3)), (D(2025, 5, 1), None)]
    }


def test_unrecorded_split_while_held_is_missing() -> None:
    """A split during the holding with no row for it is reported."""
    data = trades(("2025-01-02", "B", "ABC", 1))
    found = check(
        data, {}, lookup=market(ABC=MarketHistory({D(2025, 6, 2): Decimal(10)}, True))
    )
    assert found.missing == [
        (
            "ABC: ABC had a split of 10 on 2025-06-02, while it was held, and no "
            "STOCK_SPLIT or SPIN_OFF row records it"
        )
    ]


def test_split_recorded_a_day_apart_is_accepted() -> None:
    """A RAW row dated a day off the ex-date still records the split."""
    data = trades(("2025-01-02", "B", "ABC", 1))
    data.events = [(D(2025, 6, 3), "STOCK_SPLIT", "ABC", Decimal(9), ZERO)]
    history = MarketHistory({D(2025, 6, 2): Decimal(10)}, True)
    assert check(data, {}, lookup=market(ABC=history)).missing == []


def test_splits_outside_the_holding_are_ignored() -> None:
    """A split before the purchase or after the sale changes nothing held."""
    data = trades(("2025-03-03", "B", "ABC", 1), ("2025-04-01", "S", "ABC", 1))
    history = MarketHistory(
        {D(2025, 3, 3): Decimal(2), D(2025, 4, 2): Decimal(4)}, True
    )
    assert check(data, {}, lookup=market(ABC=history)).missing == []


def test_spin_off_recorded_for_the_new_share_covers_the_parent() -> None:
    """Yahoo shows a spin-off as a fractional split of the parent."""
    data = trades(("2025-01-02", "B", "PAR", 4))
    data.events = [(D(2025, 6, 2), "SPIN_OFF", "CHD", Decimal(1), ZERO)]
    lookup = market(
        PAR=MarketHistory({D(2025, 6, 2): Decimal("1.08")}, True), CHD=QUOTED
    )
    assert check(data, {"CHD": "PAR"}, lookup=lookup).missing == []
    unrecorded = check(trades(("2025-01-02", "B", "PAR", 4)), {}, lookup=lookup)
    assert unrecorded.missing == [
        (
            "PAR: PAR had a 1.08 adjustment (usually a spin-off) on 2025-06-02, while "
            "it was held, and no STOCK_SPLIT or SPIN_OFF row records it"
        )
    ]


def test_held_share_no_longer_quoted_is_missing() -> None:
    """A held share without a price may have been renamed or merged."""
    data = trades(("2025-01-02", "B", "OLD", 5))
    found = check(data, {}, lookup=market(OLD=MarketHistory({}, quoted=False)))
    assert found.missing == [
        (
            "OLD: still held, but OLD has no recent price: it may have been renamed, "
            "merged or delisted"
        )
    ]


def test_lookup_uses_the_current_ticker_and_the_london_listing() -> None:
    """A known rename is looked up under the new ticker, and a London share as such."""
    data = trades(("2020-01-02", "B", "FB", 5), ("2025-01-02", "B", "UKCO", 5))
    data.london = {"UKCO"}
    asked: list[str] = []

    def lookup(symbol: str) -> MarketHistory:
        asked.append(symbol)
        return QUOTED

    check(data, {}, lookup=lookup)
    assert asked == ["META", "UKCO.L"]


def test_share_that_cannot_be_looked_up_is_unchecked() -> None:
    """No market data is not a pass: the share is named as unchecked."""
    found = check(trades(("2025-01-02", "B", "ABC", 1)), {}, lookup=market())
    assert (found.missing, found.unchecked) == ([], ["ABC: unknown"])


def test_cli_fails_when_something_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command exits non-zero and says what to record."""
    ibkr = ibkr_export(
        tmp_path / "ibkr.csv", ["2025-01-02,Buy,ABC,1,10,GBP,-10,-,-10,1.0"]
    )
    history = MarketHistory({D(2025, 6, 2): Decimal(10)}, True)
    monkeypatch.setattr(corporate_actions, "yahoo", market(**{"ABC.L": history}))
    result = CliRunner().invoke(
        app, ["check-corporate-actions", "--ibkr-file", str(ibkr)]
    )
    assert result.exit_code == 1
    assert "split of 10 on 2025-06-02" in result.output


def test_cli_passes_when_everything_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing missing the command succeeds."""
    ibkr = ibkr_export(
        tmp_path / "ibkr.csv", ["2025-01-02,Buy,ABC,1,10,USD,-5,-,-5,0.5"]
    )
    monkeypatch.setattr(corporate_actions, "yahoo", market(ABC=QUOTED))
    result = CliRunner().invoke(
        app, ["check-corporate-actions", "--ibkr-file", str(ibkr)]
    )
    assert result.exit_code == 0, result.output
    assert "is recorded" in result.output


def test_cli_reads_spin_off_sources_and_reports_bad_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spin-offs file names the parent; an unreadable input exits non-zero."""
    ibkr = ibkr_export(
        tmp_path / "ibkr.csv", ["2025-01-02,Buy,PAR,4,10,USD,-20,-,-20,0.5"]
    )
    raw = tmp_path / "raw.csv"
    raw.write_text(
        "date,action,symbol,quantity,price,fees,currency\n"
        "2025-06-02,SPIN_OFF,CHD,1,0,0,USD\n",
        encoding="utf-8",
    )
    spins = tmp_path / "spins.csv"
    spins.write_text("dst,src\nCHD,PAR\n", encoding="utf-8")
    monkeypatch.setattr(
        corporate_actions,
        "yahoo",
        market(PAR=MarketHistory({D(2025, 6, 2): Decimal("1.1")}, True), CHD=QUOTED),
    )
    args = ["check-corporate-actions", "--ibkr-file", str(ibkr), "--raw-file", str(raw)]
    result = CliRunner().invoke(app, [*args, "--spin-offs-file", str(spins)])
    assert result.exit_code == 0, result.output
    raw.write_text(
        "date,action,symbol,quantity,price,fees,currency\n2025-06-02,GIFT,PAR,1,0,0,USD\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "Error:" in result.output


def test_trading_212_marks_london_listings_and_records_its_own_splits(
    tmp_path: Path,
) -> None:
    """A share priced in pence is a London listing, and Trading 212 split rows count."""
    folder = tmp_path / "t212"
    folder.mkdir()
    (folder / "history.csv").write_text(
        "Action,Time,Ticker,No. of shares,Currency (Price / share),Total,"
        "Currency (Total)\n"
        "Market buy,2025-01-02 10:00:00,UKCO,5,GBX,10,GBP\n"
        "Stock split open,2025-06-02 07:00:00,UKCO,10,GBX,0,GBP\n",
        encoding="utf-8",
    )
    data = cross_check.read_inputs(t212_dir=folder, ibkr_file=None, raw_file=None)
    assert data.london == {"UKCO"}
    assert data.broker_splits == {(D(2025, 6, 2), "UKCO")}
    history = MarketHistory({D(2025, 6, 2): Decimal(2)}, True)
    assert check(data, {}, lookup=market(**{"UKCO.L": history})).missing == []


class FakeTicker:
    """Stands in for yfinance.Ticker with fixed answers."""

    def __init__(self, recent: pd.DataFrame, splits: pd.Series[float] | None) -> None:
        """Answer with this recent history and these splits."""
        self.recent = recent
        self.splits = splits

    def history(self, *, period: str, auto_adjust: bool) -> pd.DataFrame:
        """Return the recent prices."""
        assert (period, auto_adjust) == ("1mo", False)
        return self.recent


def fake_yfinance(
    monkeypatch: pytest.MonkeyPatch, ticker: FakeTicker | Exception
) -> None:
    """Install a yfinance module whose Ticker gives `ticker` or raises it."""

    def make(symbol: str) -> FakeTicker:
        assert symbol == "ABC"
        if isinstance(ticker, Exception):
            raise ticker
        return ticker

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=make))


def test_yahoo_reads_splits_and_whether_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Splits come back by day as decimals; an empty month means no quote."""
    splits = pd.Series([10.0], index=pd.to_datetime(["2025-06-02"]).tz_localize("UTC"))
    fake_yfinance(monkeypatch, FakeTicker(pd.DataFrame(), splits))
    assert corporate_actions.yahoo("ABC") == MarketHistory(
        {D(2025, 6, 2): Decimal(10)}, quoted=False
    )


@pytest.mark.parametrize(
    "ticker",
    [RuntimeError("HTTP 404"), FakeTicker(pd.DataFrame({"Close": [1.0]}), None)],
    ids=["lookup-fails", "no-split-history"],
)
def test_yahoo_failures_are_market_data_errors(
    monkeypatch: pytest.MonkeyPatch, ticker: FakeTicker | Exception
) -> None:
    """Whatever yfinance raises or leaves out, the check sees one error type."""
    fake_yfinance(monkeypatch, ticker)
    with pytest.raises(MarketDataError, match="ABC"):
        corporate_actions.yahoo("ABC")
