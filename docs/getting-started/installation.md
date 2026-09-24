# Installation

## Requirements

- Python 3.10+
- A ClickHouse server reachable over its **native protocol** port (commonly
  `9000`, or `9440` for TLS — not the HTTP port `8123`). Tested against
  ClickHouse 22.11.
- An AppsFlyer account with **API V2** (Pull API) access and a Bearer token.

## Install

```bash
pip install afly
```

This installs the `afly` console script and its runtime dependencies
(`click`, `pydantic`, `pyyaml`, `requests`, `clickhouse-driver`).

## Development install

To work on afly itself (not a project that uses it), see the repo-level
`CLAUDE.md` and `.claude/rules/contributing.md`:

```bash
git clone https://github.com/selishchev/afly.git
cd afly
python -m venv .venv
.venv/bin/pip install -e ".[dev,integration]"
```

## Verify

```bash
afly --version
```

Next: [Quickstart](quickstart.md).
