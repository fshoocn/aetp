"""脚本签名下载服务（P4.7，§7.4/§18.8）。

封装内部下载端点的签名 URL 生成与校验：派发 ``run.assign`` 时把签名
``download_url`` 写入 ``script_ref``，Agent 经内部端点下载后按 sha256 校验。

纯签名逻辑在 ``master.domain.signed_url``；本服务只负责把配置（密钥、
对外地址、有效期）与纯函数粘合，便于容器装配与测试注入。
"""

from __future__ import annotations

from master.domain.signed_url import build_signed_path, verify_signed_path


class ScriptDownloadService:
    """为脚本生成/校验限时签名下载 URL。"""

    def __init__(
        self,
        secret: str,
        *,
        base_url: str = "",
        ttl_s: int = 300,
    ) -> None:
        self._secret = secret
        self._base_url = (base_url or "").rstrip("/")
        self._ttl_s = ttl_s

    def build_path(self, script_id: str) -> str:
        """生成签名相对路径（测试与 base_url 为空时使用）。"""
        return build_signed_path(script_id, self._secret, self._ttl_s)

    def build_download_url(self, script_id: str) -> str:
        """生成完整签名下载 URL（base_url + 相对路径）。"""
        return f"{self._base_url}{self.build_path(script_id)}"

    def verify(self, script_id: str, expires: int, signature: str) -> bool:
        """校验签名 URL 是否有效。"""
        return verify_signed_path(script_id, expires, signature, self._secret)
