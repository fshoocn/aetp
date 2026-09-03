""" 插件归档的跨端完整性和入口校验。"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePath

from common.zip_utils import validate_zip_names

from .ids import Sha256
from .plugins import PluginManifest


@dataclass(frozen=True)
class VerifiedPluginArchive:
    """通过完整性和 Manifest 校验的插件归档。"""

    filename: str
    sha256: Sha256
    manifest: PluginManifest
    members: tuple[str, ...]


class PluginArchiveVerifier:
    """只验证归档，不导入或执行插件代码。"""

    MAX_SIZE = 100 * 1024 * 1024
    _ENTRYPOINT_PATTERN = re.compile(r"^(?P<module>[A-Za-z_][A-Za-z0-9_.]*):[A-Za-z_][A-Za-z0-9_]*$")

    def verify(self, content: bytes, *, filename: str = "plugin.zip") -> VerifiedPluginArchive:
        if len(content) > self.MAX_SIZE:
            raise ValueError("插件包不能超过 100 MB")
        if not filename.lower().endswith(".zip"):
            raise ValueError("仅支持 ZIP 插件包（.zip）")
        if PurePath(filename).name != filename or not re.fullmatch(r"[A-Za-z0-9_.+-]+\.zip", filename):
            raise ValueError("插件文件名不合法")

        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError("插件包不是有效 ZIP") from exc

        with archive:
            infos = archive.infolist()
            names = tuple(info.filename for info in infos)
            validate_zip_names(list(names))
            if len(names) != len(set(names)):
                raise ValueError("插件包包含重复成员")
            self._validate_members(infos)
            if "plugin.json" not in names:
                raise ValueError(" 插件包必须包含根目录 plugin.json")
            manifest = self._read_manifest(archive)
            self._validate_manifest_files(manifest, set(names))

        return VerifiedPluginArchive(
            filename=filename,
            sha256=Sha256(hashlib.sha256(content).hexdigest()),
            manifest=manifest,
            members=names,
        )

    @staticmethod
    def _validate_members(infos: list[zipfile.ZipInfo]) -> None:
        for info in infos:
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError(f"插件包禁止包含符号链接: {info.filename}")

    @staticmethod
    def _read_manifest(archive: zipfile.ZipFile) -> PluginManifest:
        try:
            raw = json.loads(archive.read("plugin.json"))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, zipfile.BadZipFile) as exc:
            raise ValueError("plugin.json 不是有效 JSON") from exc
        try:
            return PluginManifest.model_validate(raw)
        except ValueError as exc:
            raise ValueError("plugin.json 不符合  PluginManifest") from exc

    def _validate_manifest_files(self, manifest: PluginManifest, names: set[str]) -> None:
        entrypoints = manifest.entrypoints
        if entrypoints.master is not None:
            self._require_python_entrypoint(entrypoints.master.root, "master", names)
        if entrypoints.agent is not None:
            self._require_python_entrypoint(entrypoints.agent.root, "agent", names)
        if entrypoints.ui is not None:
            self._require_ui_file(entrypoints.ui.root, names)
        if manifest.configuration_schema is not None:
            path = manifest.configuration_schema.root
            if path not in names or not path.startswith("schemas/"):
                raise ValueError(f"配置 Schema 必须位于 schemas/ 且存在: {path}")

    def _require_python_entrypoint(self, value: str, side: str, names: set[str]) -> None:
        match = self._ENTRYPOINT_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError(f"{side} 入口格式无效: {value}")
        module_path = match.group("module").replace(".", "/") + ".py"
        expected = f"{side}/{module_path}"
        if expected not in names:
            raise ValueError(f"{side} 入口文件不存在或越界: {expected}")

    @staticmethod
    def _require_ui_file(path: str, names: set[str]) -> None:
        if not path.startswith("ui/") or path not in names:
            raise ValueError(f"UI 入口文件不存在或越界: {path}")
