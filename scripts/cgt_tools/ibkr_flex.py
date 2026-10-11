"""Fetch an IBKR Flex Query and turn its corporate actions into cgt-calc input.

The Transaction History export has no row for a split or spin-off, so cgt-calc
needs them as RAW rows. A Flex Query with the Corporate Actions section has
them, and the Flex Web Service downloads one without opening the portal:

1. In the Client Portal, create an Activity Flex Query (Performance & Reports,
   Flex Queries) with the Corporate Actions section and all its fields, CSV
   format, covering the whole account history.
2. Enable the Flex Web Service there to get a token.

Only splits (FS, RS) and spin-offs (SO) of shares are converted. Anything else,
such as a merger or a rights issue, is refused: each needs its own treatment.
"""

from __future__ import annotations

from collections import defaultdict
import csv
from dataclasses import dataclass
import datetime as dt
from decimal import Decimal
import io
import re
import time
from typing import TYPE_CHECKING, Final, Protocol

from defusedxml import ElementTree
import requests

if TYPE_CHECKING:
    from collections.abc import Callable

SEND_URL: Final = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
)
API_VERSION: Final = "3"
# IBKR answers this while the statement is still being generated.
IN_PROGRESS: Final = "1019"
HEADERS: Final = {"User-Agent": "cgt-calc-tools"}
TIMEOUT_SECONDS: Final = 60

SPLIT_TYPES: Final = {"FS": "forward split", "RS": "reverse split"}
SPIN_OFF: Final = "SO"
# "ABC(US0000000001) SPINOFF  1 FOR 4 (XYZ, ...)" names the parent first.
_SPIN_OFF_PARENT = re.compile(
    r"^(?P<parent>[A-Za-z0-9.]+)\([A-Z0-9]{12}\)\s+SPIN-?OFF", re.IGNORECASE
)


class FlexError(Exception):
    """The Flex Web Service or a Flex Query could not be used."""


class Get(Protocol):
    """The part of ``requests.get`` the fetch uses, so a test can stand in for it."""

    def __call__(
        self, url: str, *, params: dict[str, str], headers: dict[str, str], timeout: int
    ) -> requests.Response:
        """Fetch a URL."""
        ...


def _response(text: str) -> dict[str, str]:
    root = ElementTree.fromstring(text)
    return {child.tag: (child.text or "") for child in root}


def fetch(
    token: str,
    query_id: str,
    *,
    get: Get = requests.get,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 10,
    wait_seconds: float = 5,
) -> str:
    """Return a Flex Query's statement, waiting while IBKR generates it."""
    sent = _response(
        get(
            SEND_URL,
            params={"t": token, "q": query_id, "v": API_VERSION},
            headers=HEADERS,
            timeout=TIMEOUT_SECONDS,
        ).text
    )
    if sent.get("Status") != "Success":
        raise FlexError(
            f"Flex request refused: {sent.get('ErrorCode')} {sent.get('ErrorMessage')}"
        )
    for _ in range(attempts):
        text = get(
            sent["Url"],
            params={"t": token, "q": sent["ReferenceCode"], "v": API_VERSION},
            headers=HEADERS,
            timeout=TIMEOUT_SECONDS,
        ).text
        if not text.lstrip().startswith("<FlexStatementResponse"):
            return text
        status = _response(text)
        if status.get("ErrorCode") != IN_PROGRESS:
            raise FlexError(
                f"Flex statement refused: {status.get('ErrorCode')} {status.get('ErrorMessage')}"
            )
        sleep(wait_seconds)
    raise FlexError(f"Flex statement not ready after {attempts} attempts")


@dataclass(frozen=True)
class RawRow:
    """One row of cgt-calc's RAW format."""

    date: dt.date
    action: str
    symbol: str
    quantity: Decimal
    currency: str


@dataclass(frozen=True)
class CorporateActions:
    """RAW rows and the spin-off sources cgt-calc's --spin-offs-file takes."""

    rows: list[RawRow]
    spin_offs: dict[str, str]

    def raw_csv(self) -> str:
        """Return the rows in cgt-calc's RAW format."""
        lines = ["date,action,symbol,quantity,price,fees,currency"]
        lines += [
            f"{r.date},{r.action},{r.symbol},{r.quantity},0,0,{r.currency}"
            for r in self.rows
        ]
        return "\n".join(lines) + "\n"

    def spin_offs_csv(self) -> str:
        """Return the spin-off sources in cgt-calc's --spin-offs-file format."""
        return "dst,src\n" + "".join(
            f"{child},{parent}\n" for child, parent in self.spin_offs.items()
        )


def _normalised(row: dict[str, str]) -> dict[str, str]:
    """Key a row by field name alone, so "Date/Time" and "DateTime" agree."""
    return {
        re.sub(r"[^a-z]", "", key.lower()): (value or "").strip()
        for key, value in row.items()
    }


def _date(value: str) -> dt.date:
    """Read the date of "20251117;202500", "2025-11-17, 20:25:00" and the like."""
    digits = re.sub(r"\D", "", value)[:8]
    try:
        return dt.datetime.strptime(digits, "%Y%m%d").date()
    except ValueError as err:
        raise FlexError(f"unreadable date {value!r}") from err


def corporate_actions(flex_csv: str) -> CorporateActions:
    """Convert a Flex Query's Corporate Actions section into RAW rows."""
    splits: dict[tuple[dt.date, str, str, str], Decimal] = defaultdict(Decimal)
    rows: list[RawRow] = []
    spin_offs: dict[str, str] = {}
    for raw in csv.DictReader(io.StringIO(flex_csv)):
        row = _normalised(raw)
        kind, description = row.get("type", ""), row.get("description", "")
        if not kind:
            raise FlexError(
                "the file has no Type column: include every Corporate Actions field"
            )
        asset_class = row.get("assetclass", "STK")
        if asset_class != "STK":
            raise FlexError(
                f"{asset_class} corporate action not supported: {description}"
            )
        when = _date(row.get("datetime") or row.get("reportdate", ""))
        symbol, quantity = row["symbol"], Decimal(row["quantity"].replace(",", ""))
        currency = row.get("currency") or row.get("currencyprimary") or "USD"
        if kind in SPLIT_TYPES:
            splits[when, symbol, currency, kind] += quantity
        elif kind == SPIN_OFF:
            match = _SPIN_OFF_PARENT.match(description)
            if match is None or quantity <= 0:
                raise FlexError(f"cannot read the spin-off: {description}")
            spin_offs[symbol] = match["parent"]
            rows.append(RawRow(when, "SPIN_OFF", symbol, quantity, currency))
        else:
            raise FlexError(
                f"corporate action type {kind} is not supported, work it out by hand: {description}"
            )
    for (when, symbol, currency, kind), quantity in splits.items():
        if (quantity > 0) != (kind == "FS") or quantity == 0:
            raise FlexError(
                f"the {SPLIT_TYPES[kind]} of {symbol} on {when} changes the holding by {quantity}"
            )
        rows.append(RawRow(when, "STOCK_SPLIT", symbol, quantity, currency))
    return CorporateActions(sorted(rows, key=lambda r: (r.date, r.symbol)), spin_offs)
