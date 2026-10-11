"""The independent recalculation, against hand-worked figures and cgt-calc."""

from __future__ import annotations

from collections import Counter
import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from scripts.cgt_tools import cross_check
from scripts.cgt_tools.cli import app
from tests.tools.ibkr_export import ibkr_export
from tests.utils import build_cmd, run_cli

if TYPE_CHECKING:
    from pathlib import Path

CALL = "ABC   250620C00010000"
PUT = "ABC   250620P00010000"
ROWS = [
    "2025-01-02,Deposit,-,-,-,-,10000,-,10000,1.0",
    # ABC: 20 pooled at 300; 8 sold, 5 of them matched to the rebuy 19 days later.
    "2025-05-01,Buy,ABC,10,10,GBP,-100,-,-100,1.0",
    "2025-05-10,Buy,ABC,10,20,GBP,-200,-,-200,1.0",
    "2025-06-01,Sell,ABC,-8,30,GBP,240,-,240,1.0",
    "2025-06-20,Buy,ABC,5,25,GBP,-125,-,-125,1.0",
    # ABC: bought and sold the same day.
    "2025-07-01,Buy,ABC,2,20,GBP,-40,-,-40,1.0",
    "2025-07-01,Sell,ABC,-2,25,GBP,50,-,50,1.0",
    # XYZ: 1 share, split 10 for 1 (in the RAW file), then all 10 sold.
    "2025-05-01,Buy,XYZ,1,100,GBP,-100,-,-100,1.0",
    "2025-06-01,Sell,XYZ,-10,15,GBP,150,-,150,1.0",
    # PAR spins off CHD (in the RAW file), with 10% of the value.
    "2025-05-01,Buy,PAR,4,100,GBP,-400,-,-400,1.0",
    "2025-06-02,Sell,CHD,-1,50,GBP,50,-,50,1.0",
    "2025-06-03,Sell,PAR,-4,95,GBP,380,-,380,1.0",
    # A bought call that lapses, and a written put bought back.
    f"2025-05-02,Buy,{CALL},1,2,USD,-100,-1,-101,0.5",
    f"2025-05-05,Sell,{PUT},-1,2,USD,100,-1,99,0.5",
    f"2025-05-20,Buy,{PUT},1,0.4,USD,-20,-1,-21,0.5",
]
EXPECTED = Counter(
    {
        (dt.date(2025, 6, 1), Decimal(70)): 1,  # ABC: 240 - (125 + 3 x 15)
        (dt.date(2025, 7, 1), Decimal(10)): 1,  # ABC: same day
        (dt.date(2025, 6, 1), Decimal(50)): 1,  # XYZ: 150 - 100
        (dt.date(2025, 6, 2), Decimal(10)): 1,  # CHD: 50 - 40
        (dt.date(2025, 6, 3), Decimal(20)): 1,  # PAR: 380 - 360
        (dt.date(2025, 6, 20), Decimal(-101)): 1,  # call lapsed
        (dt.date(2025, 5, 5), Decimal(78)): 1,  # put: 99 - 21
    }
)


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    """Write the account as cgt-calc reads it."""
    raw = tmp_path / "raw.csv"
    raw.write_text(
        "date,action,symbol,quantity,price,fees,currency\n"
        "2025-05-15,STOCK_SPLIT,XYZ,9,0,0,GBP\n"
        "2025-05-20,SPIN_OFF,CHD,1,0,0,GBP\n"
    )
    spin_offs = tmp_path / "spin_offs.csv"
    spin_offs.write_text("dst,src\nCHD,PAR\n")
    prices = tmp_path / "prices.csv"  # PAR 4 x 90 = 360, CHD 1 x 40 = 40: 10%
    prices.write_text(
        'date,symbol,price\n"May 20, 2025",PAR,90\n"May 20, 2025",CHD,40\n'
    )
    return {
        "ibkr": ibkr_export(tmp_path / "ibkr.csv", ROWS),
        "raw": raw,
        "spin_offs": spin_offs,
        "prices": prices,
    }


