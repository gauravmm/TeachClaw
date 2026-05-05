"""Mermaid diagram rendering. Channel-agnostic.

Public surface:

- ``extract_blocks(text)`` returns a list of ``MermaidBlock`` describing each
  fenced ``mermaid`` code block in ``text``, in order.
- ``render(source, theme="default")`` returns ``RenderedDiagram`` — either a
  PNG path or an error reason. Caches by ``sha256(source + theme)``.
- ``render_blocks(blocks, ...)`` renders up to a fixed cap of blocks, marking
  the rest as ``status='fail'`` so callers don't have to know the cap.
- ``format_failure(source)`` returns the markdown the caller should post for
  any ``RenderedDiagram`` whose status is ``'fail'``.

The renderer shells out to ``mmdc`` (`@mermaid-js/mermaid-cli`); install it
via ``npm install -g @mermaid-js/mermaid-cli``. If ``mmdc`` is missing or
times out, ``render`` returns a failure with the original source so the
caller can fall back to posting the raw block.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

_FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
_DEFAULT_TIMEOUT = 5.0
_MAX_DIAGRAMS = 2
_MAX_DIM = 2048
# Chromium refuses to launch with its default sandbox on Ubuntu 23.10+ because
# unprivileged user namespaces are restricted by AppArmor. mmdc renders trusted
# content (our own prompt output), so dropping the browser sandbox is fine.
_PUPPETEER_CONFIG = '{"args": ["--no-sandbox"]}'


@dataclass(frozen=True)
class MermaidBlock:
    """One fenced ``mermaid`` block found in a reply."""

    source: str
    span: tuple[int, int]  # (start, end) byte offsets in the original text


@dataclass(frozen=True)
class RenderedDiagram:
    status: Literal["ok", "fail"]
    png_path: Path | None = None
    error: str | None = None
    source: str = ""


def extract_blocks(text: str) -> list[MermaidBlock]:
    """Find fenced ``mermaid`` code blocks; returns every match in order.

    Use ``render_blocks`` if you want the per-message render cap applied.
    """
    out: list[MermaidBlock] = []
    for m in _FENCE_RE.finditer(text):
        src = m.group(1).strip()
        if not src:
            continue
        out.append(MermaidBlock(source=src, span=m.span()))
    return out


def cache_dir(workspace: Path) -> Path:
    p = workspace / "media" / "mermaid_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _puppeteer_config_path(cache: Path) -> Path:
    p = cache / "puppeteer.json"
    if not p.exists():
        p.write_text(_PUPPETEER_CONFIG, encoding="utf-8")
    return p


def _cache_key(source: str, theme: str) -> str:
    digest = hashlib.sha256(f"{theme}\x00{source}".encode("utf-8")).hexdigest()
    return digest[:32]


def _resolve_mmdc(mmdc_path: str | None) -> str | None:
    """Pick an mmdc binary: explicit override first, then PATH lookup."""
    if mmdc_path:
        candidate = Path(mmdc_path).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        logger.warning(
            f"mermaid: configured mmdc_path={mmdc_path!r} is not an executable file; "
            "falling back to PATH lookup"
        )
    return shutil.which("mmdc")


async def render(
    source: str,
    workspace: Path,
    *,
    theme: str = "default",
    timeout: float = _DEFAULT_TIMEOUT,
    mmdc_path: str | None = None,
) -> RenderedDiagram:
    """Render one Mermaid block to PNG via mmdc. Cached by (source, theme)."""
    cache = cache_dir(workspace)
    key = _cache_key(source, theme)
    out_path = cache / f"{key}.png"
    if out_path.exists():
        logger.info(f"mermaid {key}: cache hit -> {out_path}")
        return RenderedDiagram(status="ok", png_path=out_path, source=source)

    mmdc = _resolve_mmdc(mmdc_path)
    if mmdc is None:
        logger.warning(
            f"mermaid {key}: mmdc not found (config.mermaid.mmdc_path={mmdc_path!r}, "
            f"PATH={os.environ.get('PATH', '')!r}); install with "
            "`npm install -g @mermaid-js/mermaid-cli` and either put its bin dir on PATH "
            "or set config.mermaid.mmdc_path to the absolute mmdc path"
        )
        return RenderedDiagram(status="fail", error="mmdc not installed", source=source)

    src_path = out_path.with_suffix(".mmd")
    src_path.write_text(source, encoding="utf-8")
    puppeteer_cfg = _puppeteer_config_path(cache)
    # mmdc's shebang is `#!/usr/bin/env node`; if mmdc lives in an nvm bin dir
    # that isn't on the bot's PATH, env can't find node. Prepend mmdc's own
    # directory to PATH so its sibling `node` is reachable.
    env = dict(os.environ)
    mmdc_bin_dir = str(Path(mmdc).parent)
    env["PATH"] = (
        mmdc_bin_dir if not env.get("PATH") else f"{mmdc_bin_dir}{os.pathsep}{env['PATH']}"
    )
    logger.info(
        f"mermaid {key}: rendering via {mmdc} (theme={theme}, "
        f"src={src_path}, out={out_path}, puppeteer={puppeteer_cfg}, "
        f"PATH-prepended={mmdc_bin_dir})"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            mmdc,
            "-i",
            str(src_path),
            "-o",
            str(out_path),
            "-t",
            theme,
            "-b",
            "transparent",
            "-w",
            str(_MAX_DIM),
            "-H",
            str(_MAX_DIM),
            "-p",
            str(puppeteer_cfg),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(f"mermaid {key}: render timeout after {timeout}s")
            return RenderedDiagram(status="fail", error="render timeout", source=source)
    except Exception as e:
        logger.warning(f"mermaid {key}: mmdc invocation failed: {e}")
        return RenderedDiagram(status="fail", error=str(e), source=source)
    finally:
        src_path.unlink(missing_ok=True)

    if proc.returncode != 0 or not out_path.exists():
        stderr_text = (stderr or b"").decode("utf-8", errors="replace").strip()
        stdout_text = (stdout or b"").decode("utf-8", errors="replace").strip()
        logger.warning(
            f"mermaid {key}: mmdc returncode={proc.returncode}, "
            f"out_exists={out_path.exists()}\n"
            f"  stdout: {stdout_text[:500]}\n"
            f"  stderr: {stderr_text[:500]}\n"
            f"  source: {source[:200]}"
        )
        return RenderedDiagram(
            status="fail",
            error=f"mmdc returncode={proc.returncode}: {stderr_text[:200]}",
            source=source,
        )
    logger.info(f"mermaid {key}: rendered ok ({out_path.stat().st_size} bytes)")
    return RenderedDiagram(status="ok", png_path=out_path, source=source)


async def render_blocks(
    blocks: list[MermaidBlock],
    workspace: Path,
    *,
    theme: str = "default",
    timeout: float = _DEFAULT_TIMEOUT,
    mmdc_path: str | None = None,
) -> list[RenderedDiagram]:
    """Render a list of blocks, capping rendered attempts at ``_MAX_DIAGRAMS``.

    Blocks past the cap come back as ``status='fail'`` carrying the original
    source, so callers see a uniform list and don't need to know the cap.
    """
    out: list[RenderedDiagram] = []
    for i, blk in enumerate(blocks):
        if i < _MAX_DIAGRAMS:
            out.append(
                await render(
                    blk.source, workspace, theme=theme, timeout=timeout, mmdc_path=mmdc_path
                )
            )
        else:
            out.append(
                RenderedDiagram(status="fail", error="diagram cap exceeded", source=blk.source)
            )
    return out


def format_failure(source: str) -> str:
    """Markdown the caller should post in lieu of a rendered diagram."""
    return "\n_couldn't render this diagram, source below_\n```\n" + source + "\n```\n"


def is_available(mmdc_path: str | None = None) -> bool:
    return _resolve_mmdc(mmdc_path) is not None or os.environ.get("MERMAID_FORCE_OK") == "1"
