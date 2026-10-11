"""Interactive brokers transaction parser."""

from __future__ import annotations

import csv
import datetime
from decimal import Decimal
from enum import StrEnum
import io
from itertools import chain
import re
from typing import TYPE_CHECKING, ClassVar, Final, TextIO, override

from cgt_calc.exceptions import ParsingError
from cgt_calc.model import ActionType, BrokerTransaction, CurrencyCode, Isin
from cgt_calc.util import parse_decimal

from .base_parsers import StandardCSVParser
from .ibkr_options import StatementSummary, parse_ibkr_option, reconcile_ibkr_options

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


EXPECTED_COLS_IN_SUMMARY_SECTION: Final[int] = 4
# IBKR rounds Gross Amount to the penny and prints Net Amount in full.
NET_AMOUNT_ROUNDING: Final = Decimal("0.01")

# Dividend and withholding rows name the security as "VT(US9220427424) Cash
# Dividend ...". Trade rows carry a plain description, so this is best effort.
_ISIN_IN_DESCRIPTION_RE: Final = re.compile(r"^[^\s(]+\((?P<isin>[A-Z0-9]{12})\)")


# IBKR names a bond by issuer, coupon and maturity: "UKT 4 1/4 12/07/27" is the
# 4.25% Treasury Gilt maturing on 7 December 2027.
_BOND_SYMBOL_RE: Final = re.compile(
    r"^(?P<issuer>\S+) \d+(?:\.\d+)?(?: \d+/\d+)? \d{2}/\d{2}/\d{2}$"
)
# UK government gilts are gilt-edged securities, exempt from capital gains tax
# (TCGA 1992 s115). IBKR's ticker for them is UKT.
_GILT_ISSUERS: Final = frozenset({"UKT"})


def _isin_from_description(description: str) -> Isin | None:
    """Return the ISIN the description is prefixed with, if it has one."""
    match = _ISIN_IN_DESCRIPTION_RE.match(description)
    if match is None:
        return None
    return Isin.parse(match.group("isin"))


class InteractiveBrokersColumn(StrEnum):
    """Column names for the interactive brokers format."""

    TRANSACTION_HISTORY = "Transaction History"
    HEADER = "Header"
    DATE = "Date"
    ACCOUNT = "Account"
    DESCRIPTION = "Description"
    TRANSACTION_TYPE = "Transaction Type"
    SYMBOL = "Symbol"
    QUANTITY = "Quantity"
    PRICE = "Price"
    PRICE_CURRENCY = "Price Currency"
    GROSS_AMOUNT = "Gross Amount "
    COMMISSION = "Commission"
    NET_AMOUNT = "Net Amount"
    EXCHANGE_RATE = "Exchange Rate"


_IBKR_OPTIONAL_COLUMNS: Final[set[str]] = {
    InteractiveBrokersColumn.PRICE_CURRENCY,
    InteractiveBrokersColumn.EXCHANGE_RATE,
}


def _action_from_str(action_type: str, file_path: Path) -> ActionType:
    """Infer action type."""
    if action_type == "Credit Interest":
        return ActionType.INTEREST
    # A payment in lieu arrives instead of a dividend when the holding is out on
    # loan. It is the rarer of the two, so a parser that knows it and not the
    # ordinary dividend fails for most holders on their first real statement.
    if action_type in {"Dividend", "Payment in Lieu"}:
        return ActionType.DIVIDEND
    if action_type in {"Deposit", "Withdrawal"}:
        return ActionType.TRANSFER
    if action_type == "Buy":
        return ActionType.BUY
    if action_type == "Sell":
        return ActionType.SELL
    if action_type == "Foreign Tax Withholding":
        return ActionType.DIVIDEND_TAX
    # A "Transaction Fee" naming a security is a charge on that day's trades
    # in it, such as Spain's financial transaction tax. It is an incidental
    # cost of those trades (TCGA 1992 s38(2)), so the parser folds it into
    # them once the whole file is read; FEE marks it until then.
    if action_type == "Transaction Fee":
        return ActionType.FEE
    # "FX Translations P&L" arrives as an Adjustment: the revaluation of a
    # foreign currency balance. A "Forex Trade Component" is the base-currency
    # net of a conversion, filed under the currency pair. Neither is a trade in
    # a security: both move the cash balance and create no taxable event, which
    # is what ADJUSTMENT means here and how the Schwab parser treats its own.
    # Read as a fee instead, a component that brings base currency in is
    # refused outright, and one that takes some out opens a pooled cost under
    # the currency pair.
    # "Debit Interest" is interest IBKR charges on a borrowed balance, and
    # "Sales Tax" the VAT on a service such as a market-data subscription.
    # Both are costs of the account rather than of any one holding. Filing
    # them as an ADJUSTMENT moves the cash balance and leaves the interest
    # report to the interest actually received: interest paid on margin is
    # not deductible against it.
    # "Other Fee" covers an account charge such as that subscription and a
    # charge billed against a holding, such as a depositary's ADR fee. Neither
    # is a cost of acquiring or disposing of shares (TCGA 1992 s38), so
    # neither adds to a holding's cost, whichever symbol the row names.
    # Revolut's custody fee is read the same way. IBKR's reporting reference
    # lists a corporate action election fee under this type as well, which
    # may be a cost of a trade. Nothing on the row says which trade, so what
    # an Other Fee was paid for is not assessed and that one moves the cash
    # alone too; the broker page says how to have it deducted.
    if action_type in {
        "Adjustment",
        "Forex Trade Component",
        "Debit Interest",
        "Sales Tax",
        "Other Fee",
        # Accrued interest paid with a bond purchase. It belongs to income tax
        # (the accrued income scheme), not to the bond's capital cost.
        "Investment Interest Paid",
    }:
        return ActionType.ADJUSTMENT

    raise ParsingError(file_path, f"Unknown type: '{action_type}'")


