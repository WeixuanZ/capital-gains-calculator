"""Recalculate gains independently of cgt-calc, to check what it reports.

Deliberately small and separate from cgt-calc's own engine. It reads the same
Trading 212 CSVs, Interactive Brokers Transaction History and RAW
corporate-action file, and works out every disposal in a tax year:

- Shares pool per ticker across brokers. A day's disposals match that day's
  acquisitions, then those in the 30 days after (earliest first, earlier
  disposals first), then the Section 104 pool (TCGA 1992 s104-106A).
- STOCK_SPLIT adds units at no cost. SPIN_OFF moves a given fraction of the
  parent's pooled cost to the new holding. Excess reported income adds to a
  fund's cost while it is held. An IBKR Transaction Fee is a cost of the day.
- Bought options pool per series and lapse for nothing at expiry. Written
  options are disposals when written, less buy-back costs taken oldest grant
  first (s144, s148), and keep the premium when they expire.
- UK gilts are exempt and left out.

Amounts are the pounds each broker recorded (T212 Total, IBKR Net Amount), so a
gain is net proceeds less net cost however the fees are presented. It assumes
what it cannot model does not happen and asserts so: a 30-day match across a
split, or the rebuy of a sold option series within 30 days.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal
import re
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

UTC: Final = ZoneInfo("UTC")
LONDON: Final = ZoneInfo("Europe/London")
OCC: Final = re.compile(r"(?P<root>[A-Z.]+)\s+(?P<expiry>\d{6})[CP]\d{8}")
SETTLEMENT_DAYS: Final = 7
MATCH_DAYS: Final = 30
PENNY: Final = Decimal("0.01")


@dataclass
class Inputs:
    """Everything the recalculation reads, already in pounds."""

    trades: list[tuple[dt.date, str, str, Decimal, Decimal]] = field(
        default_factory=list
    )
    events: list[tuple[dt.date, str, str, Decimal, Decimal]] = field(
        default_factory=list
    )
    options: list[tuple[dt.date, str, str, Decimal, Decimal]] = field(
        default_factory=list
    )


def read_inputs(
    *,
    t212_dir: Path | None,
    ibkr_file: Path | None,
    raw_file: Path | None,
    eri: Iterable[tuple[dt.date, str, Decimal]] = (),
    aliases: dict[str, str] | None = None,
) -> Inputs:
    """Read the broker files into share trades, share events and option trades."""
    aliases = aliases or {}
    data = Inputs()
    for path in sorted(t212_dir.glob("*.csv")) if t212_dir else []:
        with path.open(encoding="utf-8-sig") as file:
            for r in csv.DictReader(file):
                action = r["Action"].lower()
                if "buy" not in action and "sell" not in action:
                    continue
                if r["Currency (Total)"] != "GBP":
                    raise ValueError(
                        f"{path.name}: only a GBP Trading 212 account is supported"
                    )
                when = dt.datetime.fromisoformat(r["Time"][:19]).replace(tzinfo=UTC)
                side = "B" if "buy" in action else "S"
                data.trades.append(
                    (
                        when.astimezone(LONDON).date(),
                        side,
                        r["Ticker"],
                        Decimal(r["No. of shares"]),
                        Decimal(r["Total"]),
                    )
                )
    if ibkr_file:
        with ibkr_file.open(encoding="utf-8-sig") as file:
            for rec in csv.reader(file):
                if rec[:2] != ["Transaction History", "Data"]:
                    continue
                day, kind, symbol = (
                    dt.date.fromisoformat(rec[2]),
                    rec[5],
                    aliases.get(rec[6], rec[6]),
                )
                net = Decimal(rec[12]) if rec[12] != "-" else Decimal(0)
                if kind == "Transaction Fee" and symbol != "-":
                    data.trades.append((day, "B", symbol, Decimal(0), -net))
                elif kind not in {"Buy", "Sell"} or symbol.startswith("UKT "):
                    continue
                elif OCC.fullmatch(rec[6]):
                    data.options.append((day, kind, rec[6], abs(Decimal(rec[7])), net))
                else:
                    data.trades.append(
                        (
                            day,
                            "B" if kind == "Buy" else "S",
                            symbol,
                            abs(Decimal(rec[7])),
                            abs(net),
                        )
                    )
    if raw_file:
        with raw_file.open(encoding="utf-8") as file:
            for r in csv.DictReader(file):
                day, quantity = dt.date.fromisoformat(r["date"]), Decimal(r["quantity"])
                if r["action"] in {"BUY", "SELL"}:
                    amount = quantity * Decimal(r["price"])
                    fees = Decimal(r["fees"])
                    gbp = amount + fees if r["action"] == "BUY" else amount - fees
                    data.trades.append(
                        (day, r["action"][0], r["symbol"], quantity, gbp)
                    )
                elif r["action"] in {"STOCK_SPLIT", "SPIN_OFF"}:
                    data.events.append(
                        (day, r["action"], r["symbol"], quantity, Decimal(0))
                    )
                else:
                    raise ValueError(f"RAW action {r['action']} is not modelled")
    data.events += [(day, "ERI", symbol, Decimal(0), gbp) for day, symbol, gbp in eri]
    return data


type Disposals = dict[tuple[dt.date, str], tuple[Decimal, Decimal]]


def share_disposals(
    data: Inputs, spin_offs: dict[str, tuple[str, Decimal]]
) -> Disposals:
    """Return {(date, symbol): (proceeds, cost)}; spin_offs maps child to (parent, fraction)."""
    days: dict[tuple[str, dt.date], dict[str, list[Decimal]]] = defaultdict(
        lambda: {"B": [Decimal(0), Decimal(0)], "S": [Decimal(0), Decimal(0)]}
    )
    for day, side, symbol, quantity, gbp in data.trades:
        cell = days[symbol, day][side]
        cell[0] += quantity
        cell[1] += gbp
    unpooled = {key: list(value["B"]) for key, value in days.items()}
    matched: dict[tuple[str, dt.date], list[Decimal]] = {}
    splits = {(e[2], e[0]) for e in data.events if e[1] == "STOCK_SPLIT"}

    def take(bought: list[Decimal], wanted: Decimal) -> tuple[Decimal, Decimal]:
        quantity = min(wanted, bought[0])
        cost = bought[1] * quantity / bought[0] if quantity else Decimal(0)
        bought[0] -= quantity
        bought[1] -= cost
        return quantity, cost

    for symbol in {s for s, _ in days}:
        dates = sorted(d for s, d in days if s == symbol)
        for day in dates:  # same day first, for every day
            quantity, cost = take(unpooled[symbol, day], days[symbol, day]["S"][0])
            matched[symbol, day] = [days[symbol, day]["S"][0] - quantity, cost]
        for day in dates:  # then the 30 days after, earlier disposals first
            pending = matched[symbol, day]
            for later in (
                d for d in dates if day < d <= day + dt.timedelta(MATCH_DAYS)
            ):
                quantity, cost = take(unpooled[symbol, later], pending[0])
                assert not quantity or not any(
                    s == symbol and day < d <= later for s, d in splits
                ), f"a 30-day match of {symbol} across a split is not modelled"
                pending[0] -= quantity
                pending[1] += cost

    pools: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    result: Disposals = {}
    for day in sorted({d for _, d in days} | {e[0] for e in data.events}):
        for _, kind, symbol, quantity, gbp in (e for e in data.events if e[0] == day):
            pool = pools[symbol]
            if kind == "STOCK_SPLIT":
                pool[0] += quantity
            elif kind == "ERI" and pool[0]:
                pool[1] += gbp
            elif kind == "SPIN_OFF":
                parent_symbol, fraction = spin_offs[symbol]
                parent = pools[parent_symbol]
                moved = parent[1] * fraction
                parent[1] -= moved
                pool[0] += quantity
                pool[1] += moved
        for symbol in sorted(s for s, d in days if d == day):
            pool, bought = pools[symbol], unpooled[symbol, day]
            pool[0] += bought[0]
            pool[1] += bought[1]
            sold, proceeds = days[symbol, day]["S"]
            if not sold:
                continue
            left, cost = matched[symbol, day]
            if left:
                if left > pool[0]:
                    raise ValueError(
                        f"{symbol} sold on {day} beyond the {pool[0]} held"
                    )
                share = pool[1] * left / pool[0]
                pool[0] -= left
                pool[1] -= share
                cost += share
            result[day, symbol] = (proceeds, cost)
    return result


@dataclass
class _Grant:
    """A written option's disposal and how many of its contracts are still written."""

    key: tuple[dt.date, str]
    written: Decimal


