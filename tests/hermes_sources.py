"""Revision-pinned Hermes fixtures: reuse verified local files, download missing ones.

插件模式的上游契约检查（tests/test_upstream_compat.py）消费这些固定版本源码，
保证「插件依赖的 draft 契约锚点」在 CI 里始终对 pinned 上游 revision 验证。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from functools import cache
from pathlib import Path

SAMPLES_DIR = Path(__file__).parent / "samples"
MANIFEST_PATH = Path(__file__).with_suffix(".json")

# draft-streaming 契约相关上游文件（契约锚点见 plugin/contract.py）
TREE_FILES = (
    "gateway/stream_consumer.py",
    "gateway/stream_consumer_transport.py",
    "gateway/stream_consumer_think.py",
    "gateway/stream_consumer_fallback.py",
    "gateway/stream_consumer_fences.py",
    "gateway/run_busy.py",
)


@cache
def source_at(relative: str) -> str:
    """Validate cached sources; download missing files from the pinned commit only."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected = manifest["sha256"][relative]
    path = SAMPLES_DIR / relative
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        revision = manifest["revision"]
        url = f"https://raw.githubusercontent.com/NousResearch/hermes-agent/{revision}/{relative}"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                data = response.read()
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f"Cannot download Hermes fixture {revision}:{relative}: {exc}") from exc
        _validate(relative, data, expected)
        # Publish only a complete, verified download; interruption cannot poison the cache.
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
    else:
        _validate(relative, data, expected)
    return data.decode("utf-8")


def _validate(relative: str, data: bytes, expected: str) -> None:
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(
            f"Hermes fixture checksum mismatch: {relative}; "
            "remove the cached file to download the pinned version again"
        )
