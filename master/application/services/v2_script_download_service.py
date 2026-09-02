"""V2 ScriptDefinition 源文件签名下载。"""

from __future__ import annotations

from master.domain.signed_url import build_signed_path, verify_signed_path

_V2_SCRIPT_PATH = "/api/v2/internal/scripts/{script_id}/download"


class V2ScriptDownloadService:
    def __init__(self, secret: str, *, base_url: str = "", ttl_s: int = 300) -> None:
        self._secret = secret
        self._base_url = base_url.rstrip("/")
        self._ttl_s = ttl_s

    def build_download_url(self, script_id: str) -> str:
        path = build_signed_path(
            script_id,
            self._secret,
            self._ttl_s,
            path_template=_V2_SCRIPT_PATH,
        )
        return f"{self._base_url}{path}"

    def verify(self, script_id: str, expires: int, signature: str) -> bool:
        return verify_signed_path(script_id, expires, signature, self._secret)


__all__ = ["V2ScriptDownloadService"]
