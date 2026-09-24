"""Shared CLI output helpers so every command renders in one house style.

Ported from detectkit's ``cli/_output.py`` (see that project's tree-style
convention), minus its autotune-specific streaming renderer — afly's
commands don't have a comparable multi-stage engine to narrate.

House conventions:
- An item *with* something to report is a tree: a cyan-bold ``┌─ <name>``
  header followed by one child line per item (``│   `` for all but the
  last, ``└─ `` for the last).
- An item with *nothing* to do is a single ``•`` line.
- A per-item error is a red ``✗`` line (to stderr).
- A non-fatal, per-item warning is a yellow ``⚠`` line (to stderr).
- The final summary is a cyan-bold ``Done. …`` line.
"""

from __future__ import annotations

from collections.abc import Callable

import click


def echo_block(
    title: str,
    children: list[str],
    *,
    warnings: list[str] | None = None,
    echo: Callable[[str], None] = click.echo,
) -> None:
    """Print a cyan-bold ``┌─ title`` header with ``│``/``└─`` child lines.

    The injectable core of the house tree style: ``warnings`` render as
    yellow ``│`` continuation lines above the children, the last child gets
    the ``└─`` elbow. ``echo`` defaults to ``click.echo`` but can be any
    line sink. ``children`` must be non-empty.
    """
    echo(click.style(f"  ┌─ {title}", fg="cyan", bold=True))
    for warning in warnings or []:
        echo(click.style(f"  │   ⚠ {warning}", fg="yellow", bold=True))
    last = len(children) - 1
    for i, child in enumerate(children):
        prefix = "  └─ " if i == last else "  │   "
        echo(f"{prefix}{child}")


def echo_tree(name: str, children: list[str], *, warnings: list[str] | None = None) -> None:
    """Print a ``┌─ name`` header with ``│``/``└─`` child lines.

    ``warnings`` (if any) render as yellow ``│`` continuation lines above
    the children. ``children`` must be non-empty (an item with nothing to
    report should use :func:`echo_noop` instead). Thin wrapper over
    :func:`echo_block` (the CLI-default ``click.echo`` sink).
    """
    echo_block(name, children, warnings=warnings)


def echo_noop(name: str, reason: str) -> None:
    """An item with nothing to do — a single ``•`` line."""
    click.echo(f"  • {name}: {reason}")


def echo_error(message: str, *, name: str | None = None) -> None:
    """A failure — a red ``✗`` line on stderr. Prefixes *name* when given."""
    text = f"{name}: {message}" if name else message
    click.echo(click.style(f"  ✗ {text}", fg="red", bold=True), err=True)


def echo_warning(message: str, *, name: str | None = None) -> None:
    """A non-fatal warning — a yellow ``⚠`` line on stderr."""
    text = f"{name}: {message}" if name else message
    click.echo(click.style(f"  ⚠ {text}", fg="yellow", bold=True), err=True)


def echo_done(summary: str) -> None:
    """The closing ``Done. …`` summary (cyan, bold), preceded by a blank line."""
    click.echo()
    click.echo(click.style(f"Done. {summary}", fg="cyan", bold=True))
