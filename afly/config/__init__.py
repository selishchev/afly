"""afly configuration models: project, profiles, and extract configs.

All config loading funnels through :class:`ConfigError` so the CLI's
top-level error handling has exactly one exception type to catch for "the
user's YAML is wrong" — as opposed to a bug in afly itself.
"""

from __future__ import annotations


class ConfigError(Exception):
    """Raised for any problem loading or validating afly's YAML configuration.

    Wraps pydantic's ``ValidationError`` (and plain YAML/IO errors) with the
    offending file path folded into the message, so a CLI command can just
    ``echo_error(str(exc))`` without having to know which layer failed.
    """


__all__ = ["ConfigError"]
