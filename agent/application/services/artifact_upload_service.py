"""Agent Run 产物上传服务（P6.6）。

Agent 只负责把插件生成的本地文件上传到 Master 内部端点；产物引用、
哈希和项目范围存储由 Master 的 ArtifactService 负责。
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class ArtifactUploadError(RuntimeError):
    """产物上传失败。"""


class ArtifactUploadService:
    """通过 Master 内部 multipart 端点上传本地产物。"""

    async def upload(
        self,
        url: str,
        path: str | Path,
        *,
        kind: str,
        filename: str | None = None,
    ) -> dict:
        """异步上传单个文件并返回 Master 创建的 artifact 引用。"""
        return await asyncio.to_thread(
            self._upload_sync,
            url,
            Path(path),
            kind,
            filename,
        )

    @staticmethod
    def _upload_sync(
        url: str,
        path: Path,
        kind: str,
        filename: str | None,
    ) -> dict:
        if not url:
            raise ArtifactUploadError("未配置产物上传地址")
        if kind not in {"report", "log_archive", "data"}:
            raise ArtifactUploadError(f"非法产物类型: {kind}")
        if not path.is_file():
            raise ArtifactUploadError(f"产物文件不存在: {path}")

        boundary = f"----aetp-{uuid.uuid4().hex}"
        upload_name = filename or path.name
        content_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
        data = path.read_bytes()
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="file"; '
                    f'filename="{upload_name}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                data,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        parts = urlsplit(url)
        query = list(parse_qsl(parts.query, keep_blank_values=True))
        query.append(("kind", kind))
        target = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
        request = Request(
            target,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ArtifactUploadError(
                f"Master 产物上传失败: HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except (URLError, OSError, json.JSONDecodeError) as exc:
            raise ArtifactUploadError(f"Master 产物上传失败: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("artifact_id"):
            raise ArtifactUploadError("Master 返回的产物引用无效")
        return payload
