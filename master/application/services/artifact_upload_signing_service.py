"""Artifact 上传 URL 签名服务。"""

from __future__ import annotations

from master.domain.signed_url import build_artifact_upload_path, verify_artifact_upload_path


class ArtifactUploadSigningService:
    """为 Agent 生成和校验绑定资源范围的限时上传 URL。"""

    def __init__(self, secret: str, *, base_url: str = "", ttl_s: int = 300) -> None:
        self._secret = secret
        self._base_url = (base_url or "").rstrip("/")
        self._ttl_s = ttl_s

    def build_url(
        self,
        run_id: str,
        project_id: str,
        node_id: str,
        shard_id: str,
        attempt_id: str | None = None,
    ) -> str:
        path = build_artifact_upload_path(
            run_id,
            project_id,
            node_id,
            shard_id,
            self._secret,
            self._ttl_s,
            attempt_id=attempt_id,
        )
        return f"{self._base_url}{path}"

    def verify(
        self,
        run_id: str,
        project_id: str,
        node_id: str,
        shard_id: str,
        attempt_id: str | None,
        expires: int,
        signature: str,
    ) -> bool:
        return verify_artifact_upload_path(
            run_id,
            project_id,
            node_id,
            shard_id,
            attempt_id,
            expires,
            signature,
            self._secret,
        )


__all__ = ["ArtifactUploadSigningService"]
