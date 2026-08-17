"""Agent 脚本下载与本地缓存编排（P5.6，§9.3/§9.8）。

``run.assign`` 通过 ``script_ref`` 携带 ``{script_id, version, sha256,
download_url}``。Agent 在 claim 前把脚本下载到本地、做完整 SHA-256 校验，
再按 hash 组织目录写入缓存，并把引用记入 ``agent_script_cache``。

关键保证：

- **校验失败不落缓存**：只有内容 hash 与 ``script_ref.sha256`` 一致时才写
  文件与账本；不一致直接抛错，不产生任何磁盘/账本痕迹。
- **幂等**：同一 ``(script_id, version, sha256)`` 命中缓存后不再重复下载。
- **原子写入**：先写临时文件再 ``os.replace`` 到目标路径，避免中断留下
  半截脚本包被后续误用。

本模块只依赖 ``Ledger`` 端口与文件系统，不接触 MQTT/HTTP 框架。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Mapping
from urllib.request import urlopen
import uuid

from agent.domain.ledger import Ledger, ScriptCacheEntry

SCRIPT_REF_INVALID = "SCRIPT_REF_INVALID"
SCRIPT_DOWNLOAD_FAILED = "SCRIPT_DOWNLOAD_FAILED"
SCRIPT_CHECKSUM_FAILED = "SCRIPT_CHECKSUM_FAILED"


class ScriptCacheError(ValueError):
    """脚本下载/缓存错误基类（携带机器可读错误码）。"""

    code = "SCRIPT_CACHE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ScriptDownloadError(ScriptCacheError):
    """下载失败（网络/HTTP 错误）。"""

    code = SCRIPT_DOWNLOAD_FAILED


class ScriptChecksumError(ScriptCacheError):
    """SHA-256 校验失败。"""

    code = SCRIPT_CHECKSUM_FAILED


def _download(url: str) -> bytes:
    """通过 HTTP(S) 下载脚本包；超时受控，生产不允许无限等待。"""
    try:
        with urlopen(url, timeout=30) as response:  # noqa: S310 - URL 来自 Master
            return response.read()
    except ScriptCacheError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一映射下载错误
        raise ScriptDownloadError(f"脚本下载失败: {url}: {exc}") from exc


class ScriptCacheService:
    """下载、校验并按 hash 缓存脚本包。"""

    def __init__(
        self,
        cache_dir: str | Path,
        ledger: Ledger,
        *,
        fetcher: Callable[[str], bytes] | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir).resolve()
        self._ledger = ledger
        self._fetcher = fetcher or _download

    def ensure_cached(self, script_ref: Mapping[str, object]) -> ScriptCacheEntry:
        """确保脚本已按 hash 缓存，返回缓存引用。

        ``script_ref`` 必须包含 script_id / version / sha256 / download_url；
        缺失或非法抛 ``ScriptCacheError(SCRIPT_REF_INVALID)``。
        下载失败抛 ``ScriptDownloadError``；校验失败抛 ``ScriptChecksumError``，
        且**不落任何缓存**。
        """
        script_id, version, sha256, download_url = self._parse_ref(script_ref)

        existing = self._ledger.get_cached_script(script_id, version, sha256)
        if existing is not None and Path(existing.path).is_file():
            return existing

        try:
            data = self._fetcher(download_url)
        except ScriptCacheError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一映射下载错误
            raise ScriptDownloadError(
                f"脚本下载失败: {download_url}: {exc}"
            ) from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest.lower() != sha256.lower():
            raise ScriptChecksumError(
                f"脚本 SHA-256 校验失败: script_id={script_id} version={version}"
            )

        path = self._path_for(script_id, version, sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

        entry = ScriptCacheEntry(
            script_id=script_id,
            version=version,
            sha256=sha256,
            path=str(path),
        )
        inserted = self._ledger.cache_script(entry)
        if not inserted:
            # 并发下同 hash 已由其他 worker 写入：复用已存在引用。
            return (
                self._ledger.get_cached_script(script_id, version, sha256)
                or entry
            )
        return entry

    def _path_for(self, script_id: str, version: int, sha256: str) -> Path:
        """按 hash 组织目录：``{cache_dir}/{sha256}/{script_id}-v{version}.bin``。"""
        return self._cache_dir / sha256 / f"{script_id}-v{version}.bin"

    @staticmethod
    def _parse_ref(script_ref: Mapping[str, object]) -> tuple[str, int, str, str]:
        """解析并校验 script_ref；非法返回结构化错误。"""
        script_id = script_ref.get("script_id")
        version = script_ref.get("version")
        sha256 = script_ref.get("sha256")
        download_url = script_ref.get("download_url")

        if not isinstance(script_id, str) or not script_id.strip():
            raise ScriptCacheError(
                "script_ref 缺少合法 script_id", code=SCRIPT_REF_INVALID
            )
        if not isinstance(version, int) or version < 1:
            raise ScriptCacheError(
                "script_ref 缺少合法 version", code=SCRIPT_REF_INVALID
            )
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ScriptCacheError(
                "script_ref 缺少合法 sha256", code=SCRIPT_REF_INVALID
            )
        try:
            int(sha256, 16)
        except ValueError as exc:
            raise ScriptCacheError(
                "script_ref.sha256 不是合法十六进制", code=SCRIPT_REF_INVALID
            ) from exc
        if not isinstance(download_url, str) or not download_url.strip():
            raise ScriptCacheError(
                "script_ref 缺少合法 download_url", code=SCRIPT_REF_INVALID
            )
        return (
            script_id.strip(),
            version,
            sha256.lower(),
            download_url.strip(),
        )
