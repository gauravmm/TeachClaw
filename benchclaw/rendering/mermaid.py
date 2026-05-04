"""Mermaid diagram rendering. Channel-agnostic.

Public surface:

- ``extract_blocks(text)`` returns a list of ``MermaidBlock`` describing each
  fenced ``mermaid`` code block in ``text``, in order.
- ``render(source, theme="default")`` returns ``RenderedDiagram`` — either a
  PNG path or an error reason. Caches by ``sha256(source + theme)``.

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
    """Find fenced ``mermaid`` code blocks; return at most ``_MAX_DIAGRAMS`` matches.

    Excess matches are returned with ``status='fail'`` from ``render`` so the
    caller can post them as raw source. The cap on extraction here returns
    every match — let the caller decide what to do with extras.
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


def _cache_key(source: str, theme: str) -> str:
    digest = hashlib.sha256(f"{theme}\x00{source}".encode("utf-8")).hexdigest()
    return digest[:32]


async def render(
    source: str,
    workspace: Path,
    *,
    theme: str = "default",
    timeout: float = _DEFAULT_TIMEOUT,
) -> RenderedDiagram:
    """Render one Mermaid block to PNG via mmdc. Cached by (source, theme)."""
    cache = cache_dir(workspace)
    out_path = cache / f"{_cache_key(source, theme)}.png"
    if out_path.exists():
        return RenderedDiagram(status="ok", png_path=out_path, source=source)

    mmdc = shutil.which("mmdc")
    if mmdc is None:
        return RenderedDiagram(status="fail", error="mmdc not installed", source=source)

    src_path = out_path.with_suffix(".mmd")
    src_path.write_text(source, encoding="utf-8")
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return RenderedDiagram(status="fail", error="render timeout", source=source)
    except Exception as e:
        logger.warning(f"mmdc invocation failed: {e}")
        return RenderedDiagram(status="fail", error=str(e), source=source)
    finally:
        src_path.unlink(missing_ok=True)

    if proc.returncode != 0 or not out_path.exists():
        msg = (stderr or b"").decode("utf-8", errors="replace").strip()
        return RenderedDiagram(
            status="fail",
            error=f"mmdc returncode={proc.returncode}: {msg[:200]}",
            source=source,
        )
    return RenderedDiagram(status="ok", png_path=out_path, source=source)


def is_available() -> bool:
    return shutil.which("mmdc") is not None or os.environ.get("MERMAID_FORCE_OK") == "1"
