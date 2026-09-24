"""Tests for ``afly init-claude`` — Claude context scaffolding.

Covers: fresh creation, idempotent re-runs, marker-based injection into an
existing CLAUDE.md (append + in-place refresh of an old versioned marker)
with user content preserved, and that every packaged rule/skill is
materialized.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from afly.cli.commands.init_claude import _BLOCK_RE, run_init_claude
from afly.cli.main import cli

RULE_FILES = {
    "overview.md",
    "cli.md",
    "project.md",
    "extracts.md",
    "formats.md",
    "idempotency.md",
    "quotas.md",
}
SKILL_FILES = {
    ".claude/skills/afly-setup-project/SKILL.md",
    ".claude/skills/afly-new-extract/SKILL.md",
    ".claude/skills/afly-backfill/SKILL.md",
    ".claude/skills/afly-debug-run/SKILL.md",
}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestFreshScaffold:
    @pytest.mark.unit
    def test_creates_all_artifacts(self, tmp_path: Path) -> None:
        rc = run_init_claude(str(tmp_path))
        assert rc == 0

        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()
        text = _read(claude_md)
        # Exactly one managed block. The marker is intentionally version-less so a
        # no-op upgrade doesn't churn the block (see init_claude._BEGIN).
        assert text.count("<!-- BEGIN afly") == 1
        assert text.count("<!-- END afly -->") == 1
        assert "<!-- BEGIN afly (managed by `afly init-claude`" in text
        assert _BLOCK_RE.search(text) is not None

        rules_dir = tmp_path / ".claude" / "rules" / "afly"
        assert {p.name for p in rules_dir.glob("*.md")} == RULE_FILES

        for rel in SKILL_FILES:
            skill = tmp_path / rel
            assert skill.exists(), rel
            skill_name = rel.split("/")[-2]
            assert f"name: {skill_name}" in _read(skill)

    @pytest.mark.unit
    def test_block_points_to_rules_and_skills(self, tmp_path: Path) -> None:
        run_init_claude(str(tmp_path))
        text = _read(tmp_path / "CLAUDE.md")
        assert ".claude/rules/afly/" in text
        for skill_name in (
            "afly-setup-project",
            "afly-new-extract",
            "afly-backfill",
            "afly-debug-run",
        ):
            assert skill_name in text
        assert "idempotency.md" in text
        assert "quotas.md" in text


class TestIdempotency:
    @pytest.mark.unit
    def test_rerun_is_byte_identical_and_reports_unchanged(self, tmp_path: Path) -> None:
        run_init_claude(str(tmp_path))
        before = {p: _read(p) for p in tmp_path.rglob("*") if p.is_file()}

        runner = CliRunner()
        result = runner.invoke(cli, ["init-claude", "--target-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "unchanged" in result.output
        assert "created" not in result.output
        assert "updated" not in result.output

        after = {p: _read(p) for p in tmp_path.rglob("*") if p.is_file()}
        assert before.keys() == after.keys()
        assert before == after


class TestInjectionIntoExistingFile:
    @pytest.mark.unit
    def test_appends_block_and_preserves_user_content(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My rules\n\nAlways write tests.\n", encoding="utf-8")

        run_init_claude(str(tmp_path))
        text = _read(claude_md)

        assert "# My rules" in text
        assert "Always write tests." in text
        assert text.count("<!-- BEGIN afly") == 1
        assert text.count("<!-- END afly -->") == 1

    @pytest.mark.unit
    def test_refreshes_stale_versioned_block_in_place(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# Top\n\nmine above.\n\n"
            "<!-- BEGIN afly v0.0.1 (managed by `afly init-claude` — do not "
            "edit between these markers) -->\n"
            "OLD STALE CONTENT\n"
            "<!-- END afly -->\n\n"
            "mine below.\n",
            encoding="utf-8",
        )

        run_init_claude(str(tmp_path))
        text = _read(claude_md)

        # User content on both sides preserved; stale body gone; single block.
        assert "mine above." in text
        assert "mine below." in text
        assert "OLD STALE CONTENT" not in text
        assert text.count("<!-- BEGIN afly") == 1
        assert text.count("<!-- END afly -->") == 1
        # The old versioned marker is replaced by the current version-less one.
        assert "v0.0.1" not in text
        assert "<!-- BEGIN afly (managed by `afly init-claude`" in text


class TestCliWiring:
    @pytest.mark.unit
    def test_init_claude_command_runs(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init-claude", "--target-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "CLAUDE.md").exists()
        for rel in SKILL_FILES:
            assert (tmp_path / rel).exists(), rel
