"""Tests for the lesson-pack loader and validator (spec/SWITCHMODE.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from teachclaw.lessons import (
    LessonValidationError,
    lesson_forbidden_files,
    load_infra_overlay,
    load_onboarding,
    validate_workspace,
)


def _write_minimal_pack(root: Path) -> None:
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text("# Test pack\n", encoding="utf-8")
    (root / "personalities.yaml").write_text(
        "personalities:\n"
        "  - name: default\n"
        "    label: Default\n"
        "    description: Neutral assistant.\n"
        '    overlay: ""\n'
        "  - name: ta\n"
        "    label: TA\n"
        "    description: Patient teaching assistant.\n"
        "    overlay: |\n"
        "      Adopt the voice of a patient TA.\n",
        encoding="utf-8",
    )
    (root / "onboarding.yaml").write_text(
        "pre_auth_welcome: |\n"
        "  Welcome. Personas: {persona_pitch}.\n"
        "group_welcome_pre_auth: |\n"
        "  Group pre-auth.\n"
        "group_welcome_authed: |\n"
        "  Group authed (react {sources_reaction}).\n"
        "post_auth_welcome: |\n"
        "  Post auth (react {trace_reaction}).\n"
        "example_prompts:\n"
        "  - label: A demo\n"
        "    prompt: do A\n"
        "  - label: B demo\n"
        "    prompt: do B\n"
        "help_text: |\n"
        "  Help text.\n",
        encoding="utf-8",
    )


def test_minimal_pack_validates(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    validate_workspace(tmp_path)  # does not raise

    onb = load_onboarding(tmp_path)
    assert onb.pre_auth_welcome.startswith("Welcome.")
    assert len(onb.example_prompts) == 2
    assert onb.example_prompts[0].label == "A demo"

    overlay = load_infra_overlay(tmp_path)
    assert overlay.mcp_servers == ()
    assert overlay.shared_roots is None  # absent → don't override

    forbidden = lesson_forbidden_files(tmp_path)
    names = {p.name for p in forbidden}
    assert names == {"AGENTS.md", "personalities.yaml", "onboarding.yaml"}


def test_missing_agents_md(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "AGENTS.md").unlink()
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("AGENTS.md" in p for p in exc.value.problems)


def test_missing_skills_dir(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "skills").rmdir()
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("skills/" in p for p in exc.value.problems)


def test_personalities_no_default(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "personalities.yaml").write_text(
        "personalities:\n"
        "  - name: ta\n"
        "    label: TA\n"
        "    description: Patient TA.\n"
        "    overlay: |\n"
        "      Be patient.\n",
        encoding="utf-8",
    )
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("default" in p for p in exc.value.problems)


def test_personalities_duplicate_name(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "personalities.yaml").write_text(
        "personalities:\n"
        "  - name: default\n"
        "    label: Default\n"
        "    description: a\n"
        '    overlay: ""\n'
        "  - name: ta\n"
        "    label: TA\n"
        "    description: a\n"
        "    overlay: |\n"
        "      Be patient.\n"
        "  - name: ta\n"
        "    label: TA2\n"
        "    description: b\n"
        "    overlay: |\n"
        "      Other.\n",
        encoding="utf-8",
    )
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("duplicate" in p.lower() for p in exc.value.problems)


def test_personalities_empty_overlay_for_non_default(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "personalities.yaml").write_text(
        "personalities:\n"
        "  - name: default\n"
        "    label: Default\n"
        "    description: a\n"
        '    overlay: ""\n'
        "  - name: ta\n"
        "    label: TA\n"
        "    description: a\n"
        '    overlay: ""\n',  # empty - only default may
        encoding="utf-8",
    )
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("empty overlay" in p for p in exc.value.problems)


def test_onboarding_missing_key(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "onboarding.yaml").write_text(
        "pre_auth_welcome: |\n  hi\n"
        "group_welcome_pre_auth: |\n  hi\n"
        "group_welcome_authed: |\n  hi\n"
        # post_auth_welcome missing
        "example_prompts:\n  - label: A\n    prompt: do A\n"
        "help_text: |\n  help\n",
        encoding="utf-8",
    )
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("post_auth_welcome" in p for p in exc.value.problems)


def test_onboarding_unknown_placeholder(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "onboarding.yaml").write_text(
        "pre_auth_welcome: |\n  Welcome {unknown_thing}.\n"
        "group_welcome_pre_auth: |\n  hi\n"
        "group_welcome_authed: |\n  hi\n"
        "post_auth_welcome: |\n  hi\n"
        "example_prompts:\n  - label: A\n    prompt: do A\n"
        "help_text: |\n  help\n",
        encoding="utf-8",
    )
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("unknown_thing" in p for p in exc.value.problems)


def test_onboarding_too_many_prompts(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "onboarding.yaml").write_text(
        "pre_auth_welcome: |\n  hi\n"
        "group_welcome_pre_auth: |\n  hi\n"
        "group_welcome_authed: |\n  hi\n"
        "post_auth_welcome: |\n  hi\n"
        "example_prompts:\n"
        "  - label: A\n    prompt: x\n"
        "  - label: B\n    prompt: x\n"
        "  - label: C\n    prompt: x\n"
        "  - label: D\n    prompt: x\n"
        "  - label: E\n    prompt: x\n"  # 5, max is 4
        "help_text: |\n  help\n",
        encoding="utf-8",
    )
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("example_prompts" in p for p in exc.value.problems)


def test_infra_unknown_top_level_key(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "infra.yaml").write_text(
        "mcp_server:\n  - name: kb\n    transport: stdio\n    command: x\n",
        encoding="utf-8",
    )
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("mcp_server" in p for p in exc.value.problems)


def test_infra_mcp_missing_name(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "infra.yaml").write_text(
        "mcp_servers:\n  - transport: stdio\n    command: x\n",
        encoding="utf-8",
    )
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("name" in p for p in exc.value.problems)


def test_infra_shared_roots_alias_with_slash(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "infra.yaml").write_text(
        "media:\n  shared_roots:\n    bad/alias: /tmp\n",
        encoding="utf-8",
    )
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    assert any("slash" in p.lower() for p in exc.value.problems)


def test_aggregated_problems(tmp_path: Path) -> None:
    """Multiple problems should surface in one exception, not stop at the first."""
    _write_minimal_pack(tmp_path)
    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "personalities.yaml").write_text(
        "personalities:\n"
        "  - name: only\n"
        "    label: x\n"
        "    description: y\n"
        "    overlay: |\n"
        "      z\n",
        encoding="utf-8",
    )
    with pytest.raises(LessonValidationError) as exc:
        validate_workspace(tmp_path)
    problems = exc.value.problems
    assert any("AGENTS.md" in p for p in problems)
    assert any("default" in p for p in problems)


def test_infra_overlay_loads_mcp_and_shared_roots(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "infra.yaml").write_text(
        "mcp_servers:\n"
        "  - name: kb\n"
        "    transport: stdio\n"
        "    command: sh\n"
        "    args: ['-c', 'echo hi']\n"
        "media:\n"
        "  shared_roots:\n"
        "    cuteness: /tmp\n",
        encoding="utf-8",
    )
    validate_workspace(tmp_path)
    overlay = load_infra_overlay(tmp_path)
    assert len(overlay.mcp_servers) == 1
    assert overlay.mcp_servers[0].name == "kb"
    assert overlay.shared_roots == {"cuteness": "/tmp"}


def test_forbidden_files_includes_infra_when_present(tmp_path: Path) -> None:
    _write_minimal_pack(tmp_path)
    (tmp_path / "infra.yaml").write_text("mcp_servers: []\n", encoding="utf-8")
    forbidden = lesson_forbidden_files(tmp_path)
    names = {p.name for p in forbidden}
    assert "infra.yaml" in names