def option_disposals(data: Inputs, period_end: dt.date) -> Disposals:
    """Return {(date, series): (proceeds, cost)} for option disposals."""
    result: dict[tuple[dt.date, str], list[Decimal]] = defaultdict(
        lambda: [Decimal(0), Decimal(0)]
    )
    series: dict[str, list[tuple[dt.date, str, str, Decimal, Decimal]]] = defaultdict(
        list
    )
    for trade in data.options:
        series[trade[2]].append(trade)
    for occ, trades in series.items():
        match = OCC.fullmatch(occ)
        assert match is not None
        expiry = dt.datetime.strptime(match["expiry"], "%y%m%d").date()
        held, held_cost = Decimal(0), Decimal(0)
        last_sale: dt.date | None = None
        grants: list[_Grant] = []
        for day, side, _, quantity, net in sorted(trades, key=lambda t: t[0]):
            if side == "Buy":
                closing = min(quantity, sum((g.written for g in grants), Decimal(0)))
                remaining = closing
                for grant in grants:  # a buy-back is a cost of the oldest grants (s148)
                    taken = min(remaining, grant.written)
                    grant.written -= taken
                    remaining -= taken
                    result[grant.key][1] += -net * taken / quantity
                opening = quantity - closing
                assert (
                    not opening
                    or last_sale is None
                    or day > last_sale + dt.timedelta(MATCH_DAYS)
                ), f"a 30-day rebuy of {occ} is not modelled"
                held += opening
                held_cost += -net * opening / quantity
            else:
                closing = min(quantity, held)
                if closing:
                    share = held_cost * closing / held
                    result[day, occ][0] += net * closing / quantity
                    result[day, occ][1] += share
                    held -= closing
                    held_cost -= share
                    last_sale = day
                if quantity - closing:
                    result[day, occ][0] += net * (quantity - closing) / quantity
                    grants.append(_Grant((day, occ), quantity - closing))
        if held and expiry + dt.timedelta(SETTLEMENT_DAYS) <= period_end:
            result[expiry, occ][1] += held_cost  # lapsed: a disposal for nothing
    return {key: (value[0], value[1]) for key, value in result.items()}