def _parse_str(row: dict[str, str], column: str) -> str | None:
    """Read a text column, treating IBKR's "-" placeholder as absent.

    IBKR writes "-" wherever a column does not apply to a row, in text columns
    as well as numeric ones. Taking it at face value puts a currency called "-"
    on the transaction.
    """
    value = row.get(column)
    if not value or value == "-":
        return None
    return value


def _parse_decimal(row: dict[str, str], column: str) -> Decimal | None:
    """Parse decimal value from the row, raising ValueError with context on failure."""

    value = row[column]
    if value == "-":
        return None

    return parse_decimal(value, f"column '{column}'", strip=",")


class InteractiveBrokersTransaction(BrokerTransaction):
    """Represents a single Interactive Brokers transaction."""

    def __init__(
        self,
        row: dict[str, str],
        file_path: Path,
    ):
        """Create transaction from CSV row."""
        date_str = row[InteractiveBrokersColumn.DATE]
        date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()

        action = _action_from_str(
            row[InteractiveBrokersColumn.TRANSACTION_TYPE], file_path
        )
        symbol = _parse_str(row, InteractiveBrokersColumn.SYMBOL)
        quantity = _parse_decimal(row, InteractiveBrokersColumn.QUANTITY)
        price = _parse_decimal(row, InteractiveBrokersColumn.PRICE)
        amount = _parse_decimal(row, InteractiveBrokersColumn.NET_AMOUNT)
        fees = _parse_decimal(row, InteractiveBrokersColumn.COMMISSION) or Decimal(0)
        price_currency = CurrencyCode(
            _parse_str(row, InteractiveBrokersColumn.PRICE_CURRENCY) or "GBP"
        )
        exchange_rate = (
            _parse_decimal(row, InteractiveBrokersColumn.EXCHANGE_RATE)
            if InteractiveBrokersColumn.EXCHANGE_RATE in row
            else None
        )

        option_contract = (
            parse_ibkr_option(symbol, date)
            if symbol is not None and action in {ActionType.BUY, ActionType.SELL}
            else None
        )
        # The price as exported, in the price currency, before it is converted
        # below. An option assignment would deliver shares at the strike, and
        # the strike is in that currency.
        price_as_exported = price

        bond = _BOND_SYMBOL_RE.fullmatch(symbol) if symbol is not None else None
        if bond is not None and action in {ActionType.BUY, ActionType.SELL}:
            if bond.group("issuer") not in _GILT_ISSUERS:
                raise ParsingError(
                    file_path,
                    f"'{symbol}' looks like a bond, and only UK gilts are "
                    "supported for Interactive Brokers. A corporate bond may "
                    "be a qualifying corporate bond, exempt like a gilt, or "
                    "not: work out its gains by hand.",
                )
            # Exempt, so only the cash moves (TCGA 1992 s115).
            action = ActionType.ADJUSTMENT

        if action is ActionType.FEE and symbol is None:
            # A transaction fee naming no security is a charge on the account.
            action = ActionType.ADJUSTMENT

        # Withholding with no security behind it is tax on the account's cash
        # interest, not on a dividend, so INTEREST_TAX reports it against the
        # interest it was taken from. The Schwab parser makes the same switch.
        if action is ActionType.DIVIDEND_TAX and symbol is None:
            action = ActionType.INTEREST_TAX

        # An adjustment moves the cash balance and nothing else: the calculator
        # reads its amount and never its symbol, quantity or price. IBKR files a
        # Forex Trade Component under the currency pair and gives it both a
        # quantity and a price, so clear the columns that describe a security
        # rather than leave a currency pair looking like a holding. An Other
        # Fee that names a holding loses its symbol the same way: the charge is
        # no part of what that holding cost.
        if action is ActionType.ADJUSTMENT:
            symbol = None
            quantity = None
            price = None
            fees = Decimal(0)

        # The Gross/Net Amount and Commission columns are always in the account's
        # base currency (GBP), so that is the transaction's currency whatever
        # priced the leg. Price Currency describes the price alone. Only a Buy
        # or Sell reads the price back, to check it against the amount
        # (quantity x price + fees ≈ |amount|), so only those two have a foreign
        # price converted, and are refused where no rate is given to convert it
        # with; every other row's price is left as the row states it rather
        # than blocking rows the calculator never checks it on.
        if (
            action in {ActionType.BUY, ActionType.SELL}
            and price is not None
            and price_currency != "GBP"
        ):
            if exchange_rate is None:
                raise ParsingError(
                    file_path,
                    f"Price is in {price_currency} but the Exchange Rate column is "
                    "missing or empty, so the price cannot be converted to the "
                    "account's base currency (GBP), which Gross Amount, Commission "
                    "and Net Amount are in. Re-export the statement with the "
                    "Exchange Rate filled in for this row: a Buy or Sell always "
                    "needs a price, so clearing it only trades this error for a "
                    "missing-price one.",
                )
            price = price * exchange_rate
        if option_contract is not None and price is not None:
            # One contract is a right over `multiplier` shares, and the
            # calculator counts contracts, so it is priced per contract.
            price = price * option_contract.multiplier

        # Net Amount is everything the trade cost or raised, so its gap to
        # Gross Amount is every incidental cost of it: the commission and, for
        # a UK share, the stamp duty and PTM levy IBKR adds to Net Amount
        # without showing them in Commission. All are allowable costs (TCGA
        # 1992 s38(2)). The gap can only add to the commission: one that takes
        # from it is not a cost, and read as one would understate the gain.
        gross = _parse_decimal(row, InteractiveBrokersColumn.GROSS_AMOUNT)
        if (
            action in {ActionType.BUY, ActionType.SELL}
            and gross is not None
            and amount is not None
        ):
            if amount - gross - fees > NET_AMOUNT_ROUNDING:
                raise ParsingError(
                    file_path,
                    f"Net Amount {amount} is more than Gross Amount {gross} plus "
                    f"Commission {fees}, so it holds something that is not a "
                    "cost of the trade. Check the row against IBKR.",
                )
            fees = amount - gross

        super().__init__(
            date=date,
            action=action,
            symbol=symbol,
            description=row[InteractiveBrokersColumn.DESCRIPTION],
            quantity=abs(quantity) if quantity is not None else None,
            price=price,
            fees=-fees,
            amount=amount,
            currency=CurrencyCode("GBP"),
            broker="Interactive Brokers",
            isin=_isin_from_description(row[InteractiveBrokersColumn.DESCRIPTION]),
            option_contract=option_contract,
        )
        self.price_as_exported = price_as_exported


