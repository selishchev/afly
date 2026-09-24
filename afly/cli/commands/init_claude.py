"""
Implementation of ``afly init-claude``.

Scaffolds Claude Code context into the folder that holds afly project(s), so
an AI assistant can natively help the user operate afly — configure extracts,
debug pulls, run backfills, and reason about the idempotent write path.

It writes three things into the target directory:

- ``CLAUDE.md`` — created if absent, otherwise a managed afly block is
  injected/refreshed between HTML-comment markers (existing content is kept).
- ``.claude/rules/afly/`` — the reference docs the assistant reads on demand
  (overview, cli, project, extracts, formats, idempotency, quotas).
- ``.claude/skills/`` — user-facing skills (``afly-setup-project``,
  ``afly-new-extract``, ``afly-backfill``, ``afly-debug-run``).

The source of truth for all of the above lives in ``afly/cli/assets/claude``
and ships with the package, so re-running this command after upgrading afly
refreshes the context to match the installed version. The operation is
idempotent: re-running with no upstream change reports everything unchanged.
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

import click

from afly import __version__
from afly.cli._output import echo_done, echo_tree

if TYPE_CHECKING:
    # ``importlib.resources.abc`` only exists on Python 3.11+. Import it for
    # typing only (``from __future__ import annotations`` keeps annotations as
    # strings, so it is never evaluated at runtime — safe on 3.10).
    from importlib.resources.abc import Traversable

# Region in CLAUDE.md owned by this command. The BEGIN marker is intentionally
# version-less: stamping the version here made the block churn on every release
# (a no-op upgrade still rewrote the marker), pushing users to re-run for nothing.
# The block now changes only when its content actually changes, so a refresh is a
# true no-op otherwise. The regex still matches an old *versioned* marker
# (`<!-- BEGIN afly v0.0.1 ... -->`), so refreshing an existing file replaces the
# whole region in place rather than appending a duplicate.
_BEGIN = "<!-- BEGIN afly (managed by `afly init-claude` — do not edit between these markers) -->"
_END = "<!-- END afly -->"
_BLOCK_RE = re.compile(r"<!-- BEGIN afly.*?-->.*?<!-- END afly -->", re.DOTALL)

# New CLAUDE.md preamble (only used when the file does not exist yet).
_NEW_FILE_HEADER = (
    "# Project guidance for Claude\n\n"
    "This file gives Claude Code context about this workspace. Add your own\n"
    "notes anywhere outside the afly block below.\n"
)


def _assets_root() -> Traversable:
    """Return the packaged ``assets/claude`` directory as a Traversable.

    Uses single-argument ``joinpath`` chaining; the multi-argument form is only
    available on Python 3.12+, while afly supports 3.10+.
    """
    return files("afly.cli").joinpath("assets").joinpath("claude")


def _render_block(section_text: str) -> str:
    """Wrap the packaged CLAUDE section in the managed markers."""
    return f"{_BEGIN}\n{section_text.strip()}\n{_END}"


def _write_if_changed(target: Path, content: str) -> str:
    """Write ``content`` to ``target``; return 'created' | 'updated' | 'unchanged'."""
    if target.exists():
        if target.read_text(encoding="utf-8") == content:
            return "unchanged"
        status = "updated"
    else:
        status = "created"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return status


def _inject_claude_md(target_dir: Path, section_text: str) -> str:
    """Create or refresh the managed afly block in ``CLAUDE.md``.

    Returns the status of the *block* ('created' | 'updated' | 'added' |
    'unchanged'). Any user content outside the markers is preserved verbatim.
    """
    claude_md = target_dir / "CLAUDE.md"
    block = _render_block(section_text)

    if not claude_md.exists():
        claude_md.write_text(f"{_NEW_FILE_HEADER}\n{block}\n", encoding="utf-8")
        return "created"

    original = claude_md.read_text(encoding="utf-8")
    if _BLOCK_RE.search(original):
        updated = _BLOCK_RE.sub(lambda _m: block, original, count=1)
        status = "updated"
    else:
        sep = "" if original.endswith("\n\n") else ("\n" if original.endswith("\n") else "\n\n")
        updated = f"{original}{sep}{block}\n"
        status = "added"

    if updated == original:
        return "unchanged"
    claude_md.write_text(updated, encoding="utf-8")
    return status


def _copy_tree(src: Traversable, dst: Path, results: list[tuple[str, str]], _rel: str = "") -> None:
    """Recursively materialize a packaged Traversable tree under ``dst``.

    ``results`` accumulates ``(relative_path, status)`` for reporting (relative
    to the copy root), where status is 'created' | 'updated' | 'unchanged'.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir(), key=lambda c: c.name):
        target = dst / child.name
        rel = f"{_rel}{child.name}"
        if child.is_dir():
            _copy_tree(child, target, results, f"{rel}/")
        else:
            status = _write_if_changed(target, child.read_text(encoding="utf-8"))
            results.append((rel, status))


def _summarize(results: list[tuple[str, str]]) -> str:
    counts: dict[str, int] = {}
    for _name, status in results:
        counts[status] = counts.get(status, 0) + 1
    return ", ".join(f"{n} {status}" for status, n in counts.items())


def run_init_claude(target_dir: str) -> int:
    """Generate Claude context (CLAUDE.md + .claude/rules + skills) in ``target_dir``.

    Always returns 0 — every failure mode here (an unwritable target
    directory, a corrupt packaged asset) is a genuine bug worth a traceback,
    not a user-facing error this command should swallow into an exit code.
    """
    target_path = Path(target_dir).resolve()
    target_path.mkdir(parents=True, exist_ok=True)

    assets = _assets_root()
    section_text = assets.joinpath("CLAUDE.section.md").read_text(encoding="utf-8")

    click.echo(f"Target: {target_path}")
    click.echo()

    # 1) CLAUDE.md
    claude_status = _inject_claude_md(target_path, section_text)
    echo_tree("CLAUDE.md", [f"afly section {claude_status}"])

    # 2) .claude/rules/afly/
    rule_results: list[tuple[str, str]] = []
    _copy_tree(
        assets.joinpath("rules"),
        target_path / ".claude" / "rules" / "afly",
        rule_results,
    )
    echo_tree(
        ".claude/rules/afly/",
        [f"{name} ({status})" for name, status in rule_results],
    )

    # 3) .claude/skills/
    skill_results: list[tuple[str, str]] = []
    _copy_tree(
        assets.joinpath("skills"),
        target_path / ".claude" / "skills",
        skill_results,
    )
    echo_tree(
        ".claude/skills/",
        [f"{name} ({status})" for name, status in skill_results],
    )

    all_results = [("CLAUDE.md", claude_status), *rule_results, *skill_results]
    echo_done(f"Claude context ready for afly v{__version__} ({_summarize(all_results)}).")
    click.echo()
    click.echo("Open this folder in Claude Code and ask it to help with your extracts,")
    click.echo("or run the `afly-new-extract` skill to scaffold one.")
    return 0


__all__ = ["run_init_claude"]
