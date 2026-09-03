"""插件包签名下载服务（P5.5 延伸，§18.8）。

封装内部插件下载端点的签名 URL 生成与校验：派发 ``execution.plan`` 时把签名
``download_url`` 写入 ``plugin_ref``，Agent 检查本地版本、缺失时经内部
端点下载插件包并按 sha256 校验安装。

纯签名逻辑在 ``master.domain.signed_url``；本服务只负责把配置（密钥、
对外地址、有效期）与纯函数粘合，便于容器装配与测试注入。
"""

from __future__ import annotations

from aetp_protocol.ids import PluginId, SemVer

from master.domain.signed_url import (
    _PLUGIN_VERSION_URL_PATH,
    build_signed_path,
    verify_signed_path,
)


class PluginDownloadService:
    """为插件包生成/校验限时签名下载 URL。"""

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

    def build_versioned_download_url(self, plugin_id: PluginId, version: SemVer) -> str:
        """生成  指定插件版本的签名下载 URL。"""
        resource_id = f"{plugin_id.root}@{version.root}"
        path = build_signed_path(
            resource_id,
            self._secret,
            self._ttl_s,
            path_template=_PLUGIN_VERSION_URL_PATH,
            version=version.root,
            path_resource_id=plugin_id.root,
        )
        return f"{self._base_url}{path}"

    def verify(self, plugin_id: str, expires: int, signature: str) -> bool:
        """校验插件签名 URL 是否有效。"""
        return verify_signed_path(plugin_id, expires, signature, self._secret)

    def verify_version(self, plugin_id: PluginId, version: SemVer, expires: int, signature: str) -> bool:
        """校验  指定版本插件签名 URL。"""
        return verify_signed_path(
            f"{plugin_id.root}@{version.root}",
            expires,
            signature,
            self._secret,
        )