class InteractiveBrokersParser(StandardCSVParser[BrokerTransaction]):
    """Parser for Interactive Brokers format transaction files."""

    arg_name = "interactive-brokers"
    pretty_name = "Interactive Brokers"
    format_name = "CSV"
    optional_columns: ClassVar[set[str]] = _IBKR_OPTIONAL_COLUMNS
    columns: ClassVar[set[str]] = {
        column.value
        for column in InteractiveBrokersColumn
        if column.value not in _IBKR_OPTIONAL_COLUMNS
    }

    @classmethod
    @override
    def read_row(
        cls, row: dict[str, str], file_path: Path
    ) -> InteractiveBrokersTransaction | None:
        """Read a single transaction from a row in the CSV."""
        return InteractiveBrokersTransaction(row, file_path)

    @classmethod
    @override
    def read_transactions(
        cls, file: TextIO, file_path: Path
    ) -> list[BrokerTransaction]:
        """Read the rows, then settle what a row cannot say on its own.

        A Transaction Fee is a cost of other rows, and an option row says
        neither whether it opened or closed a position nor when one expired,
        which takes the whole file and its header to work out.
        """
        text = file.read()
        transactions = super().read_transactions(io.StringIO(text), file_path)
        transactions = _fold_transaction_fees(transactions, file_path)
        return reconcile_ibkr_options(
            transactions, _statement_summary(text, file_path), file_path
        )

    @classmethod
    @override
    def pre_reading(
        cls, file: Iterable[str], file_path: Path
    ) -> tuple[Iterable[str], int]:
        """Skip Statement and Summary sections. Transaction History is the important one."""
        for row, line in enumerate(file, start=1):
            # Validate we're dealing with a GBP account
            if line.startswith("Summary,Data,Base Currency,"):
                rows = line.split(",")
                if (
                    len(rows) < EXPECTED_COLS_IN_SUMMARY_SECTION
                    or rows[3].strip() != "GBP"
                ):
                    raise ParsingError(
                        file_path,
                        f"Unexpected base currency: '{rows[3]}', only GBP is supported",
                    )
            if line.startswith("Transaction History"):
                return chain([line], file), row
        raise ParsingError(
            file_path,
            "Couldn't find Transaction History header, is this the right file?",
        )

    @classmethod
    @override
    def post_process_transactions(
        cls, transactions: list[BrokerTransaction]
    ) -> list[BrokerTransaction]:
        """Sort transactions by date, reading each day's sales last.

        IBKR can list a day's rows newest first, so a security bought and then
        sold that day may arrive sale first, and the sale would be refused as
        not owned. With the sales last, a holding never runs short partway
        through a day that it ends at zero or above. Neither the cash balance,
        which is checked at the end of each day, nor the gain, which TCGA 1992
        s105 works out from the day's totals, depends on the order within a day.
        """
        transactions.sort(key=lambda t: (t.date, t.action is ActionType.SELL))
        return transactions


