"""afly CLI subcommand implementations, one module per command.

Each module exposes a single ``run_<command>(...)`` function with no click
dependency in its public signature (plain str/bool/int/None args, returns an
``int`` exit code) — ``cli/main.py`` is the only place click options get
parsed and turned into these calls, and it lazy-imports each module inside
its callback so ``afly --help`` never pays for every command's imports.
"""
