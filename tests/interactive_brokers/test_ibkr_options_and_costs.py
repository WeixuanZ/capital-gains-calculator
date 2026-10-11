"""Interactive Brokers options, gilts and the costs a trade's Net Amount holds.

Each row is written as the export has it, from Date to Exchange Rate with the
Account and Description left out:
Date,Transaction Type,Symbol,Quantity,Price,Price Currency,Gross Amount,
Commission,Net Amount,Exchange Rate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import re
from typing import TYPE_CHECKING

import pytest

from cgt_calc.exceptions import ParsingError
from cgt_calc.model import ActionType, BrokerTransaction, DatedAmount
from cgt_calc.parsers.interactive_brokers import InteractiveBrokersParser
from tests.utils import build_cmd, run_cli, stderr_alerts

if TYPE_CHECKING:
    from pathlib import Path

CALL = "ABC   250620C00010000"  # ABC call, 20 June 2025, strike 10
PUT = "ABC   250620P00010000"
SERIES = "ABC 20JUN25 10 C"
DEPOSIT = "2025-05-01,Deposit,-,-,-,-,1000,-,1000,1.0"
HEADER = (
    "Transaction History,Header,Date,Account,Description,Transaction Type,"
    "Symbol,Quantity,Price,Price Currency,Gross Amount ,Commission,Net Amount,"
    "Exchange Rate\n"
)


def export(
    tmp_path: Path,
    *rows: str,
    period: str | None = "May 1, 2025 - December 31, 2025",
    cash_change: Decimal | None = None,
    summary: bool = True,
) -> Path:
    """Write an export whose cash summary agrees with its rows by default."""
    if cash_change is None:
        nets = (r.split(",")[8] for r in rows)
        cash_change = sum((Decimal(n) for n in nets if n != "-"), Decimal(0))
    lines = ["Statement,Header,Field Name,Field Value"]
    if period:
        lines.append(f'Statement,Data,Period,"{period}"')
    lines += ["Summary,Header,Field Name,Field Value", "Summary,Data,Base Currency,GBP"]
    if summary:
        lines += [
            "Summary,Data,Starting Cash,0",
            f"Summary,Data,Ending Cash,{cash_change}",
        ]
    data = (
        "Transaction History,Data,{},U***00000,X,{}".format(*r.split(",", 1))
        for r in rows
    )
    path = tmp_path / "transactions.csv"
    path.write_text(
        "\n".join(lines) + "\n" + HEADER + "\n".join(data) + "\n", encoding="utf-8"
    )
    return path


def load(path: Path) -> list[BrokerTransaction]:
    """Parse an export, leaving out any deposit that funds it."""
    return [
        t
        for t in InteractiveBrokersParser.load_from_file(path)
        if t.action is not ActionType.TRANSFER
    ]


def close_costs(grant: BrokerTransaction) -> list[DatedAmount]:
    """Return the buy-back costs charged to a written option's grant."""
    assert grant.written_option_tax is not None
    return grant.written_option_tax.close_costs


def tax_summary(tmp_path: Path, *rows: str) -> dict[str, str]:
    """Run the 2025/26 calculation and return its capital gains figures."""
    path = export(tmp_path, DEPOSIT, *rows)
    cmd = build_cmd(
        "--year", "2025", "--interactive-brokers-file", str(path), "--no-report"
    )
    result = run_cli(cmd)
    assert stderr_alerts(result.stderr) == []
    return dict(re.findall(r"\n  ([A-Za-z ]+):\s+(£[\d,.]+)", result.stdout))


# Costs held in Net Amount

UK_BUY = "2025-08-01,Buy,UKCO,10,20,GBP,-200,-3,-204,1.0"


def test_stamp_duty_in_net_amount_is_a_cost_of_the_purchase(tmp_path: Path) -> None:
    """IBKR adds UK stamp duty to Net Amount but not to Commission."""
    (buy,) = load(export(tmp_path, UK_BUY))
    assert (buy.fees, buy.amount) == (Decimal(4), Decimal(-204))


def test_net_amount_better_than_gross_and_commission_is_refused(
    tmp_path: Path,
) -> None:
    """Paying less than gross plus commission is no cost, so it is not read as one."""
    path = export(tmp_path, "2025-08-01,Buy,UKCO,10,20,GBP,-200,-3,-190,1.0")
    with pytest.raises(ParsingError, match="not a cost of the trade"):
        load(path)