def _statement_summary(text: str, file_path: Path) -> StatementSummary:
    """Read the period end and cash balances an export's header states."""
    fields = {
        record[2]: record[3]
        for record in csv.reader(text.splitlines())
        if record[:2] in (["Statement", "Data"], ["Summary", "Data"])
        and len(record) >= EXPECTED_COLS_IN_SUMMARY_SECTION
    }
    try:
        period_end = datetime.datetime.strptime(
            fields["Period"].split(" - ")[-1].strip(), "%B %d, %Y"
        ).date()
    except (KeyError, ValueError):
        period_end = None

    def cash(name: str) -> Decimal | None:
        value = fields.get(name)
        return parse_decimal(value, f"{name} in {file_path}") if value else None

    return StatementSummary(period_end, cash("Starting Cash"), cash("Ending Cash"))


def _fold_transaction_fees(
    transactions: list[BrokerTransaction], file_path: Path
) -> list[BrokerTransaction]:
    """Add each security's Transaction Fees to that day's trades in it.

    IBKR posts a charge such as Spain's financial transaction tax, and any
    correction of it, as rows of their own. The day's net is shared between
    that day's trades in the security in proportion to their quantity.
    """
    totals: dict[tuple[datetime.date, str | None], Decimal] = {}
    for fee in (t for t in transactions if t.action is ActionType.FEE):
        key = (fee.date, fee.symbol)
        totals[key] = totals.get(key, Decimal(0)) + (fee.amount or Decimal(0))
    rest = [t for t in transactions if t.action is not ActionType.FEE]
    for (date, symbol), total in totals.items():
        trades = [
            t
            for t in rest
            if (t.date, t.symbol) == (date, symbol)
            and t.action in {ActionType.BUY, ActionType.SELL}
        ]
        quantity = sum((t.quantity or Decimal(0) for t in trades), Decimal(0))
        if not quantity:
            raise ParsingError(
                file_path,
                f"A Transaction Fee of {total} for {symbol} on {date} has no "
                f"trade in {symbol} that day to be a cost of.",
            )
        for t in trades:
            share = total * (t.quantity or Decimal(0)) / quantity
            # The fee row is signed as it moves cash, so a charge lowers the
            # trade's amount and raises its fees.
            t.amount = (t.amount or Decimal(0)) + share
            t.fees -= share
    return rest
