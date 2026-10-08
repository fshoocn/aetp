from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cordis_port import Context

from common.cordis_utils import unload_all
from common.node_runtime import (
    NodeKind,
    assemble_node,
    ensure_installed,
    source_plugin_id,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request(base_url: str, path: str) -> tuple[int, bytes]:
    request = Request(f"{base_url}{path}")
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.read()


def _write_source(root: Path, plugin_id: str) -> Path:
    """造一个最小插件源目录（executor 风格：注册一条本节点 API）。"""
    source = root / plugin_id
    source.mkdir(parents=True)
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "1.0.0",
                "entrypoint": "plugin:DemoPlugin",
                "role": "executor",
                "requires": ["can-bus"],
            }
        ),
        encoding="utf-8",
    )
    (source / "plugin.py").write_text(
        (
            "class DemoPlugin:\n"
            f"    name = {plugin_id!r}\n"
            "    inject = ['webapi', 'node_kind']\n"
            "\n"
            "    def __init__(self, ctx, config):\n"
            f"        @ctx.webapi.get('/api/{plugin_id}', kind='api')\n"
            "        def status(request):\n"
            "            return {'active': True, 'node_kind': str(ctx.node_kind)}\n"
        ),
        encoding="utf-8",
    )
    return source


class NodeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context: Context | None = None
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.port: int = _free_port()
        self.base_url: str = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
        self.temp_dir.cleanup()

    async def test_assemble_slave_node_installs_sources_and_runs_headless(self) -> None:
        root = Path(self.temp_dir.name)
        source = _write_source(root, "executor-demo")
        self.assertEqual(source_plugin_id(source), "executor-demo")

        self.context = await assemble_node(
            kind=NodeKind.SLAVE,
            node_name="测试从节点",
            web_config={"port": self.port},
            install_root=root / "plugins",
            plugin_sources=[source],
        )

        # 节点类型注册为 node_kind 服务，插件声明 inject 后可读
        self.assertEqual(self.context.node_kind, NodeKind.SLAVE)

        # 插件源已安装到本节点安装目录并启用，其 API 可用
        self.assertTrue((root / "plugins" / "executor-demo").is_dir())
        self.assertTrue((root / "plugins" / ".archives" / "executor-demo.zip").is_file())
        status, body = _request(self.base_url, "/api/executor-demo")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["node_kind"], "slave")

        # 无头语义：没装 webui —— 页面路径 404，/api/web/ui 不存在
        self.assertEqual(_request(self.base_url, "/some/page")[0], 404)
        self.assertEqual(_request(self.base_url, "/api/web/ui")[0], 404)

        # 幂等：重复 ensure 不报错、不重复安装
        self.assertEqual(await ensure_installed(self.context, source), "executor-demo")
        plugins = await self.context.plugins.list_plugins()
        self.assertEqual([item["id"] for item in plugins], ["executor-demo"])

        # 归属元数据随安装保留（供未来的分发插件筛选）
        record = plugins[0]
        self.assertEqual(record["role"], "executor")
        self.assertEqual(record["requires"], ["can-bus"])


if __name__ == "__main__":
    unittest.main()