def test_stamp_duty_reduces_the_gain(tmp_path: Path) -> None:
    """Bought for 204 with duty, sold for 250 less 3 commission: gain 43."""
    figures = tax_summary(
        tmp_path, UK_BUY, "2025-09-01,Sell,UKCO,-10,25,GBP,250,-3,247,1.0"
    )
    assert figures["Allowable costs"] == "£207.00"
    assert figures["Gain"] == "£43.00"


def test_transaction_fee_is_shared_by_that_days_trades(tmp_path: Path) -> None:
    """A charge, its cancellation and its recharge net to one cost, by quantity."""
    trades = load(
        export(
            tmp_path,
            "2025-05-20,Buy,ESCO,100,5,GBP,-500,-1,-501,1.0",
            "2025-05-20,Buy,ESCO,200,5.1,GBP,-1020,-1,-1021,1.0",
            "2025-05-20,Transaction Fee,ESCO,300,-,-,-3,-,-3,1.0",
            "2025-05-20,Transaction Fee,ESCO,300,-,-,3,-,3,1.0",
            "2025-05-20,Transaction Fee,ESCO,300,-,-,-3.3,-,-3.3,1.0",
        )
    )
    assert [(t.fees, t.amount) for t in trades] == [
        (Decimal("2.1"), Decimal("-502.1")),
        (Decimal("3.2"), Decimal("-1023.2")),
    ]


def test_transaction_fee_without_a_trade_is_refused(tmp_path: Path) -> None:
    """A security's fee with no trade that day has nothing to be a cost of."""
    path = export(tmp_path, "2025-05-20,Transaction Fee,ESCO,300,-,-,-2,-,-2,1.0")
    with pytest.raises(ParsingError, match="no trade in ESCO"):
        load(path)


@pytest.mark.parametrize(
    "charge",
    [
        "2025-07-01,Transaction Fee,-,-,-,-,-2,-,-2,1.0",
        "2025-07-01,Investment Interest Paid,UKT 4 1/4 12/07/27,-,-,-,-2,-,-2,1.0",
    ],
)
def test_account_charges_move_cash_only(tmp_path: Path, charge: str) -> None:
    """A fee naming no security, and a bond's accrued interest, are no CGT cost."""
    (row,) = load(export(tmp_path, charge))
    assert (row.action, row.symbol, row.amount) == (
        ActionType.ADJUSTMENT,
        None,
        Decimal(-2),
    )


# Bonds


def test_gilt_trade_moves_cash_only(tmp_path: Path) -> None:
    """A gilt is exempt (TCGA 1992 s115), so it is no holding and no disposal."""
    (gilt,) = load(
        export(
            tmp_path,
            "2025-07-01,Buy,UKT 4 1/4 12/07/27,1000,101.5,GBP,-1015,-2,-1017,1.0",
        )
    )
    assert (gilt.action, gilt.symbol, gilt.amount) == (
        ActionType.ADJUSTMENT,
        None,
        Decimal(-1017),
    )


def test_other_bond_is_refused(tmp_path: Path) -> None:
    """A corporate bond may or may not be exempt, which a row cannot show."""
    path = export(
        tmp_path, "2025-07-01,Buy,ACME 5 1/2 01/15/30,1000,101,GBP,-1010,-2,-1012,1.0"
    )
    with pytest.raises(ParsingError, match="only UK gilts"):
        load(path)


# Options: what each trade is


def test_purchased_option_is_pooled_as_its_series(tmp_path: Path) -> None:
    """Bought and sold, a purchased option is an ordinary holding of its series."""
    buy, sell = load(
        export(
            tmp_path,
            f"2025-06-02,Buy,{CALL},1,2,USD,-100,-1,-101,0.5",
            f"2025-06-10,Sell,{CALL},-1,3,USD,150,-1,149,0.5",
        )
    )
    # Priced per contract in pounds: 2 dollars x 100 shares x 0.5.
    assert (buy.action, buy.symbol, buy.price, buy.fees, buy.amount) == (
        ActionType.BUY,
        SERIES,
        Decimal(100),
        Decimal(1),
        Decimal(-101),
    )
    assert (sell.action, sell.symbol, sell.price, sell.amount) == (
        ActionType.SELL,
        SERIES,
        Decimal(150),
        Decimal(149),
    )
    assert buy.option_contract is None


