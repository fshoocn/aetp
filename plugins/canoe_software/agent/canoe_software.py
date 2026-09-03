"""CANoe software 环境发现插件 Provider。

License 状态无法在进程内可靠探测，默认从固定映射文件读取（与串口 resource 插件的
serial_ports.json 同思路）：用户可通过编辑**已安装插件**目录下
``agent/canoe_software.json`` 自定义版本与 License 可用性。文件不存在或无效时
发现为空（不报能力、不伪造）。构造时也可注入 ``discoverer``（测试用）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

from aetp_protocol.capabilities import SoftwareCapability, Version
from aetp_protocol.discovery import SoftwareDiscoveryError

DEFAULT_CANOE_MAP_FILE = "canoe_software.json"


class CanoeSoftwareProvider:
    """上报本机 CANoe 商业软件能力（版本 + License 状态）。"""

    provider_id = "org.aetp.canoe-software"
    name = "CANoe"

    def __init__(
        self,
        map_file: str | Path | None = None,
        *,
        discoverer: Callable[[], Iterable[SoftwareCapability]] | None = None,
    ) -> None:
        self._map_file = Path(map_file) if map_file is not None else None
        self._discoverer = discoverer

    def discover(self) -> tuple[SoftwareCapability, ...]:
        if self._discoverer is not None:
            return self._validate(tuple(self._discoverer()))
        path = self._map_file
        if path is None or not path.exists():
            return ()
        raw = _read_map(path)
        if raw is None:
            return ()
        return self._validate(
            (
                SoftwareCapability(
                    provider_id=self.provider_id,
                    name=self.name,
                    version=Version(str(raw["version"])),
                    properties={
                        "license_available": bool(raw.get("license_available", False)),
                        "source": "canoe_software.json",
                    },
                ),
            )
        )

    def _validate(self, discovered: tuple[SoftwareCapability, ...]) -> tuple[SoftwareCapability, ...]:
        for item in discovered:
            if item.name != self.name:
                raise SoftwareDiscoveryError(
                    f"软件名不一致: expected={self.name} actual={item.name}"
                )
            if item.provider_id != self.provider_id:
                raise SoftwareDiscoveryError(
                    f"provider_id 不一致: expected={self.provider_id} actual={item.provider_id}"
                )
        return discovered


def _read_map(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SoftwareDiscoveryError(f"CANoe 映射文件无效: {path}") from exc
    if not isinstance(raw, dict):
        raise SoftwareDiscoveryError("CANoe 映射文件必须是 JSON 对象")
    version = raw.get("version")
    if not isinstance(version, str) or not version:
        raise SoftwareDiscoveryError("CANoe 映射文件缺少非空 version")
    return raw


__all__ = ["CanoeSoftwareProvider"]
