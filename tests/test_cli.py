from typer.testing import CliRunner

from section_scan.cli import app


def test_help_runs():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Capture a webpage" in result.output