def test_written_option_closed_charges_its_grant(tmp_path: Path) -> None:
    """Selling first writes the option; buying back is a cost of that grant."""
    grant, close = load(
        export(
            tmp_path,
            f"2025-06-02,Sell,{PUT},-1,2,USD,100,-1,99,0.5",
            f"2025-06-10,Buy,{PUT},1,0.4,USD,-20,-1,-21,0.5",
        )
    )
    assert (grant.action, grant.price) == (ActionType.OPTION_GRANT, Decimal(1))
    assert grant.written_option_tax is not None
    assert grant.written_option_tax.taxable_quantity == 1
    assert close_costs(grant) == [
        DatedAmount(date(2025, 6, 10), Decimal(21), grant.currency)
    ]
    assert (close.action, close.amount) == (ActionType.OPTION_CLOSE, Decimal(-21))


def test_trade_through_zero_closes_then_opens(tmp_path: Path) -> None:
    """Long 1 and selling 3 sells the 1 held and writes 2, sharing the costs."""
    rows = load(
        export(
            tmp_path,
            f"2025-06-02,Buy,{CALL},1,2,USD,-100,-1,-101,0.5",
            f"2025-06-10,Sell,{CALL},-3,3,USD,450,-3,447,0.5",
        )
    )
    assert {t.action: (t.quantity, t.fees, t.amount) for t in rows[1:]} == {
        ActionType.SELL: (Decimal(1), Decimal(1), Decimal(149)),
        ActionType.OPTION_GRANT: (Decimal(2), Decimal(2), Decimal(298)),
    }


def test_buy_backs_charge_grants_oldest_first(tmp_path: Path) -> None:
    """Closing 3 then 1 of 2 + 2 written charges each grant for its own two."""
    first, second, *_ = load(
        export(
            tmp_path,
            f"2025-06-02,Sell,{PUT},-2,1,USD,100,-,100,0.5",
            f"2025-06-03,Sell,{PUT},-2,1,USD,100,-,100,0.5",
            f"2025-06-10,Buy,{PUT},3,0.2,USD,-30,-,-30,0.5",
            f"2025-06-11,Buy,{PUT},1,0.4,USD,-20,-,-20,0.5",
        )
    )
    assert [c.amount for c in close_costs(first)] == [Decimal(20)]
    assert [c.amount for c in close_costs(second)] == [Decimal(10), Decimal(20)]


def test_part_contract_is_refused(tmp_path: Path) -> None:
    """An option trades in whole contracts, so a fraction is a broken row."""
    path = export(tmp_path, f"2025-06-02,Buy,{CALL},0.5,2,USD,-50,-,-50,0.5")
    with pytest.raises(ParsingError, match="Invalid option trade"):
        load(path)


# Options: expiry

CALL_BOUGHT = f"2025-06-02,Buy,{CALL},1,2,USD,-100,-1,-101,0.5"


def test_expired_purchased_option_is_disposed_of_for_nothing(tmp_path: Path) -> None:
    """An option still held after it expired was abandoned (s144(4))."""
    _, expiry = load(export(tmp_path, CALL_BOUGHT))
    assert (expiry.date, expiry.action, expiry.symbol, expiry.quantity) == (
        date(2025, 6, 20),
        ActionType.SELL,
        SERIES,
        Decimal(1),
    )
    assert expiry.amount == expiry.price == expiry.fees == 0


def test_expired_written_option_keeps_its_grant(tmp_path: Path) -> None:
    """A written option that expires adds no row: the premium stays the gain."""
    (grant,) = load(export(tmp_path, f"2025-06-02,Sell,{PUT},-1,2,USD,100,-1,99,0.5"))
    assert grant.action is ActionType.OPTION_GRANT
    assert close_costs(grant) == []


