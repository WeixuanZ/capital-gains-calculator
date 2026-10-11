"""Merge Interactive Brokers Transaction History exports into one file.

cgt-calc reads one ``--interactive-brokers-file``, and the Transaction History
page exports a limited period at once. This joins exports of one account
without changing any row it keeps:

- For each date, rows come from the most recently generated export whose
  period covers it, so overlapping ranges are not doubled. Where exports
  overlap, every row must agree, or the merge stops.
- Every export must add up on its own to the change in cash its summary
  states, so a filtered or truncated one is refused.
- IBKR ends each export with an "FX Translations P&L" Adjustment revaluing
  foreign cash over that export's period alone, which does not add up across
  stitched periods. Those rows are replaced by one balancing Adjustment, so the
  merged rows add up to the first starting and the last ending cash. It moves
  cash only.
- A missing Exchange Rate on a foreign-currency Buy or Sell (older exports) is
  filled from the row: |Gross Amount| / (|Quantity| x Price x multiplier).
- A row can be dropped, for a trade moved into a RAW file because a corporate
  action the export lacks happened the same day: cgt-calc needs that day's rows
  for the security in one input. Its cash leaves the summary with it.
"""

from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

COLUMNS = (
    "Date",
    "Account",
    "Description",
    "Transaction Type",
    "Symbol",
    "Quantity",
    "Price",
    "Price Currency",
    "Gross Amount ",
    "Commission",
    "Net Amount",
    "Exchange Rate",
)
FX_TRANSLATION = "FX Translations P&L"
# IBKR prints amounts to many places, so a penny is a generous margin.
TOLERANCE = Decimal("0.01")
_OPTION = re.compile(r"[A-Z.\-]+\s+\d{6}[CP]\d{8}")
_PERIOD_FORMAT = "%B %d, %Y"

type Row = dict[str, str]


class MergeError(Exception):
    """The exports cannot be merged safely."""


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def _net(row: Row) -> Decimal:
    value = row["Net Amount"]
    return Decimal(0) if value in {"", "-"} else _decimal(value)


@dataclass(frozen=True)
class Export:
    """One Transaction History export."""

    name: str
    start: dt.date
    end: dt.date
    generated: str
    starting_cash: Decimal
    ending_cash: Decimal
    rows: tuple[Row, ...]

    def covers(self, row: Row) -> bool:
        """Whether the row's date falls in this export's period."""
        return self.start <= dt.date.fromisoformat(row["Date"]) <= self.end


def read_export(path: Path) -> Export:
    """Read an export, refusing one that is not a complete GBP account history."""
    fields: dict[str, str] = {}
    header: list[str] = []
    rows: list[Row] = []
    with path.open(encoding="utf-8-sig", newline="") as file:
        for record in csv.reader(file):
            section, kind = record[:2]
            if kind == "Data" and section in {"Statement", "Summary"}:
                fields[record[2]] = record[3]
            elif section == "Transaction History" and kind == "Header":
                header = record[2:]
            elif section == "Transaction History" and kind == "Data":
                rows.append(
                    {"Exchange Rate": "", **dict(zip(header, record[2:], strict=True))}
                )
    if fields.get("Base Currency") != "GBP":
        raise MergeError(f"{path.name}: base currency is not GBP")
    if unknown := set(header) - set(COLUMNS):
        raise MergeError(f"{path.name}: unexpected columns {sorted(unknown)}")
    try:
        start, end = (
            dt.datetime.strptime(part.strip(), _PERIOD_FORMAT).date()
            for part in fields["Period"].split(" - ")
        )
        starting, ending = (
            _decimal(fields["Starting Cash"]),
            _decimal(fields["Ending Cash"]),
        )
    except (KeyError, ValueError) as err:
        raise MergeError(f"{path.name}: no readable period or cash summary") from err
    if abs(sum(map(_net, rows), Decimal(0)) - (ending - starting)) > TOLERANCE:
        raise MergeError(
            f"{path.name}: its rows do not add up to the change in cash its summary "
            "states, so it is filtered or incomplete"
        )
    return Export(
        path.name,
        start,
        end,
        fields.get("WhenGenerated", ""),
        starting,
        ending,
        tuple(rows),
    )


def _key(row: Row) -> tuple[str, ...]:
    """Identify a row for comparing exports, ignoring how numbers are printed."""

    def number(value: str) -> str:
        return value if value in {"", "-"} else f"{_decimal(value):.6f}"

    return (
        row["Date"],
        row["Transaction Type"],
        row["Symbol"],
        row["Description"],
        number(row["Quantity"]),
        number(row["Price"]),
        number(row["Net Amount"]),
    )


