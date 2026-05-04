from __future__ import annotations

import stat
from pathlib import Path

import pytest

from benchclaw.rendering import mermaid as mermaid_renderer

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)

SAMPLE_FLOWCHART = """flowchart LR
    A[Data Ingestion & Analysis] --> B(AI Insight Generation);
    B --> C{Personalized CX & Operations};
    C --> D[Optimized Marketing Execution];
    D --> E(Increased Customer LTV & Revenue);"""


def _wrap(src: str) -> str:
    return f"```mermaid\n{src}\n```"


def test_extract_single_block():
    text = "preamble\n\n" + _wrap(SAMPLE_FLOWCHART) + "\n\ntrailing"
    [block] = mermaid_renderer.extract_blocks(text)
    assert block.source == SAMPLE_FLOWCHART
    assert text[block.span[0] : block.span[1]].startswith("```mermaid")
    assert text[block.span[0] : block.span[1]].endswith("```")


def test_extract_multiple_blocks_returns_all():
    text = _wrap("graph TD\nA-->B") + "\nbetween\n" + _wrap(SAMPLE_FLOWCHART)
    blocks = mermaid_renderer.extract_blocks(text)
    assert [b.source for b in blocks] == ["graph TD\nA-->B", SAMPLE_FLOWCHART]


def test_extract_skips_empty_block():
    text = "```mermaid\n\n```\nthen\n" + _wrap(SAMPLE_FLOWCHART)
    [block] = mermaid_renderer.extract_blocks(text)
    assert block.source == SAMPLE_FLOWCHART


def test_extract_returns_empty_when_no_blocks():
    assert mermaid_renderer.extract_blocks("plain prose, no fences") == []
    assert mermaid_renderer.extract_blocks("```python\nx = 1\n```") == []


def test_extract_requires_closing_fence():
    text = "```mermaid\nflowchart LR\n  A --> B"
    assert mermaid_renderer.extract_blocks(text) == []


def test_cache_dir_is_created(tmp_path: Path):
    cache = mermaid_renderer.cache_dir(tmp_path)
    assert cache.is_dir()
    assert cache == tmp_path / "media" / "mermaid_cache"


def test_cache_key_is_stable_and_theme_sensitive():
    a = mermaid_renderer._cache_key("graph TD\nA-->B", "default")
    b = mermaid_renderer._cache_key("graph TD\nA-->B", "default")
    c = mermaid_renderer._cache_key("graph TD\nA-->B", "dark")
    d = mermaid_renderer._cache_key("graph TD\nA-->C", "default")
    assert a == b
    assert a != c
    assert a != d
    assert len(a) == 32


def _install_fake_mmdc(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch, *, exit_code: int = 0
) -> Path:
    """Install a fake mmdc shim on PATH that writes PNG_1X1 to the -o argument."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    # POSIX-safe inline base64 of PNG_1X1.
    import base64

    encoded = base64.b64encode(PNG_1X1).decode("ascii")
    script = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "out=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -o) out="$2"; shift 2 ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        f'echo "{encoded}" | base64 -d > "$out"\n'
        f"exit {exit_code}\n"
    )
    mmdc = bin_dir / "mmdc"
    mmdc.write_text(script)
    mmdc.chmod(mmdc.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    return mmdc


@pytest.mark.asyncio
async def test_render_uses_cache_when_png_exists(tmp_path: Path):
    cache = mermaid_renderer.cache_dir(tmp_path)
    key = mermaid_renderer._cache_key(SAMPLE_FLOWCHART, "default")
    expected = cache / f"{key}.png"
    expected.write_bytes(PNG_1X1)

    # No mmdc on PATH would normally fail, but the cache hit short-circuits.
    result = await mermaid_renderer.render(SAMPLE_FLOWCHART, tmp_path)
    assert result.status == "ok"
    assert result.png_path == expected
    assert result.source == SAMPLE_FLOWCHART


@pytest.mark.asyncio
async def test_render_returns_fail_when_mmdc_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no mmdc reachable
    result = await mermaid_renderer.render(SAMPLE_FLOWCHART, tmp_path)
    assert result.status == "fail"
    assert result.error == "mmdc not installed"
    assert result.source == SAMPLE_FLOWCHART


@pytest.mark.asyncio
async def test_render_invokes_mmdc_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_mmdc(tmp_path / "bin", monkeypatch)
    cache = mermaid_renderer.cache_dir(tmp_path)
    key = mermaid_renderer._cache_key(SAMPLE_FLOWCHART, "default")
    expected = cache / f"{key}.png"
    assert not expected.exists()

    result = await mermaid_renderer.render(SAMPLE_FLOWCHART, tmp_path)
    assert result.status == "ok"
    assert result.png_path == expected
    assert expected.read_bytes() == PNG_1X1
    # Source scratch file is cleaned up.
    assert not expected.with_suffix(".mmd").exists()


@pytest.mark.asyncio
async def test_render_failure_preserves_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_mmdc(tmp_path / "bin", monkeypatch, exit_code=1)
    result = await mermaid_renderer.render("not actually a diagram", tmp_path)
    assert result.status == "fail"
    assert result.source == "not actually a diagram"
    assert result.png_path is None
    assert result.error and result.error.startswith("mmdc returncode=1")


@pytest.mark.asyncio
async def test_render_second_call_hits_cache_without_mmdc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bin_dir = tmp_path / "bin"
    _install_fake_mmdc(bin_dir, monkeypatch)
    first = await mermaid_renderer.render(SAMPLE_FLOWCHART, tmp_path)
    assert first.status == "ok"

    # Remove mmdc; second call must still succeed via cache.
    (bin_dir / "mmdc").unlink()
    second = await mermaid_renderer.render(SAMPLE_FLOWCHART, tmp_path)
    assert second.status == "ok"
    assert second.png_path == first.png_path