@pytest.mark.parametrize(
    "period", ["May 1, 2025 - June 26, 2025", None], ids=["too-short", "no-period"]
)
def test_expiry_is_not_assumed_before_the_export_shows_settlement(
    tmp_path: Path, period: str | None
) -> None:
    """Assignment can settle days after expiry, so the export must run past it."""
    rows = load(export(tmp_path, CALL_BOUGHT, period=period))
    assert [t.action for t in rows] == [ActionType.BUY]


@pytest.mark.parametrize(
    ("cash_change", "summary"),
    [(Decimal(50), True), (None, False)],
    ids=["cash-missing", "no-summary"],
)
def test_expiry_is_not_inferred_without_cash_that_adds_up(
    tmp_path: Path, cash_change: Decimal | None, *, summary: bool
) -> None:
    """Rows that do not add up to the stated cash may be missing a settlement."""
    path = export(tmp_path, CALL_BOUGHT, cash_change=cash_change, summary=summary)
    with pytest.raises(ParsingError, match="settlement may be missing"):
        load(path)


def test_share_trade_at_the_strike_is_refused_as_an_exercise(tmp_path: Path) -> None:
    """100 shares at the strike near expiry may be the option's delivery."""
    path = export(
        tmp_path,
        f"2025-06-02,Sell,{PUT},-1,2,USD,100,-1,99,0.5",
        "2025-06-23,Buy,ABC,100,10,USD,-500,-1,-501,0.5",
    )
    with pytest.raises(ParsingError, match="exercise or assignment"):
        load(path)


def test_cash_settled_option_expires_without_a_delivery_check(tmp_path: Path) -> None:
    """An index option delivers no shares, so expiry needs no share row."""
    _, expiry = load(
        export(
            tmp_path, "2025-06-02,Buy,SPX   250620P05000000,1,2,USD,-100,-1,-101,0.5"
        )
    )
    assert (expiry.symbol, expiry.amount) == ("SPX 20JUN25 5000 P", Decimal(0))


# Options and listings: the tax they come to


def test_expired_option_is_a_loss_of_its_cost(tmp_path: Path) -> None:
    """Bought for 100 plus 1 commission and expired: a loss of 101."""
    figures = tax_summary(tmp_path, CALL_BOUGHT)
    assert (figures["Disposal proceeds"], figures["Loss"]) == ("£0.00", "£101.00")


def test_written_option_bought_back_gains_premium_less_cost(tmp_path: Path) -> None:
    """Written for 100 (1 commission), bought back for 21: gain 78 (s148)."""
    figures = tax_summary(
        tmp_path,
        f"2025-06-02,Sell,{PUT},-1,2,USD,100,-1,99,0.5",
        f"2025-06-10,Buy,{PUT},1,0.4,USD,-20,-1,-21,0.5",
    )
    assert figures["Disposal proceeds"] == "£100.00"
    assert figures["Allowable costs"] == "£22.00"
    assert figures["Gain"] == "£78.00"


def test_open_written_options_are_listed_with_the_portfolio(tmp_path: Path) -> None:
    """A written option still open is shown as contracts written, not a holding.

    The put bought back and the put that expired are gone; the call expiring
    after the tax year is open both at the end of the history and at the end
    of the tax year.
    """
    late_call = "XYZ   260619C00050000"
    expired_put = "XYZ   250620P00040000"
    path = export(
        tmp_path,
        DEPOSIT,
        f"2025-06-02,Sell,{PUT},-1,2,USD,100,-1,99,0.5",
        f"2025-06-10,Buy,{PUT},1,0.4,USD,-20,-1,-21,0.5",
        f"2025-06-03,Sell,{expired_put},-1,1,USD,50,-1,49,0.5",
        f"2025-06-04,Sell,{late_call},-2,3,USD,300,-2,298,0.5",
        "2025-12-01,Deposit,-,-,-,-,10,-,10,1.0",
    )
    cmd = build_cmd(
        "--year", "2025", "--interactive-brokers-file", str(path), "--no-report"
    )  # fmt: skip
    stdout = run_cli(cmd).stdout
    final, at_year_end = (
        stdout.split(heading)[1].split("\n\n")[0]
        for heading in ("Final portfolio", "Portfolio at the end of")
    )
    written = "XYZ 19JUN26 50 C: -2 written"
    for portfolio in (final, at_year_end):
        assert written in portfolio
        assert " P: " not in portfolio