def recalculate(files: dict[str, Path]) -> Counter[tuple[dt.date, Decimal]]:
    """Recalculate the account's 2025/26 gains with the tool."""
    data = cross_check.read_inputs(
        t212_dir=None, ibkr_file=files["ibkr"], raw_file=files["raw"]
    )
    disposals = cross_check.share_disposals(data, {"CHD": ("PAR", Decimal("0.1"))})
    disposals |= cross_check.option_disposals(data, dt.date(2025, 12, 31))
    return cross_check.gains_in_year(disposals, 2025)


def test_recalculation_matches_hand_worked_figures(files: dict[str, Path]) -> None:
    """Same day, 30 days, pool, split, spin-off and both kinds of option."""
    assert recalculate(files) == EXPECTED


def test_recalculation_agrees_with_cgt_calc(
    files: dict[str, Path], tmp_path: Path
) -> None:
    """cgt-calc's report of the same account has the same disposals."""
    cmd = build_cmd(
        "--year", "2025",
        "--interactive-brokers-file", str(files["ibkr"]),
        "--raw-file", str(files["raw"]),
        "--spin-offs-file", str(files["spin_offs"]),
        "--prices-file", str(files["prices"]),
        "--output", str(tmp_path / "report"),
        keep_tex=True,
    )  # fmt: skip
    run_cli(cmd)
    report = cross_check.report_gains(
        (tmp_path / "report.tex").read_text(encoding="utf-8")
    )
    assert report == EXPECTED
    assert cross_check.differences(recalculate(files), report) == []


def test_differences_name_the_day(files: dict[str, Path]) -> None:
    """A gain more than a penny out is reported against its day."""
    reported = EXPECTED - Counter({(dt.date(2025, 6, 3), Decimal(20)): 1})
    reported[dt.date(2025, 6, 3), Decimal("20.02")] += 1
    assert cross_check.differences(recalculate(files), reported) == [
        (dt.date(2025, 6, 3), [Decimal(20)], [Decimal("20.02")])
    ]


def test_sale_beyond_the_holding_is_refused(tmp_path: Path) -> None:
    """Selling more than is held means the history is incomplete."""
    path = ibkr_export(
        tmp_path / "ibkr.csv", ["2025-06-01,Sell,ABC,-1,10,GBP,10,-,10,1.0"]
    )
    data = cross_check.read_inputs(t212_dir=None, ibkr_file=path, raw_file=None)
    with pytest.raises(ValueError, match="beyond the 0 held"):
        cross_check.share_disposals(data, {})


