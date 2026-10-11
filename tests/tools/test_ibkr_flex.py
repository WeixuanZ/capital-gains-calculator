"""Fetching IBKR Flex Queries and reading their corporate actions."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import requests

from scripts.cgt_tools.ibkr_flex import (
    SEND_URL,
    CorporateActions,
    FlexError,
    RawRow,
    corporate_actions,
    fetch,
)

COLUMNS = (
    "Asset Class,Currency,Symbol,ISIN,Description,Report Date,Date/Time,Quantity,Type"
)


def flex(*rows: str) -> str:
    """Return a Flex corporate actions CSV with these rows."""
    return "\n".join([COLUMNS, *rows]) + "\n"


def test_forward_split_adds_the_new_units() -> None:
    """A 10-for-1 split of 1 share adds 9."""
    actions = corporate_actions(
        flex(
            "STK,USD,ABC,US0000000001,ABC(US0000000001) SPLIT 10 FOR 1,"
            "20250617,20250616;202500,9,FS"
        )
    )
    assert actions.rows == [
        RawRow(dt.date(2025, 6, 16), "STOCK_SPLIT", "ABC", Decimal(9), "USD")
    ]
    assert actions.raw_csv().splitlines()[1] == "2025-06-16,STOCK_SPLIT,ABC,9,0,0,USD"


def test_split_written_as_removal_and_issue_is_netted() -> None:
    """IBKR may take the old shares away and issue the new: the net is the change."""
    actions = corporate_actions(
        flex(
            "STK,USD,ABC,US0000000001,ABC SPLIT,20250617,2025-06-16 20:25:00,-1,FS",
            "STK,USD,ABC,US0000000001,ABC SPLIT,20250617,2025-06-16 20:25:00,10,FS",
        )
    )
    assert [(r.symbol, r.quantity) for r in actions.rows] == [("ABC", Decimal(9))]


def test_reverse_split_removes_units() -> None:
    """A 1-for-10 consolidation of 20 shares removes 18."""
    actions = corporate_actions(
        flex("STK,USD,ABC,US0000000001,ABC SPLIT 1 FOR 10,20250617,20250616,-18,RS")
    )
    assert actions.rows[0].quantity == Decimal(-18)


@pytest.mark.parametrize(
    "row",
    [
        "STK,USD,ABC,US0000000001,ABC SPLIT,20250617,20250616,-1,FS",
        "STK,USD,ABC,US0000000001,ABC SPLIT,20250617,20250616,5,RS",
    ],
    ids=["forward-split-shrinks", "reverse-split-grows"],
)
def test_split_changing_the_wrong_way_is_refused(row: str) -> None:
    """A split whose net goes the wrong way is probably a ticker change too."""
    with pytest.raises(FlexError, match="changes the holding by"):
        corporate_actions(flex(row))


def test_spin_off_names_its_parent() -> None:
    """The new holding's row names the company it was spun off from."""
    actions = corporate_actions(
        flex(
            "STK,USD,NEWCO,US0000000002,ABC(US0000000001) SPINOFF  1 FOR 2 "
            "(NEWCO; NEW COMPANY; US0000000002),20250315,20250314;202000,3,SO"
        )
    )
    assert actions == CorporateActions(
        [RawRow(dt.date(2025, 3, 14), "SPIN_OFF", "NEWCO", Decimal(3), "USD")],
        {"NEWCO": "ABC"},
    )
    assert actions.spin_offs_csv() == "dst,src\nNEWCO,ABC\n"


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            "STK,USD,ABC,US0000000001,ABC MERGED WITH XYZ,20250617,20250616,-10,TC",
            "type TC",
        ),
        (
            "OPT,USD,ABC 250620C10,-,CONTRACT SPLIT,20250617,20250616,1,CS",
            "OPT corporate",
        ),
        (
            "STK,USD,NEWCO,US0000000002,UNREADABLE,20250617,20250616,1,SO",
            "cannot read the spin-off",
        ),
        ("STK,USD,ABC,US0000000001,ABC SPLIT,20250617,someday,1,FS", "unreadable date"),
    ],
    ids=["merger", "option", "spin-off-without-parent", "bad-date"],
)
def test_what_cannot_be_converted_is_refused(row: str, message: str) -> None:
    """Every event the tool does not model stops it, with what it was."""
    with pytest.raises(FlexError, match=message):
        corporate_actions(flex(row))


def test_export_without_type_column_is_refused() -> None:
    """Without Type a split cannot be told from a merger."""
    with pytest.raises(FlexError, match="no Type column"):
        corporate_actions("Symbol,Quantity\nABC,1\n")


class FakeIbkr:
    """Answers the two Flex Web Service calls from a script of responses."""

    def __init__(self, *responses: str) -> None:
        """Answer with `responses`, in order."""
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(
        self, url: str, *, params: dict[str, str], headers: dict[str, str], timeout: int
    ) -> requests.Response:
        """Record the call and give the next response."""
        assert headers["User-Agent"]
        assert timeout > 0
        self.calls.append((url, params))
        response = requests.Response()
        response._content = self.responses.pop(0).encode()  # noqa: SLF001
        return response


SENT = (
    "<FlexStatementResponse><Status>Success</Status><ReferenceCode>42</ReferenceCode>"
    "<Url>https://example.test/GetStatement</Url></FlexStatementResponse>"
)
NOT_READY = (
    "<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>"
    "<ErrorMessage>Statement generation in progress.</ErrorMessage></FlexStatementResponse>"
)


def test_fetch_waits_until_the_statement_is_ready() -> None:
    """IBKR may need a moment to generate the statement."""
    ibkr, waits = FakeIbkr(SENT, NOT_READY, "Symbol,Type\n"), list[float]()
    assert fetch("token", "123", get=ibkr, sleep=waits.append) == "Symbol,Type\n"
    assert ibkr.calls == [
        (SEND_URL, {"t": "token", "q": "123", "v": "3"}),
        ("https://example.test/GetStatement", {"t": "token", "q": "42", "v": "3"}),
        ("https://example.test/GetStatement", {"t": "token", "q": "42", "v": "3"}),
    ]
    assert waits == [5]


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        (
            [
                (
                    "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1015</ErrorCode>"
                    "<ErrorMessage>Token is invalid.</ErrorMessage></FlexStatementResponse>"
                )
            ],
            "request refused: 1015",
        ),
        (
            [
                SENT,
                (
                    "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1020</ErrorCode>"
                    "</FlexStatementResponse>"
                ),
            ],
            "statement refused: 1020",
        ),
        ([SENT, NOT_READY, NOT_READY], "not ready after 2 attempts"),
    ],
    ids=["bad-token", "statement-failed", "never-ready"],
)
def test_fetch_failures_are_reported(responses: list[str], message: str) -> None:
    """A refusal, or a statement that never arrives, stops with IBKR's reason."""
    with pytest.raises(FlexError, match=message):
        fetch(
            "token", "123", get=FakeIbkr(*responses), sleep=lambda _: None, attempts=2
        )
