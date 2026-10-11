"""Merging IBKR Transaction History exports."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from cgt_calc.model import ActionType
from cgt_calc.parsers.interactive_brokers import InteractiveBrokersParser
from scripts.cgt_tools.ibkr_merge import MergeError, merge, read_export, write
from tests.tools.ibkr_export import ibkr_export

if TYPE_CHECKING:
    from pathlib import Path

DEPOSIT = "2025-01-02,Deposit,-,-,-,-,1000,-,1000,1.0"
BUY = "2025-03-03,Buy,ABC,10,10,GBP,-100,-1,-101,1.0"
SELL = "2025-08-01,Sell,ABC,-10,12,GBP,120,-1,119,1.0"


def first_half(tmp_path: Path, *extra: str) -> Path:
    """Write an export of January to June."""
    return ibkr_export(
        tmp_path / "a.csv",
        [DEPOSIT, BUY, "2025-06-30,FX,-,-,-,-,-2,-,-2,1.0", *extra],
        period="January 1, 2025 - June 30, 2025",
        generated="2025-07-01, 10:00:00 EST",
    )


def second_half(tmp_path: Path, *extra: str) -> Path:
    """Write a later export of March to December, overlapping the first."""
    return ibkr_export(
        tmp_path / "b.csv",
        [BUY, SELL, "2025-12-31,FX,-,-,-,-,5,-,5,1.0", *extra],
        period="March 1, 2025 - December 31, 2025",
        generated="2026-01-01, 10:00:00 EST",
        starting_cash=Decimal(1000),
    )


def test_overlap_is_read_once_and_cash_still_adds_up(tmp_path: Path) -> None:
    """The newer export supplies the overlap; FX revaluations become one row."""
    merged = merge(
        [read_export(first_half(tmp_path)), read_export(second_half(tmp_path))]
    )
    kinds = [(r["Date"], r["Transaction Type"]) for r in merged.rows]
    assert kinds == [
        ("2025-12-31", "Adjustment"),
        ("2025-08-01", "Sell"),
        ("2025-03-03", "Buy"),
        ("2025-01-02", "Deposit"),
    ]
    assert merged.used == {"b.csv": 3, "a.csv": 1}
    # Rows sum to 1000 - 101 + 119 = 1018; the latest export ends at 1023.
    assert (merged.starting_cash, merged.ending_cash) == (Decimal(0), Decimal(1023))
    assert merged.rows[0]["Net Amount"] == "5"


def test_written_file_is_read_by_cgt_calc(tmp_path: Path) -> None:
    """The merged file is an export cgt-calc accepts, with every row in it."""
    merged = merge(
        [read_export(first_half(tmp_path)), read_export(second_half(tmp_path))]
    )
    write(merged, tmp_path / "merged.csv")
    actions = [
        t.action
        for t in InteractiveBrokersParser.load_from_file(tmp_path / "merged.csv")
    ]
    assert actions == [
        ActionType.TRANSFER,
        ActionType.BUY,
        ActionType.SELL,
        ActionType.ADJUSTMENT,
    ]
    assert read_export(tmp_path / "merged.csv").ending_cash == Decimal(1023)


def test_disagreeing_overlap_is_refused(tmp_path: Path) -> None:
    """Exports that tell the overlap differently cannot both be right."""
    a = read_export(first_half(tmp_path, "2025-04-01,Deposit,-,-,-,-,5,-,5,1.0"))
    with pytest.raises(MergeError, match="disagree where they overlap"):
        merge([a, read_export(second_half(tmp_path))])


def test_export_that_does_not_add_up_is_refused(tmp_path: Path) -> None:
    """A filtered export no longer adds up to the cash its summary states."""
    path = first_half(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace("Ending Cash,897", "Ending Cash,900"),
        encoding="utf-8",
    )
    with pytest.raises(MergeError, match="filtered or incomplete"):
        read_export(path)


def test_non_gbp_account_is_refused(tmp_path: Path) -> None:
    """Amounts in another base currency would be read as pounds."""
    path = first_half(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Base Currency,GBP", "Base Currency,USD"
        ),
        encoding="utf-8",
    )
    with pytest.raises(MergeError, match="not GBP"):
        read_export(path)


@pytest.mark.parametrize(
    ("row", "rate"),
    [
        ("2025-03-03,Buy,ABC,10,2,USD,-16,-1,-17,-", "0.8000000"),
        ("2025-03-03,Buy,ABC   250620C00010000,2,0.5,USD,-80,-1,-81,-", "0.8000000"),
        ("2025-03-03,Deposit,-,-,-,-,1000,-,1000,-", "1.0"),
    ],
    ids=["share", "option-per-100-shares", "not-a-trade"],
)
def test_missing_exchange_rate_is_filled_from_the_row(
    tmp_path: Path, row: str, rate: str
) -> None:
    """Gross Amount over quantity x price (x 100 for an option) is the rate."""
    path = ibkr_export(tmp_path / "old.csv", [row], exchange_rate_column=False)
    (filled,) = [r for r in merge([read_export(path)]).rows if r["Description"] == "X"]
    assert filled["Exchange Rate"] == rate


def test_dropped_row_takes_its_cash_with_it(tmp_path: Path) -> None:
    """A row moved to a RAW file leaves the rows and the ending cash together."""
    merged = merge(
        [read_export(first_half(tmp_path)), read_export(second_half(tmp_path))],
        [("2025-08-01", "Sell", "ABC")],
    )
    assert [r["Transaction Type"] for r in merged.dropped] == ["Sell"]
    assert merged.ending_cash == Decimal(1023 - 119)


def test_drop_must_match_exactly_one_row(tmp_path: Path) -> None:
    """A drop that misses, or hits two rows, says so rather than guessing."""
    with pytest.raises(MergeError, match="matches 0 rows"):
        merge([read_export(first_half(tmp_path))], [("2025-06-02", "Sell", "XYZ")])


def test_nothing_to_merge_is_refused() -> None:
    """An empty merge would write a file with no account in it."""
    with pytest.raises(MergeError, match="no exports"):
        merge([])


def test_unknown_column_is_refused(tmp_path: Path) -> None:
    """A column the merge does not know would be dropped from the output."""
    path = first_half(tmp_path)
    text = path.read_text(encoding="utf-8").replace(",1.0\n", ",1.0,\n")
    path.write_text(
        text.replace("Exchange Rate\n", "Exchange Rate,Notes\n"), encoding="utf-8"
    )
    with pytest.raises(MergeError, match="unexpected columns"):
        read_export(path)


def test_unreadable_period_is_refused(tmp_path: Path) -> None:
    """Without a period the merge cannot tell which export covers a date."""
    path = first_half(tmp_path)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("January 1, 2025 - June 30, 2025", "soon"), encoding="utf-8"
    )
    with pytest.raises(MergeError, match="no readable period"):
        read_export(path)
