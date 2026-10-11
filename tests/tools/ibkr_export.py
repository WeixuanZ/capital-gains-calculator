"""Build synthetic IBKR Transaction History exports for the tool tests."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

HEADER = (
    "Transaction History,Header,Date,Account,Description,Transaction Type,Symbol,"
    "Quantity,Price,Price Currency,Gross Amount ,Commission,Net Amount,Exchange Rate"
)


def ibkr_export(
    path: Path,
    rows: list[str],
    *,
    period: str = "January 1, 2025 - December 31, 2025",
    generated: str = "2026-01-01, 10:00:00 EST",
    starting_cash: Decimal = Decimal(0),
    exchange_rate_column: bool = True,
) -> Path:
    """Write an export whose summary adds up to its rows.

    Each row is Date,Transaction Type,Symbol,Quantity,Price,Price Currency,
    Gross Amount,Commission,Net Amount,Exchange Rate; the Account and a
    Description are added. "FX Translations P&L" rows keep that description.
    """
    header = HEADER if exchange_rate_column else HEADER.rsplit(",", 1)[0]
    lines = []
    total = Decimal(0)
    for row in rows:
        day, rest = row.split(",", 1)
        fields = rest.split(",")
        description = "FX Translations P&L" if fields[0] == "FX" else "X"
        if fields[0] == "FX":
            fields[0] = "Adjustment"
        if not exchange_rate_column:
            fields = fields[:-1]
        total += Decimal(fields[7]) if fields[7] != "-" else Decimal(0)
        lines.append(
            f"Transaction History,Data,{day},U***00000,{description},"
            + ",".join(fields)
        )
    path.write_text(
        "Statement,Header,Field Name,Field Value\n"
        "Statement,Data,Title,Transaction History\n"
        f'Statement,Data,Period,"{period}"\n'
        f'Statement,Data,WhenGenerated,"{generated}"\n'
        "Summary,Header,Field Name,Field Value\n"
        "Summary,Data,Base Currency,GBP\n"
        f"Summary,Data,Starting Cash,{starting_cash}\n"
        f"Summary,Data,Ending Cash,{starting_cash + total}\n"
        + header
        + "\n"
        + "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )
    return path