def _fill_exchange_rate(row: Row) -> Row:
    if row["Exchange Rate"] not in {"", "-"}:
        return row
    rate = "1.0"
    if row["Transaction Type"] in {"Buy", "Sell"} and row["Price Currency"] not in {
        "GBP",
        "-",
        "",
    }:
        multiplier = 100 if _OPTION.fullmatch(row["Symbol"]) else 1
        computed = abs(_decimal(row["Gross Amount "])) / (
            abs(_decimal(row["Quantity"])) * _decimal(row["Price"]) * multiplier
        )
        rate = f"{computed:.7f}"
    return {**row, "Exchange Rate": rate}


@dataclass
class Merged:
    """Merged rows, newest first, with the summary that matches them."""

    start: dt.date
    end: dt.date
    generated: str
    starting_cash: Decimal
    ending_cash: Decimal
    rows: list[Row]
    dropped: list[Row] = field(default_factory=list)
    used: dict[str, int] = field(default_factory=dict)


def merge(
    exports: Iterable[Export], drops: Iterable[tuple[str, str, str]] = ()
) -> Merged:
    """Merge exports of one account; `drops` names rows by (date, type, symbol)."""
    newest_first = sorted(exports, key=lambda e: e.generated, reverse=True)
    if not newest_first:
        raise MergeError("no exports to merge")
    rows: list[Row] = []
    used: dict[str, int] = {}
    for index, export in enumerate(newest_first):
        newer = newest_first[:index]
        taken = [r for r in export.rows if not any(n.covers(r) for n in newer)]
        used[export.name] = len(taken)
        rows += taken
        for other in newer:
            mine = Counter(
                _key(r)
                for r in export.rows
                if other.covers(r) and r["Description"] != FX_TRANSLATION
            )
            theirs = Counter(
                _key(r)
                for r in other.rows
                if export.covers(r) and r["Description"] != FX_TRANSLATION
            )
            if mine != theirs:
                raise MergeError(
                    f"{export.name} and {other.name} disagree where they overlap: "
                    f"only in the first {sorted(mine - theirs)}, "
                    f"only in the second {sorted(theirs - mine)}"
                )
    rows = [_fill_exchange_rate(r) for r in rows if r["Description"] != FX_TRANSLATION]
    first = min(newest_first, key=lambda e: e.start)
    last = max(newest_first, key=lambda e: e.end)
    balance = last.ending_cash - first.starting_cash - sum(map(_net, rows), Decimal(0))
    rows.append(
        {
            **dict.fromkeys(COLUMNS, "-"),
            "Date": last.end.isoformat(),
            "Account": rows[0]["Account"] if rows else "-",
            "Description": FX_TRANSLATION,
            "Transaction Type": "Adjustment",
            "Gross Amount ": str(balance),
            "Net Amount": str(balance),
            "Exchange Rate": "1.0",
        }
    )
    ending = last.ending_cash
    dropped = []
    for drop in drops:
        hits = [
            r for r in rows if (r["Date"], r["Transaction Type"], r["Symbol"]) == drop
        ]
        if len(hits) != 1:
            raise MergeError(f"drop {','.join(drop)} matches {len(hits)} rows, not 1")
        rows.remove(hits[0])
        dropped.append(hits[0])
        ending -= _net(hits[0])
    rows.sort(key=lambda r: r["Date"], reverse=True)
    return Merged(
        first.start,
        last.end,
        last.generated,
        first.starting_cash,
        ending,
        rows,
        dropped,
        used,
    )


def write(merged: Merged, path: Path) -> None:
    """Write merged rows as one Transaction History export."""

    def period(day: dt.date) -> str:
        return f"{day:%B} {day.day}, {day.year}"

    with path.open("w", newline="", encoding="utf-8") as file:
        out = csv.writer(file)
        out.writerow(["Statement", "Header", "Field Name", "Field Value"])
        out.writerow(["Statement", "Data", "Title", "Transaction History"])
        out.writerow(
            [
                "Statement",
                "Data",
                "Period",
                f"{period(merged.start)} - {period(merged.end)}",
            ]
        )
        out.writerow(["Statement", "Data", "WhenGenerated", merged.generated])
        out.writerow(["Summary", "Header", "Field Name", "Field Value"])
        out.writerow(["Summary", "Data", "Base Currency", "GBP"])
        out.writerow(["Summary", "Data", "Starting Cash", str(merged.starting_cash)])
        out.writerow(
            [
                "Summary",
                "Data",
                "Change",
                str(merged.ending_cash - merged.starting_cash),
            ]
        )
        out.writerow(["Summary", "Data", "Ending Cash", str(merged.ending_cash)])
        out.writerow(["Transaction History", "Header", *COLUMNS])
        for row in merged.rows:
            out.writerow(["Transaction History", "Data", *(row[c] for c in COLUMNS)])
