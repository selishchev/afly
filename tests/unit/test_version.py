import pytest
from click.testing import CliRunner

from afly import __version__
from afly.cli.main import cli


@pytest.mark.unit
def test_version() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output