def gains_in_year(disposals: Disposals, year: int) -> Counter[tuple[dt.date, Decimal]]:
    """Count each disposal's (date, gain to the penny) in the tax year starting in `year`."""
    start, end = dt.date(year, 4, 6), dt.date(year + 1, 4, 5)
    return Counter(
        (day, (proceeds - cost).quantize(PENNY))
        for (day, _), (proceeds, cost) in disposals.items()
        if start <= day <= end
    )


def report_gains(tex: str) -> Counter[tuple[dt.date, Decimal]]:
    """Count each disposal's (date, gain) in a cgt-calc LaTeX report."""
    gains: Counter[tuple[dt.date, Decimal]] = Counter()
    for section in re.split(r"\\subsection\*\{", tex)[1:]:
        try:
            day = dt.datetime.strptime(section[: section.index("}")], "%d %B %Y").date()
        except ValueError:
            continue  # a summary heading, not a day
        for sign, amount in re.findall(
            r"Chargeable \\textbf\{(gain|loss)\} is £([\d,.]+)", section
        ):
            value = Decimal(amount.replace(",", ""))
            gains[day, value if sign == "gain" else -value] += 1
    return gains


def differences(
    mine: Counter[tuple[dt.date, Decimal]], theirs: Counter[tuple[dt.date, Decimal]]
) -> list[tuple[dt.date, list[Decimal], list[Decimal]]]:
    """Return the days whose gains differ by more than a penny."""
    found = []
    for day in sorted({d for d, _ in mine} | {d for d, _ in theirs}):
        a = sorted(g for (d, g), n in mine.items() if d == day for _ in range(n))
        b = sorted(g for (d, g), n in theirs.items() if d == day for _ in range(n))
        if len(a) != len(b) or any(
            abs(x - y) > PENNY for x, y in zip(a, b, strict=True)
        ):
            found.append((day, a, b))
    return found
