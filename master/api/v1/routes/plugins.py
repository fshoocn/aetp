"""任务类型插件清单 API。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from master.api.v1.dependencies import CurrentUser, PluginManagerDep, PluginRegistryDep
from master.api.v1.permissions import PlatformAdminDep

router = APIRouter(prefix="/task-types", tags=["v1-task-types"])


def _managed(item):
    return {"plugin_id": item.plugin_id, "filename": item.filename, "task_type": item.task_type, "version": item.version, "sha256": item.sha256, "enabled": item.enabled, "installed": item.installed}


@router.get("")
def list_task_types(_current_user: CurrentUser, registry: PluginRegistryDep) -> list[dict]:
    """返回 Master 当前已加载的受信任插件元数据，供 Web 配置页使用。"""
    return [
        {
            "task_type": package.metadata.task_type,
            "display_name": package.metadata.display_name or package.metadata.task_type,
            "plugin_version": package.metadata.plugin_version,
            "supported_versions": sorted(package.metadata.supported_versions),
            "config_schema": package.metadata.config_schema,
            "upload_spec": package.metadata.upload_spec,
            "ui": _ui_metadata(package.metadata.task_type, package.metadata.ui),
            "agent_available": package.agent is not None,
            "agent_package": (
                {
                    "package_name": package.metadata.agent_package.package_name,
                    "version": package.metadata.agent_package.version,
                    "entry_point": package.metadata.agent_package.entry_point,
                }
                if package.metadata.agent_package is not None
                else None
            ),
        }
        for package in registry.list()
    ]


def _ui_metadata(task_type: str, metadata: dict) -> dict:
    ui = dict(metadata or {})
    entry = ui.get("entry")
    if isinstance(entry, str) and entry:
        ui["url"] = f"/api/v1/task-types/{quote(task_type, safe='')}/ui/{quote(entry, safe='/')}"
    return ui


@router.get("/{task_type}/ui/{path:path}")
def plugin_ui_asset(
    task_type: str,
    path: str,
    _current_user: CurrentUser,
    manager: PluginManagerDep,
) -> FileResponse:
    """托管插件 ZIP 内的 UI 资源，不把插件页面编译进 Web 主应用。"""
    try:
        asset = manager.ui_asset(task_type, path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(asset)


@router.get("/managed")
def list_managed_plugins(_admin: PlatformAdminDep, manager: PluginManagerDep) -> list[dict]:
    return [_managed(item) for item in manager.list()]


@router.post("/managed", status_code=status.HTTP_201_CREATED)
async def upload_plugin(_admin: PlatformAdminDep, manager: PluginManagerDep, file: UploadFile = File(...)) -> dict:
    try:
        record = manager.upload(file.filename or "plugin.whl", await file.read())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _managed(record)


@router.post("/managed/{plugin_id}/install")
def install_plugin(plugin_id: str, _admin: PlatformAdminDep, manager: PluginManagerDep) -> dict:
    try:
        return _managed(manager.install(plugin_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"插件安装失败: {exc}") from exc


@router.patch("/managed/{plugin_id}")
def set_plugin_enabled(plugin_id: str, enabled: bool, _admin: PlatformAdminDep, manager: PluginManagerDep) -> dict:
    try:
        return _managed(manager.set_enabled(plugin_id, enabled))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/managed/{plugin_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plugin(plugin_id: str, _admin: PlatformAdminDep, manager: PluginManagerDep) -> None:
    try:
        manager.delete(plugin_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc