"""The cgt_tools command line."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from scripts.cgt_tools import ibkr_flex
from scripts.cgt_tools.cli import app
from tests.tools.ibkr_export import ibkr_export

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def test_module_runs_as_a_command() -> None:
    """``python -m scripts.cgt_tools`` lists the commands."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.cgt_tools", "--help"],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    assert "ibkr-merge" in result.stdout
    assert "cross-check" in result.stdout


def test_merge_writes_one_file(tmp_path: Path) -> None:
    """Exports in, one merged export out, and a row moved out on request."""
    export = ibkr_export(
        tmp_path / "a.csv",
        [
            "2025-01-02,Deposit,-,-,-,-,100,-,100,1.0",
            "2025-02-03,Sell,ABC,-1,5,GBP,5,-,5,1.0",
        ],
    )
    result = runner.invoke(
        app,
        [
            "ibkr-merge",
            str(tmp_path / "out.csv"),
            str(export),
            "--drop",
            "2025-02-03,Sell,ABC",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dropped 2025-02-03 Sell ABC" in result.output
    assert "Ending Cash,100" in (tmp_path / "out.csv").read_text(encoding="utf-8")


@pytest.mark.parametrize("drop", ["2025-02-03,Sell", "2025-02-03,Sell,XYZ"])
def test_merge_failure_exits_non_zero(tmp_path: Path, drop: str) -> None:
    """A malformed or missing drop stops the merge without writing."""
    export = ibkr_export(
        tmp_path / "a.csv", ["2025-01-02,Deposit,-,-,-,-,100,-,100,1.0"]
    )
    result = runner.invoke(
        app, ["ibkr-merge", str(tmp_path / "out.csv"), str(export), "--drop", drop]
    )
    assert result.exit_code == 1
    assert not (tmp_path / "out.csv").exists()


def test_corporate_actions_writes_raw_and_spin_off_files(tmp_path: Path) -> None:
    """A Flex export becomes the two files cgt-calc takes."""
    flex = tmp_path / "flex.csv"
    flex.write_text(
        "Asset Class,Currency,Symbol,Description,Date/Time,Quantity,Type\n"
        "STK,USD,NEWCO,ABC(US0000000001) SPINOFF 1 FOR 2,20250314,3,SO\n",
        encoding="utf-8",
    )
    raw, spins = tmp_path / "raw.csv", tmp_path / "spins.csv"
    result = runner.invoke(
        app, ["ibkr-corporate-actions", str(flex), str(raw), str(spins)]
    )
    assert result.exit_code == 0, result.output
    assert (
        raw.read_text(encoding="utf-8").splitlines()[1]
        == "2025-03-14,SPIN_OFF,NEWCO,3,0,0,USD"
    )
    assert spins.read_text(encoding="utf-8") == "dst,src\nNEWCO,ABC\n"


def test_corporate_actions_refusal_exits_non_zero(tmp_path: Path) -> None:
    """An unsupported event is named and nothing is written."""
    flex = tmp_path / "flex.csv"
    flex.write_text(
        "Asset Class,Symbol,Description,Date/Time,Quantity,Type\nSTK,ABC,MERGER,20250314,1,TC\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["ibkr-corporate-actions", str(flex), str(tmp_path / "r"), str(tmp_path / "s")],
    )
    assert result.exit_code == 1
    assert "type TC is not supported" in result.output


def test_fetch_reads_token_and_query_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token stays out of the command line and shell history."""
    seen: list[tuple[str, str]] = []

    def fake_fetch(token: str, query_id: str) -> str:
        seen.append((token, query_id))
        return "Symbol,Type\n"

    monkeypatch.setattr(ibkr_flex, "fetch", fake_fetch)
    out = tmp_path / "flex.csv"
    result = runner.invoke(
        app,
        ["ibkr-fetch", str(out)],
        env={"IBKR_FLEX_TOKEN": "t", "IBKR_FLEX_QUERY": "q"},
    )
    assert result.exit_code == 0, result.output
    assert seen == [("t", "q")]
    assert out.read_text(encoding="utf-8") == "Symbol,Type\n"


def test_fetch_failure_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IBKR's refusal is shown and the command fails."""

    def refuse(token: str, query_id: str) -> str:
        raise ibkr_flex.FlexError("Flex request refused: 1015 Token is invalid.")

    monkeypatch.setattr(ibkr_flex, "fetch", refuse)
    result = runner.invoke(
        app, ["ibkr-fetch", str(tmp_path / "x"), "--token", "t", "--query", "q"]
    )
    assert result.exit_code == 1
    assert "1015" in result.output