def test_cli_reports_agreement(files: dict[str, Path], tmp_path: Path) -> None:
    """The command prints the totals and exits 0 when every disposal agrees."""
    report = tmp_path / "report.tex"
    report.write_text(
        "".join(
            f"\\subsection*{{{day:%d %B %Y}}}Chargeable \\textbf{{{'gain' if g > 0 else 'loss'}}}"
            f" is £{abs(g)}\n"
            for (day, g), n in EXPECTED.items()
            for _ in range(n)
        )
    )
    result = CliRunner().invoke(
        app,
        [
            "cross-check", "--year", "2025", "--period-end", "2025-12-31",
            "--report", str(report), "--ibkr-file", str(files["ibkr"]),
            "--raw-file", str(files["raw"]), "--spin-off", "CHD=PAR:0.1",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "gains 238.00, losses 101.00, net 137.00" in result.output
    assert "all 7 disposals in the report agree" in result.output


def t212_history(tmp_path: Path, *rows: str) -> Path:
    """Write a Trading 212 history with the columns the recalculation reads."""
    folder = tmp_path / "t212"
    folder.mkdir()
    (folder / "history.csv").write_text(
        "Action,Time,Ticker,No. of shares,Total,Currency (Total)\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    return folder


def test_trading_212_dates_are_uk_days(tmp_path: Path) -> None:
    """A trade at 23:30 UTC in summer is the next day in the UK (BST)."""
    t212 = t212_history(
        tmp_path,
        "Market buy,2025-06-01 23:30:00,ABC,2,20,GBP",
        "Deposit,2025-06-01 10:00:00,,,100,GBP",
        "Market sell,2025-06-02 09:00:00,ABC,2,26,GBP",
    )
    data = cross_check.read_inputs(t212_dir=t212, ibkr_file=None, raw_file=None)
    # Bought and sold on the same UK day, so matched same day.
    assert cross_check.share_disposals(data, {}) == {
        (dt.date(2025, 6, 2), "ABC"): (Decimal(26), Decimal(20))
    }


def test_trading_212_in_another_currency_is_refused(tmp_path: Path) -> None:
    """Totals in another currency are not pounds."""
    t212 = t212_history(tmp_path, "Market buy,2025-06-01 10:00:00,ABC,2,20,EUR")
    with pytest.raises(ValueError, match="only a GBP"):
        cross_check.read_inputs(t212_dir=t212, ibkr_file=None, raw_file=None)


def test_fees_fund_income_and_raw_trades_change_the_cost(tmp_path: Path) -> None:
    """A Transaction Fee and ERI add to the cost; RAW trades count like others."""
    ibkr = ibkr_export(
        tmp_path / "ibkr.csv",
        [
            "2025-05-01,Buy,ABC,10,10,GBP,-100,-,-100,1.0",
            "2025-05-01,Transaction Fee,ABC,10,-,-,-0.2,-,-0.2,1.0",
            "2025-07-01,Sell,ABC,-10,12,GBP,120,-,120,1.0",
        ],
    )
    raw = tmp_path / "raw.csv"
    raw.write_text(
        "date,action,symbol,quantity,price,fees,currency\n"
        "2025-05-02,BUY,FUND,4,10,1,GBP\n"
        "2025-08-01,SELL,FUND,4,12,1,GBP\n",
        encoding="utf-8",
    )
    data = cross_check.read_inputs(
        t212_dir=None,
        ibkr_file=ibkr,
        raw_file=raw,
        eri=[(dt.date(2025, 6, 30), "FUND", Decimal(3))],
    )
    assert cross_check.share_disposals(data, {}) == {
        (dt.date(2025, 7, 1), "ABC"): (Decimal(120), Decimal("100.2")),
        (dt.date(2025, 8, 1), "FUND"): (Decimal(47), Decimal(44)),  # 41 + 3 of ERI
    }


def test_unmodelled_raw_action_is_refused(tmp_path: Path) -> None:
    """A RAW action the recalculation has no rule for stops it."""
    raw = tmp_path / "raw.csv"
    raw.write_text(
        "date,action,symbol,quantity,price,fees,currency\n2025-05-02,GIFT,ABC,1,0,0,GBP\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="GIFT is not modelled"):
        cross_check.read_inputs(t212_dir=None, ibkr_file=None, raw_file=raw)


def test_bought_option_sold_is_a_disposal_from_its_pool(tmp_path: Path) -> None:
    """Two bought for 101 and 61, one sold for 90: half the pooled 162 is its cost."""
    ibkr = ibkr_export(
        tmp_path / "ibkr.csv",
        [
            f"2025-05-01,Buy,{CALL},1,2,USD,-100,-1,-101,0.5",
            f"2025-05-02,Buy,{CALL},1,1.2,USD,-60,-1,-61,0.5",
            f"2025-05-09,Sell,{CALL},-1,1.8,USD,90,-,90,0.5",
        ],
    )
    data = cross_check.read_inputs(t212_dir=None, ibkr_file=ibkr, raw_file=None)
    assert cross_check.option_disposals(data, dt.date(2025, 6, 1)) == {
        (dt.date(2025, 5, 9), CALL): (Decimal(90), Decimal(81))
    }


def test_cli_fails_on_a_difference_or_bad_input(
    files: dict[str, Path], tmp_path: Path
) -> None:
    """A disagreement, or input that cannot be read, exits non-zero."""
    report = tmp_path / "report.tex"
    report.write_text("\\subsection*{01 June 2025}Chargeable \\textbf{gain} is £1\n")
    base = [
        "cross-check",
        "--year",
        "2025",
        "--period-end",
        "2025-12-31",
        "--report",
        str(report),
    ]
    account = ["--ibkr-file", str(files["ibkr"]), "--raw-file", str(files["raw"])]
    differs = CliRunner().invoke(app, [*base, *account, "--spin-off", "CHD=PAR:0.1"])
    assert differs.exit_code == 1
    assert "2025-06-01: recalculated" in differs.output
    unreadable = CliRunner().invoke(app, [*base, "--eri", "not-a-date,ABC,1"])
    assert unreadable.exit_code == 1
    assert "Error:" in unreadable.output
