"""Command line for the cgt-calc data tools: ``python -m scripts.cgt_tools --help``."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from . import cross_check, ibkr_flex, ibkr_merge

app = typer.Typer(no_args_is_help=True, help=__doc__)

Existing = Annotated[Path, typer.Argument(exists=True, dir_okay=False)]


def _fail(err: Exception) -> typer.Exit:
    typer.secho(f"Error: {err}", err=True, fg=typer.colors.RED)
    return typer.Exit(1)


@app.command("ibkr-merge")
def ibkr_merge_command(
    output: Annotated[Path, typer.Argument(help="Merged file to write.")],
    exports: Annotated[
        list[Path], typer.Argument(exists=True, dir_okay=False, help="Exports.")
    ],
    drop: Annotated[
        list[str] | None,
        typer.Option(
            help="Move a row out as DATE,TYPE,SYMBOL, e.g. 2025-01-02,Sell,ABC."
        ),
    ] = None,
) -> None:
    """Merge IBKR Transaction History exports of one account into one file."""
    try:
        drops = []
        for item in drop or []:
            date, kind, symbol = item.split(",")
            drops.append((date, kind, symbol))
        merged = ibkr_merge.merge([ibkr_merge.read_export(p) for p in exports], drops)
    except (ibkr_merge.MergeError, ValueError) as err:
        raise _fail(err) from err
    ibkr_merge.write(merged, output)
    for name, count in merged.used.items():
        typer.echo(f"{name}: {count} rows used")
    for row in merged.dropped:
        typer.echo(
            f"dropped {row['Date']} {row['Transaction Type']} {row['Symbol']}: record it in RAW"
        )
    typer.echo(
        f"wrote {len(merged.rows)} rows, {merged.start} to {merged.end}, to {output}"
    )


@app.command("ibkr-fetch")
def ibkr_fetch_command(
    output: Annotated[Path, typer.Argument(help="File to save the statement to.")],
    token: Annotated[
        str, typer.Option(envvar="IBKR_FLEX_TOKEN", help="Flex Web Service token.")
    ],
    query: Annotated[
        str, typer.Option(envvar="IBKR_FLEX_QUERY", help="Flex Query ID.")
    ],
) -> None:
    """Download a Flex Query's statement through the IBKR Flex Web Service."""
    try:
        output.write_text(ibkr_flex.fetch(token, query), encoding="utf-8")
    except ibkr_flex.FlexError as err:
        raise _fail(err) from err
    typer.echo(f"saved {output}")


@app.command("ibkr-corporate-actions")
def ibkr_corporate_actions_command(
    flex_csv: Existing,
    raw_output: Annotated[Path, typer.Argument(help="RAW file for --raw-file.")],
    spin_offs_output: Annotated[
        Path, typer.Argument(help="File for --spin-offs-file.")
    ],
) -> None:
    """Convert a Flex Query's corporate actions into cgt-calc RAW rows."""
    try:
        actions = ibkr_flex.corporate_actions(flex_csv.read_text(encoding="utf-8-sig"))
    except (ibkr_flex.FlexError, KeyError, ArithmeticError) as err:
        raise _fail(err) from err
    raw_output.write_text(actions.raw_csv(), encoding="utf-8")
    spin_offs_output.write_text(actions.spin_offs_csv(), encoding="utf-8")
    for row in actions.rows:
        typer.echo(f"{row.date} {row.action} {row.symbol} {row.quantity}")
    typer.echo(
        "Check each date against the company's announcement before relying on it."
    )


@app.command("cross-check")
def cross_check_command(
    year: Annotated[
        int, typer.Option(help="First year of the tax year, e.g. 2025 for 2025/26.")
    ],
    period_end: Annotated[
        dt.datetime,
        typer.Option(formats=["%Y-%m-%d"], help="Last day the exports cover."),
    ],
    report: Annotated[
        Path, typer.Option(exists=True, help="cgt-calc's .tex report (--no-pdflatex).")
    ],
    t212_dir: Annotated[Path | None, typer.Option(exists=True, file_okay=False)] = None,
    ibkr_file: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    raw_file: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    eri: Annotated[
        list[str] | None, typer.Option(help="Fund income as DATE,SYMBOL,GBP.")
    ] = None,
    alias: Annotated[
        list[str] | None, typer.Option(help="Ticker alias as FROM=TO.")
    ] = None,
    spin_off: Annotated[
        list[str] | None,
        typer.Option(help="CHILD=PARENT:FRACTION of the parent's cost moved."),
    ] = None,
) -> None:
    """Recalculate every disposal independently and compare with cgt-calc's report."""
    try:
        data = cross_check.read_inputs(
            t212_dir=t212_dir,
            ibkr_file=ibkr_file,
            raw_file=raw_file,
            eri=[
                (dt.date.fromisoformat(d), s, Decimal(g))
                for d, s, g in (item.split(",") for item in eri or [])
            ],
            aliases=dict(item.split("=") for item in alias or []),
        )
        spins = {
            child: (parent, Decimal(fraction))
            for child, rest in (item.split("=") for item in spin_off or [])
            for parent, fraction in [rest.split(":")]
        }
        mine = cross_check.gains_in_year(
            cross_check.share_disposals(data, spins)
            | cross_check.option_disposals(data, period_end.date()),
            year,
        )
    except (ValueError, KeyError, ArithmeticError) as err:
        raise _fail(err) from err
    theirs = cross_check.report_gains(report.read_text(encoding="utf-8"))
    gains = sum((g * n for (_, g), n in mine.items() if g > 0), Decimal(0))
    losses = -sum((g * n for (_, g), n in mine.items() if g < 0), Decimal(0))
    typer.echo(
        f"recalculated {sum(mine.values())} disposals: gains {gains}, losses {losses}, "
        f"net {gains - losses}"
    )
    found = cross_check.differences(mine, theirs)
    for day, recalculated, reported in found:
        typer.echo(f"{day}: recalculated {recalculated}, report {reported}")
    if found:
        raise typer.Exit(1)
    typer.echo(
        f"all {sum(theirs.values())} disposals in the report agree within a penny"
    )
