"""Keep the rejected draft of a Narrative Writer preview run.

A preview that fails validation is thrown away by design: the runner refuses
to hand a body to the dashboard unless every hard self-review holds. That is
the right behaviour for readers, but it leaves an operator debugging a model
or a relay with nothing but an error code and no way to see what the model
actually wrote.

This module writes that rejected output next to the instance database so the
draft can be read instead of re-generated. Only the model's own output is
stored. The prompt carries the bound source materials, so it is never
written here.
"""
import json
import os
import time
from pathlib import Path

MAX_CONTENT_CHARS = 200_000
MAX_FILES = 20
DEBUG_DIRNAME = "writer-debug"


def debug_dir(directory) -> Path:
    return Path(directory) / DEBUG_DIRNAME


def _rotate(target: Path) -> None:
    """Keep the most recent MAX_FILES dumps so the directory cannot grow forever."""
    try:
        existing = sorted(
            (item for item in target.parent.glob("*.json") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
        )
        for stale in existing[: max(0, len(existing) - MAX_FILES)]:
            stale.unlink()
    except OSError:
        pass


def record(directory, *, layer, error, model=None, content=None, finish_reason=None) -> None:
    """Append one rejected Writer output. Never raises, never blocks a request."""
    try:
        target = debug_dir(directory)
        target.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        text = content if isinstance(content, str) else ("" if content is None else repr(content))
        if text and len(text) > MAX_CONTENT_CHARS:
            text = text[:MAX_CONTENT_CHARS] + f"\n...[truncated, {len(text)} chars total]"
        record_path = target / f"{stamp}-{time.time_ns() % 1_000_000:06d}-{layer}.json"
        record_path.write_text(
            json.dumps(
                {
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "layer": layer,
                    "error": str(error)[:500],
                    "model": str(model or "")[:200],
                    "finish_reason": str(finish_reason or "")[:80],
                    "content": text,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(record_path, 0o600)
        _rotate(record_path)
    except Exception:
        # A debug trail must never turn a preview failure into a different failure.
        pass