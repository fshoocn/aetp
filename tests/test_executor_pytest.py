"""pytest 执行端（slave_plugins/executor_pytest）行为测试（DESIGN §5–§7/§10/§11/§16.1）。

覆盖（分组 TestCase，对齐任务清单 9 组）：

1. 参数翻译：argv 组装优先级（unit_refs → targets → selection → 兜底
   ``["."]``）、``AETP_*`` env 过滤 + warning、``shell=False`` 断言（argv 化、
   拒 shell）；recipe 是不透明字符串，全链路不解释；
2. assemble：平铺夹具工程解压即可跑、zip-slip 拒绝、路径越界拒绝、符号链接
   拒绝（安全校验不依赖 recipe）；
3. 材料接收：自描述分块乱序（含元包晚到）拼装 ready、sha 不符回 missing、
   幂等重块、selections 物化（pytest.material 元包带 selections）；
4. inspect：问题码（deps.missing warning——包内 requirements.txt 探测 /
   syntax.error / collect.error；无 requirements.txt 跳过探测）、analyze 应答
   同带用例清单（case_id/markers/params 近似，与 collect 同构）、collect 清单
   结构、request_id 幂等重发同应答；
5. 执行集成（真子进程跑 tests/fixtures/pytest_project 平铺工程）：junit 六
   status 映射与计数、succeeded 判定、产物（junit.xml/pytest.log、sha256/size、
   local:/// 盘符 uri）、组装目录清理；
6. 日志流：LogChunk seq 从 0 连续、双写一致、哨兵 case 归属、节流 / 截断；
7. 取消 / 超时：case_slow 杀进程树（无残留子进程）、canceled/timeout 且产物
   仍在、取消优先于超时；
8. 幂等：同 (job_id, attempt) 重复消息只执行一次；
9. proto 拒收：proto ≠ TEST_PROTO → ``failed: proto mismatch`` 不执行。

测试引导：IsolatedAsyncioTestCase；执行端栈 = appconfig + comm 替身（记录
report / send_to，注入收件）+ PytestExecutorPlugin（手法同 tests/test_heartbeat.py
的 _FakeComm）；执行集成跑真 pytest 子进程（venv 内同栈解释器）；杀树后以
tasklist CSV 探测残留 PID（teardown 兜底清杀防泄漏）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast
from unittest import mock

from cordis_port import Context

from common.cordis_utils import unload_all, wait_until
from common.plugins.appconfig import AppConfigPlugin
from common.test_contracts import (
    TEST_PROTO,
    TOPIC_JOB_LOG,
    TOPIC_JOB_PROGRESS,
    TOPIC_JOB_RESULT,
)
from slave_plugins.executor_pytest.assemble import (
    AssembleError,
    extract_zip,
)
from slave_plugins.executor_pytest.config import PytestExecutorConfig
from slave_plugins.executor_pytest.executor import (
    JobRequest,
    JobRunError,
    build_argv,
    build_env,
    parse_junit,
    resolve_targets,
    run_job,
    run_process,
    zero_counts,
)
from slave_plugins.executor_pytest.inspect import run_inspect
from slave_plugins.executor_pytest.plugin import (
    PROTO,
    TOPIC_INSPECT_RESULT,
    TOPIC_MATERIAL_MISSING,
    MaterialStore,
    PytestExecutorPlugin,
)
from slave_plugins.executor_pytest.stream import (
    CASE_END_MARKER,
    CASE_MARKER,
    LogStreamer,
)

#: 夹具工程（平铺包：根 conftest/pytest.ini + helpers/ + tests/case_*.py，解压即可跑）
FIXTURE_DIR: Path = Path(__file__).resolve().parent / "fixtures" / "pytest_project"

#: 夹具的 recipe（不透明字符串，pytest 全链路不解析；只验透传）
FIXTURE_RECIPE: str = ""


# ================================================================ 纯函数辅助
def _zip_bytes(files: Mapping[str, str | bytes]) -> bytes:
    """把 ``{zip 内相对路径: 内容}`` 打成 zip 字节（材料 / 测试工程造件）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in files.items():
            package.writestr(name, content)
    return buffer.getvalue()


def fixture_zip() -> bytes:
    """tests/fixtures/pytest_project 打包（排除 __pycache__）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for path in sorted(FIXTURE_DIR.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                package.write(path, path.relative_to(FIXTURE_DIR).as_posix())
    return buffer.getvalue()


def _pid_alive(pid: int) -> bool:
    """Windows 进程存活探测（tasklist CSV 形态：整字段精确匹配 PID）。"""
    try:
        out = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return f'"{pid}"' in out.stdout


def _ensure_dead(pid: int) -> None:
    """兜底清杀残留子进程（teardown 防泄漏；正常路径已被杀树覆盖）。"""
    if not _pid_alive(pid):
        return
    subprocess.run(  # noqa: S603
        ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607
        capture_output=True,
        timeout=15,
    )


class _LineCollector:
    """子进程输出收集器（LineSink 实现）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def feed(self, stream: Literal["stdout", "stderr"], raw_line: str) -> None:
        self.lines.append(raw_line.rstrip("\r\n"))


class _LogRecorder:
    """run_job 的 log 面替身：记录 warning / info / error。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []

    def _format(self, message: str, args: tuple[object, ...]) -> str:
        return message % args if args else message

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append(self._format(message, args))

    def info(self, message: str, *args: object) -> None:
        self.infos.append(self._format(message, args))

    def error(self, message: str, *args: object) -> None:
        self.errors.append(self._format(message, args))


class _FakeComm:
    """comm 通道替身（test_heartbeat._FakeComm 同手法）：记录上行、可注入收件。"""

    def __init__(self) -> None:
        self.reports: list[tuple[str, dict[str, Any]]] = []
        self.directed: list[tuple[str, str, dict[str, Any]]] = []
        self._handlers: dict[str, list[Callable[..., object]]] = {}

    def on(self, topic: str, handler: Callable[..., object]) -> Callable[[], None]:
        self._handlers.setdefault(topic, []).append(handler)

        def dispose() -> None:
            handlers = self._handlers.get(topic, [])
            if handler in handlers:
                handlers.remove(handler)

        return dispose

    def report(self, topic: str, payload: object, **_kwargs: object) -> str:
        self.reports.append((str(topic), dict(cast(Mapping[str, Any], payload))))
        return f"msg-{len(self.reports)}"

    def send_to(self, node: str, topic: str, payload: object, **_kwargs: object) -> str:
        self.directed.append(
            (str(node), str(topic), dict(cast(Mapping[str, Any], payload)))
        )
        return f"msg-{len(self.directed)}"

    def deliver(self, topic: str, payload: object) -> None:
        """模拟下行收件（调用 comm.on 登记的处理器）。"""
        for handler in list(self._handlers.get(topic, [])):
            handler(payload, {"node": "master-node"})


# ================================================================ 栈基类
class ExecutorStackTestCase(unittest.IsolatedAsyncioTestCase):
    """pytest 执行端最小栈基类：appconfig + comm 替身 + PytestExecutorPlugin。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)  # 节点目录
        self.comm = _FakeComm()
        self.context: Context | None = None
        self.plugin: Any = None
        self._stray_pids: list[int] = []

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
            self.context = None
        for pid in self._stray_pids:
            _ensure_dead(pid)
        self._temp.cleanup()

    async def boot(self, *, config: Mapping[str, object] | None = None) -> None:
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\nkind = slave\nname = slave-node\n\n[comm]\nbatch_interval = 0.05\n",
            encoding="utf-8",
        )
        context = Context()
        self.context = context
        context.provide("node_kind", "slave")
        context.provide("node_name", "slave-node")
        await context.plugin(AppConfigPlugin, {"path": str(config_path)})
        context.provide("comm", self.comm)
        await context.plugin(PytestExecutorPlugin, dict(config or {}))
        self.plugin = context.executor_pytest

    # ------------------------------------------------------------ 材料推送
    def material_messages(
        self,
        material_id: str,
        data: bytes,
        recipe: str = "",
        *,
        chunk_size: int = 512,
        selections: Sequence[Mapping[str, Any]] = (),
        sha256: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """自描述分块消息 + 元包消息（§5.1：每条 chunk 带 total / sha256）。"""
        digest = sha256 if sha256 is not None else hashlib.sha256(data).hexdigest()
        total = max(1, (len(data) + chunk_size - 1) // chunk_size)
        chunks: list[dict[str, Any]] = []
        for index in range(total):
            piece = data[index * chunk_size : (index + 1) * chunk_size]
            chunks.append(
                {
                    "proto": PROTO,
                    "material_id": material_id,
                    "sha256": digest,
                    "total": total,
                    "index": index,
                    "data_b64": base64.b64encode(piece).decode("ascii"),
                }
            )
        meta = {
            "proto": PROTO,
            "material_id": material_id,
            "sha256": digest,
            "recipe_fingerprint": "fp-test",
            "total": total,
            "chunk_size": chunk_size,
            "recipe": recipe,
            "selections": [dict(item) for item in selections],
        }
        return chunks, meta

    def push_material(
        self,
        material_id: str,
        data: bytes,
        recipe: str = "",
        *,
        selections: Sequence[Mapping[str, Any]] = (),
        with_meta: bool = True,
        order: Sequence[int] | None = None,
        chunk_size: int = 512,
        sha256: str | None = None,
    ) -> dict[str, Any]:
        """按指定块序推送材料（缺省正序 + 元包同行），返回元包消息。"""
        chunks, meta = self.material_messages(
            material_id,
            data,
            recipe,
            selections=selections,
            chunk_size=chunk_size,
            sha256=sha256,
        )
        indexes = list(order) if order is not None else list(range(len(chunks)))
        for index in indexes:
            self.plugin._on_material_chunk(chunks[index], None)
        if with_meta:
            self.plugin._on_material(meta, None)
        return meta

    # ------------------------------------------------------------ 载荷与等待
    def job_payload(
        self,
        job_id: str,
        *,
        attempt: int = 1,
        params: Mapping[str, Any] | None = None,
        artifact_dir: str | None = None,
        deadline_ts: float = 0.0,
        proto: int = TEST_PROTO,
        shard: Mapping[str, Any] | None = None,
        task_id: str = "t-1",
    ) -> dict[str, Any]:
        """test.job 下行载荷（JobDescriptor 的 dict 形态；artifact_dir 默认
        取核心实际下发的**节点本地相对路径**形态）。"""
        return {
            "proto": proto,
            "job_id": job_id,
            "task_id": task_id,
            "stage_name": "unit",
            "attempt": attempt,
            "executor": {"id": "pytest", "kind": "executor", "labels": {}},
            "params": dict(params or {}),
            "shard": dict(shard) if shard else None,
            "artifact_dir": (
                artifact_dir
                if artifact_dir is not None
                else f"data/plugins/test_core/artifacts/{task_id}/{job_id}/a{attempt}"
            ),
            "deadline_ts": deadline_ts,
        }

    def reports(self, topic: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.comm.reports if name == topic]

    def results(self) -> list[dict[str, Any]]:
        return self.reports(TOPIC_JOB_RESULT)

    async def wait_results(self, count: int, *, timeout: float = 120.0) -> list[dict[str, Any]]:
        ok = await wait_until(lambda: len(self.results()) >= count, timeout=timeout, interval=0.05)
        if not ok:
            self.fail(
                f"等待 {count} 个 test.job.result 超时，已收上报："
                f"{[name for name, _ in self.comm.reports]}"
            )
        return self.results()

    async def wait_result(self, *, timeout: float = 120.0) -> dict[str, Any]:
        rows = await self.wait_results(1, timeout=timeout)
        return rows[0]

    def artifact_path(self, artifact: Mapping[str, Any]) -> Path:
        """产物 uri（local:///D:/…）→ 本地路径。"""
        uri = str(artifact.get("uri") or "")
        self.assertTrue(uri.startswith("local:///"), f"产物 uri 形态不对：{uri}")
        return Path(uri[len("local:///") :])


# ================================================================ 1. 参数翻译
class ArgvEnvTranslationTests(unittest.TestCase):
    """argv / targets / env 翻译（§10.3 / §11.2 / §15）。"""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _selection_dir(self) -> Path:
        material_dir = self.root / "cache" / "m-1"
        (material_dir / "selections").mkdir(parents=True, exist_ok=True)
        (material_dir / "selections" / "sel-1.json").write_text(
            json.dumps(
                {"selection_id": "sel-1", "case_ids": ["cases/a.py::t1", "cases/b.py::t2"]}
            ),
            encoding="utf-8",
        )
        return material_dir

    def test_targets_priority_unit_refs_then_targets_then_selection_then_dot_fallback(self) -> None:
        material_dir = self._selection_dir()
        # 1) 非 plan 分片的 unit_refs 最优先
        self.assertEqual(
            resolve_targets(
                {"targets": ["x"], "selection_id": "sel-1"},
                {"index": 0, "total": 2, "unit_refs": ["u1", "u2"]},
                material_dir,
            ),
            ["u1", "u2"],
        )
        # 2) params.targets 压过 selection
        self.assertEqual(
            resolve_targets({"targets": ["t1"], "selection_id": "sel-1"}, None, material_dir),
            ["t1"],
        )
        # 3) selection 展开（§20-Q1 (a) 本地清单）
        self.assertEqual(
            resolve_targets({"selection_id": "sel-1"}, None, material_dir),
            ["cases/a.py::t1", "cases/b.py::t2"],
        )
        # 4) 全空兜底 ["."]（平铺工程目录直接跑）
        self.assertEqual(resolve_targets({}, None, material_dir), ["."])
        self.assertEqual(resolve_targets({}, None, None), ["."])
        # plan 分片不走 unit_refs（plan 由 _run_plan 另行收集）
        self.assertEqual(
            resolve_targets({"targets": ["t1"]}, {"plan": True, "unit_refs": ["u"]}, material_dir),
            ["t1"],
        )
        # 空 unit_refs 回落下一级
        self.assertEqual(
            resolve_targets({"targets": ["t1"]}, {"unit_refs": []}, material_dir),
            ["t1"],
        )

    def test_targets_selection_errors_still_raise(self) -> None:
        """selection_id 给定但展开失败仍抛 JobRunError（与兜底 ["."] 无关）。"""
        material_dir = self._selection_dir()
        with self.assertRaises(JobRunError):
            resolve_targets({"selection_id": "sel-1"}, None, None)  # 材料缓存缺失
        with self.assertRaises(JobRunError):
            resolve_targets({"selection_id": "sel-nope"}, None, material_dir)
        (material_dir / "selections" / "sel-empty.json").write_text(
            json.dumps({"case_ids": []}), encoding="utf-8"
        )
        with self.assertRaises(JobRunError):
            resolve_targets({"selection_id": "sel-empty"}, None, material_dir)
        (material_dir / "selections" / "sel-bad.json").write_text("{oops", encoding="utf-8")
        with self.assertRaises(JobRunError):
            resolve_targets({"selection_id": "sel-bad"}, None, material_dir)

    def test_build_argv_keyword_precedence(self) -> None:
        config = PytestExecutorConfig()
        junit = self.root / "junit.xml"
        argv = build_argv(
            "python-x",
            ["tests/case_basic.py"],
            {"extra_args": ["-x"], "keyword": "k9"},
            config,
            junit,
        )
        self.assertEqual(
            argv,
            [
                "python-x", "-m", "pytest", "tests/case_basic.py",
                "-p", "no:cacheprovider",  # 默认参数
                "-x",  # extra_args
                "--junitxml", str(junit), "-p", "aetp_marker",
            ],
        )
        # extra_args 缺席时 keyword 折算 -k
        argv = build_argv("python-x", ["tests"], {"keyword": "k9"}, config, junit)
        self.assertIn("-k", argv)
        self.assertEqual(argv[argv.index("-k") + 1], "k9")
        # 并存时 extra_args 优先（§11.2）
        argv = build_argv("python-x", ["tests"], {"keyword": "k9", "extra_args": ["-v"]}, config, junit)
        self.assertNotIn("-k", argv)
        self.assertIn("-v", argv)

    def test_build_env_filters_aetp_prefixed_keys_with_drop_list(self) -> None:
        env, dropped = build_env(
            {"PATH": "p", "PYTHONPATH": "existing"},
            {"AETP_P": "2", "P_K": "p"},
            marker_dir=Path("M"),
        )
        self.assertEqual(dropped, ["AETP_P"])  # 回传给调用方逐个告警
        self.assertNotIn("AETP_P", env)
        self.assertEqual(env["P_K"], "p")
        self.assertEqual(env["PYTHONPATH"], f"M{os.pathsep}existing")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual(env["PATH"], "p")
        # 非对象 env 静默忽略
        env2, dropped2 = build_env({}, "junk")
        self.assertEqual(dropped2, [])
        self.assertIn("PYTHONPATH", env2)

    def test_shell_false_argv_rejects_shell_metacharacters(self) -> None:
        """D7：argv 化、shell=False——元字符按字面传参，无 shell 解释副作用。"""
        async def scenario() -> tuple[list[str], list[str]]:
            real_exec = asyncio.create_subprocess_exec
            captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            collector = _LineCollector()

            async def spy(*args: Any, **kwargs: Any) -> Any:
                captured.append((args, dict(cast(Mapping[str, Any], kwargs))))
                return await real_exec(*args, **kwargs)

            with (
                mock.patch("asyncio.create_subprocess_exec", side_effect=spy),
                mock.patch(
                    "asyncio.create_subprocess_shell",
                    side_effect=AssertionError("必须 argv 化，禁止 shell"),
                ),
            ):
                outcome = await run_process(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print(sys.argv[1])",
                        "a; echo pwned > pwned.txt",
                    ],
                    cwd=self.root,
                    env=dict(os.environ),
                    timeout_s=60.0,
                    streamer=collector,
                )
            self.assertEqual(outcome.exit_code, 0)
            return collector.lines, [str(item) for item in captured[0][0]]

        lines, argv = asyncio.run(scenario())
        self.assertIn("a; echo pwned > pwned.txt", lines)  # 字面输出、未被 shell 解释
        self.assertFalse((self.root / "pwned.txt").exists())
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("a; echo pwned > pwned.txt", argv)


# ================================================================ 1b. run_job 告警
class RunJobWarningTests(unittest.IsolatedAsyncioTestCase):
    """run_job 直调：AETP_* env 过滤的 warning 证据（§11.2 / §15 纵深）。"""

    async def test_run_job_logs_aetp_env_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / "proj"
            project_dir.mkdir()
            (project_dir / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
            artifact_dir = root / "art"
            artifact_dir.mkdir()
            recorder = _LogRecorder()
            chunks: list[dict[str, Any]] = []
            request = JobRequest(
                job_id="j-smoke",
                attempt=1,
                params={
                    "targets": ["test_ok.py"],
                    "env": {"AETP_SECRET": "x", "HONEST": "1"},
                },
                shard=None,
                config=PytestExecutorConfig(),
                zip_path=None,
                project_dir=project_dir,
                artifact_dir=artifact_dir,
                material_dir=None,
                deadline_ts=0.0,
            )
            result = await run_job(
                request,
                cancel_event=asyncio.Event(),
                send_chunk=lambda chunk: chunks.append(dict(chunk)),
                log=recorder,
            )
            self.assertEqual(result.status, "succeeded")
            self.assertTrue(
                any("AETP_SECRET" in message for message in recorder.warnings),
                f"期望拒收告警，实得：{recorder.warnings}",
            )


# ================================================================ 2. 组装
class AssembleTests(unittest.TestCase):
    """assemble：安全解压（zip-slip / 越界 / 符号链接拒绝）+ 平铺工程即解即跑。"""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_extract_flat_fixture_project_directly_runnable(self) -> None:
        """平铺夹具解压即收集可跑（无任何配方 / 铺排）。"""
        zpath = self.root / "bundle.zip"
        zpath.write_bytes(fixture_zip())
        project = self.root / "proj"
        extract_zip(zpath, project)
        self.assertTrue((project / "tests" / "case_basic.py").is_file())
        self.assertTrue((project / "conftest.py").is_file())
        self.assertTrue((project / "pytest.ini").is_file())
        self.assertTrue((project / "requirements.txt").is_file())
        self.assertTrue((project / "helpers" / "util.py").is_file())
        outcome = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "."],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(outcome.returncode, 0, outcome.stdout + outcome.stderr)
        self.assertIn("13 tests collected", outcome.stdout)

    def test_extract_rejects_zip_slip(self) -> None:
        for name in ("../evil.txt", "..\\evil.txt", "/abs/evil.txt", "C:/evil.txt", "ok/../../evil.txt"):
            with self.subTest(name=name):
                zpath = self.root / "slip.zip"
                zpath.write_bytes(_zip_bytes({name: "x"}))
                dest = self.root / "out"
                with self.assertRaises(AssembleError):
                    extract_zip(zpath, dest)
                self.assertFalse((self.root / "evil.txt").exists())
                self.assertFalse((self.root / "out" / "evil.txt").exists())

    def test_extract_rejects_symlink_entry(self) -> None:
        zpath = self.root / "link.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as package:
            info = zipfile.ZipInfo("link.txt")
            info.external_attr = 0o120777 << 16  # S_IFLNK
            package.writestr(info, "target")
        zpath.write_bytes(buffer.getvalue())
        with self.assertRaises(AssembleError):
            extract_zip(zpath, self.root / "out2")

    def test_extract_rejects_corrupt_and_missing_zip(self) -> None:
        bad = self.root / "bad.zip"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(AssembleError):
            extract_zip(bad, self.root / "out3")
        with self.assertRaises(AssembleError):
            extract_zip(self.root / "nope.zip", self.root / "out4")

    def test_extract_rejects_corrupt_zip_with_malicious_names(self) -> None:
        """恶意手工 zip（越界 / 绝对路径 / 盘符）逐条拒绝，落点不越出工程目录。"""
        for name in ("ok/../../evil.txt", "..\\..\\evil.txt", "/etc/evil.txt", "C:\\evil.txt"):
            with self.subTest(name=name):
                zpath = self.root / "evil.zip"
                zpath.write_bytes(_zip_bytes({name: "x"}))
                dest = self.root / "out-evil"
                with self.assertRaises(AssembleError):
                    extract_zip(zpath, dest)
                self.assertFalse((self.root / "evil.txt").exists())


# ================================================================ 3. 材料接收
class MaterialReceiveTests(ExecutorStackTestCase):
    """pytest.material(.chunk)：自描述乱序 / sha 复核 / 幂等 / selections 物化（§5）。"""

    async def test_out_of_order_chunks_with_late_meta_assemble_ready(self) -> None:
        await self.boot()
        data = _zip_bytes({"hello.txt": "hi" * 200})
        chunks, meta = self.material_messages("m-recv-1", data, "canary-recipe", chunk_size=64)
        self.assertGreaterEqual(len(chunks), 2)
        # 分块自描述：乱序到达（先 index 1 后 index 0）不需要元包先行
        self.plugin._on_material_chunk(chunks[1], None)
        self.plugin._on_material_chunk(chunks[0], None)
        for chunk in chunks[2:]:
            self.plugin._on_material_chunk(chunk, None)
        self.assertFalse(self.plugin._materials.is_ready("m-recv-1"))  # 元包未到
        self.plugin._on_material(meta, None)  # 元包晚到
        self.assertTrue(self.plugin._materials.is_ready("m-recv-1"))
        # recipe 是不透明字符串：原样透传落盘、不解析
        self.assertEqual(self.plugin._materials.recipe_of("m-recv-1"), "canary-recipe")
        ready_file = self.plugin._materials.ready_dir("m-recv-1") / "hello.txt"
        self.assertEqual(ready_file.read_text(encoding="utf-8"), "hi" * 200)

    async def test_sha_mismatch_reports_missing_then_retry_assembles(self) -> None:
        await self.boot()
        data = _zip_bytes({"hello.txt": "content"})
        chunks, meta = self.material_messages("m-recv-2", data, sha256="0" * 64)
        self.plugin._on_material(meta, None)
        for chunk in chunks:
            self.plugin._on_material_chunk(chunk, None)
        # sha 不符 → 删分片回 missing（兜底补发请求，§5.2）
        missing = self.reports(TOPIC_MATERIAL_MISSING)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["proto"], PROTO)
        self.assertEqual(missing[0]["material_id"], "m-recv-2")
        self.assertEqual(missing[0]["missing"], list(range(len(chunks))))
        self.assertFalse(self.plugin._materials.is_ready("m-recv-2"))
        # 重拉一轮（正确 sha）→ 拼装就绪
        self.push_material("m-recv-2", data)
        self.assertTrue(self.plugin._materials.is_ready("m-recv-2"))

    async def test_meta_late_after_assembly_still_becomes_ready(self) -> None:
        await self.boot()
        data = _zip_bytes({"hello.txt": "late"})
        self.push_material("m-recv-3", data, with_meta=False)
        self.assertFalse(self.plugin._materials.is_ready("m-recv-3"))
        _chunks, meta = self.material_messages("m-recv-3", data)
        self.plugin._on_material(meta, None)  # 元包晚到
        self.assertTrue(self.plugin._materials.is_ready("m-recv-3"))

    async def test_duplicate_chunks_are_idempotent(self) -> None:
        await self.boot()
        data = _zip_bytes({"hello.txt": "dup"})
        chunks, meta = self.material_messages("m-recv-4", data, chunk_size=64)
        self.plugin._on_material_chunk(chunks[0], None)
        self.plugin._on_material_chunk(chunks[0], None)  # 重复块
        for chunk in chunks[1:]:
            self.plugin._on_material_chunk(chunk, None)
        self.plugin._on_material_chunk(chunks[-1], None)  # ready 后重复块直接跳过
        self.plugin._on_material(meta, None)
        self.assertTrue(self.plugin._materials.is_ready("m-recv-4"))
        content = (self.plugin._materials.ready_dir("m-recv-4") / "hello.txt").read_text(encoding="utf-8")
        self.assertEqual(content, "dup")  # 幂等落盘、内容不被重写污染

    async def test_meta_materializes_selections(self) -> None:
        await self.boot()
        data = _zip_bytes({"hello.txt": "sel"})
        selections = [{"selection_id": "sel-1", "case_ids": ["cases/a.py::t1", "cases/b.py::t2"]}]
        self.push_material("m-sel-1", data, selections=selections)
        sel_path = self.plugin._materials.ready_dir("m-sel-1") / "selections" / "sel-1.json"
        self.assertTrue(sel_path.is_file())
        self.assertEqual(
            json.loads(sel_path.read_text(encoding="utf-8")),
            {"selection_id": "sel-1", "case_ids": ["cases/a.py::t1", "cases/b.py::t2"]},
        )
        # 元包晚到（先拼装后元包）同样物化
        self.push_material("m-sel-2", data, with_meta=False, selections=selections)
        _chunks, meta = self.material_messages("m-sel-2", data, selections=selections)
        self.plugin._on_material(meta, None)
        self.assertTrue(
            (self.plugin._materials.ready_dir("m-sel-2") / "selections" / "sel-1.json").is_file()
        )

    def test_store_chunk_outcomes_and_gave_up(self) -> None:
        """MaterialStore 分块状态机：stored / duplicate / mismatch / gave-up / assembled。"""
        store = MaterialStore(self.root / "cache")
        data = _zip_bytes({"hello.txt": "x"})
        half = len(data) // 2
        part0, part1 = data[:half], data[half:]
        good = hashlib.sha256(data).hexdigest()
        store.set_meta(
            "m-store",
            sha256=good,
            recipe_fingerprint="fp",
            total=2,
            chunk_size=half,
            recipe="opaque",
            selections=[],
        )
        self.assertEqual(store.add_chunk("m-store", 1, 2, good, part1), "stored")
        self.assertEqual(store.add_chunk("m-store", 1, 2, good, part1), "duplicate")
        self.assertEqual(store.add_chunk("m-store", 0, 2, "bad", part0), "mismatch")
        self.assertEqual(store.missing("m-store", 2), [0, 1])
        self.assertEqual(store.add_chunk("m-store", 0, 2, good, part0), "stored")
        self.assertEqual(store.add_chunk("m-store", 1, 2, good, part1), "assembled")
        self.assertTrue(store.is_ready("m-store"))
        self.assertEqual(store.add_chunk("m-store", 0, 2, good, part0), "duplicate")  # ready 后幂等
        # 两轮 sha 失败 → gave-up（重拉轮次封顶）
        store.set_meta("m-bad", sha256="bad", recipe_fingerprint="fp", total=1, chunk_size=1, recipe="", selections=[])
        self.assertEqual(store.add_chunk("m-bad", 0, 1, "bad", b"X"), "mismatch")
        self.assertEqual(store.add_chunk("m-bad", 0, 1, "bad", b"X"), "gave-up")
        self.assertFalse(store.is_ready("m-bad"))


# ================================================================ 4. inspect
class InspectTests(ExecutorStackTestCase):
    """inspect：五项问题码 / collect 清单结构 / request_id 幂等（§6 / §7）。"""

    async def test_analyze_on_fixture_flags_only_missing_dep_warning(self) -> None:
        await self.boot()
        zpath = self.root / "bundle.zip"
        zpath.write_bytes(fixture_zip())
        outcome = await run_inspect(
            "analyze",
            zip_path=zpath,
            temp_dir=self.root / "work" / "inspect-deps",
            config=PytestExecutorConfig(),
        )
        problems = cast(list[dict[str, Any]], outcome["problems"])
        codes = [(str(p["code"]), str(p["severity"])) for p in problems]
        self.assertIn(("deps.missing", "warning"), codes)  # 夹具 requirements.txt 故意声明缺失依赖
        self.assertEqual([code for code, severity in codes if severity == "error"], [])
        self.assertTrue(outcome["ok"])  # warning 不阻塞
        dep = next(p for p in problems if p["code"] == "deps.missing")
        self.assertIn("aetp-missing-probe-dep", str(dep["message"]))
        self.assertEqual(dep["path"], "requirements.txt")
        # analyze 应答同带用例清单（fixtures 平铺包）：含 markers / params
        cases = cast(list[dict[str, Any]], outcome["cases"])
        self.assertEqual(len(cases), 13)  # 与 collect 清单同构：6 基础 + 5 参数化 + 2 慢用例
        by_id = {str(item["case_id"]): item for item in cases}
        self.assertEqual(by_id["tests/case_basic.py::test_skip"]["markers"], ["skip"])
        self.assertEqual(by_id["tests/case_params.py::test_tagged[t-smoke]"]["params"], {"id": "t-smoke"})

    async def test_analyze_skips_deps_probe_without_requirements_file(self) -> None:
        """包内无 requirements.txt：依赖探测跳过，不报 deps.missing。"""
        await self.boot()
        zpath = self.root / "no-req.zip"
        zpath.write_bytes(_zip_bytes({"tests/test_x.py": "def test_x():\n    pass\n"}))
        outcome = await run_inspect(
            "analyze",
            zip_path=zpath,
            temp_dir=self.root / "work" / "inspect-no-req",
            config=PytestExecutorConfig(),
        )
        problems = cast(list[dict[str, Any]], outcome["problems"])
        self.assertNotIn("deps.missing", [str(p["code"]) for p in problems])
        self.assertTrue(outcome["ok"])

    async def test_analyze_flags_syntax_error(self) -> None:
        await self.boot()
        zpath = self.root / "bad-syntax.zip"
        zpath.write_bytes(_zip_bytes({"bad.py": "def broken(:\n    pass\n"}))
        outcome = await run_inspect(
            "analyze",
            zip_path=zpath,
            temp_dir=self.root / "work" / "inspect-syntax",
            config=PytestExecutorConfig(),
        )
        self.assertFalse(outcome["ok"])
        problems = cast(list[dict[str, Any]], outcome["problems"])
        syntax = [p for p in problems if p["code"] == "syntax.error"]
        self.assertTrue(syntax)
        self.assertEqual(syntax[0]["path"], "bad.py")
        self.assertGreaterEqual(int(syntax[0]["line"]), 1)
        self.assertEqual(syntax[0]["severity"], "error")
        self.assertEqual(outcome["cases"], [])  # ok=false（编译失败）；bad.py 非用例文件，清单为空

    async def test_analyze_flags_collect_error(self) -> None:
        await self.boot()
        zpath = self.root / "bad-collect.zip"
        zpath.write_bytes(
            _zip_bytes(
                {
                    "conftest.py": "raise RuntimeError('boom')\n",
                    "cases/test_x.py": "def test_x():\n    pass\n",
                }
            )
        )
        outcome = await run_inspect(
            "analyze",
            zip_path=zpath,
            temp_dir=self.root / "work" / "inspect-collect",
            config=PytestExecutorConfig(),
        )
        self.assertFalse(outcome["ok"])
        codes = [str(p["code"]) for p in cast(list[dict[str, Any]], outcome["problems"])]
        self.assertIn("collect.error", codes)
        self.assertEqual(outcome["cases"], [])  # 可收集失败 → 不给清单

    async def test_collect_exports_structured_manifest(self) -> None:
        await self.boot()
        zpath = self.root / "bundle.zip"
        zpath.write_bytes(fixture_zip())
        outcome = await run_inspect(
            "collect",
            zip_path=zpath,
            temp_dir=self.root / "work" / "inspect-collect-ok",
            config=PytestExecutorConfig(),
        )
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["problems"], [])
        cases = cast(list[dict[str, Any]], outcome["cases"])
        by_id = {str(item["case_id"]): item for item in cases}
        self.assertEqual(len(cases), 13)  # 6 基础 + 5 参数化 + 2 慢用例
        self.assertEqual(by_id["tests/case_basic.py::test_pass"]["path"], "tests/case_basic.py")
        self.assertEqual(by_id["tests/case_basic.py::test_skip"]["markers"], ["skip"])
        self.assertEqual(by_id["tests/case_params.py::test_tagged[t-smoke]"]["params"], {"id": "t-smoke"})
        self.assertEqual(by_id["tests/case_params.py::test_square[1-1]"]["name"], "test_square[1-1]")
        self.assertEqual(by_id["tests/case_params.py::test_square[1-1]"]["markers"], [])  # parametrize 隐藏
        self.assertEqual(by_id["tests/case_slow.py::test_sleep"]["params"], {})

    async def test_request_id_idempotent_resends_same_response(self) -> None:
        await self.boot()
        self.push_material("m-insp-1", fixture_zip(), FIXTURE_RECIPE)
        request = {"proto": PROTO, "request_id": "r-1", "material_id": "m-insp-1", "mode": "collect"}
        with mock.patch(
            "slave_plugins.executor_pytest.plugin.run_inspect", wraps=run_inspect
        ) as spy:
            self.plugin._on_inspect(dict(request), None)
            self.plugin._on_inspect(dict(request), None)  # 在途重复请求
            results = await self._wait_inspect(2)
            self.assertEqual(spy.call_count, 1)  # 幂等：只执行一次
            self.assertEqual(results[0], results[1])  # 重发同一应答（含 duration_s）
            # 完成后重复请求走缓存重发
            self.plugin._on_inspect(dict(request), None)
            results = await self._wait_inspect(3)
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(results[2], results[0])

    async def test_inspect_missing_material_reports_problem(self) -> None:
        await self.boot(config={"material_wait_s": 0.1})
        self.plugin._on_inspect(
            {"proto": PROTO, "request_id": "r-m", "material_id": "m-none", "mode": "analyze"}, None
        )
        results = await self._wait_inspect(1)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["problems"][0]["code"], "material.missing")
        self.assertEqual(results[0]["request_id"], "r-m")

    async def _wait_inspect(self, count: int, *, timeout: float = 60.0) -> list[dict[str, Any]]:
        ok = await wait_until(
            lambda: len(self.reports(TOPIC_INSPECT_RESULT)) >= count,
            timeout=timeout,
            interval=0.05,
        )
        if not ok:
            self.fail(f"等待 {count} 个 inspect 应答超时：{self.reports(TOPIC_INSPECT_RESULT)}")
        return self.reports(TOPIC_INSPECT_RESULT)


# ================================================================ 5. 执行集成
class JunitParseTests(unittest.TestCase):
    """parse_junit：六种 status 映射与计数（合成 XML 对齐 pytest 8.x 实测形态）。"""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write(self, xml: str) -> Path:
        path = self.root / "junit.xml"
        path.write_text(xml, encoding="utf-8")
        return path

    def test_six_status_mapping_and_counts(self) -> None:
        xml = (
            '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
            '<testcase classname="cases.a" name="t_pass" time="0.1"/>'
            '<testcase classname="cases.a" name="t_fail" time="0.2">'
            '<failure message="assert 1 == 2">trace</failure></testcase>'
            '<testcase classname="cases.a" name="t_skip" time="0">'
            '<skipped type="pytest.skip" message="fixture skip">s</skipped></testcase>'
            '<testcase classname="cases.a" name="t_xfail" time="0">'
            '<skipped type="pytest.xfail" message="known bug"/></testcase>'
            '<testcase classname="cases.a" name="t_xpass" time="0">'
            '<failure message="[XPASS(strict)] strict">strict</failure></testcase>'
            '<testcase classname="cases.a" name="t_error" time="0">'
            '<error message="boom">trace</error></testcase>'
            "</testsuite></testsuites>"
        )
        nodeids = [
            "cases/a.py::t_pass",
            "cases/a.py::t_fail",
            "cases/a.py::t_skip",
            "cases/a.py::t_xfail",
            "cases/a.py::t_xpass",
            "cases/a.py::t_error",
        ]
        cases, counts = parse_junit(self._write(xml), nodeids)
        self.assertEqual([case.status for case in cases], ["passed", "failed", "skipped", "xfailed", "xpassed", "error"])
        self.assertEqual([case.case_id for case in cases], nodeids)  # marker 顺序对齐
        self.assertEqual(
            counts,
            {"total": 6, "passed": 1, "failed": 1, "errors": 1, "skipped": 1, "xfailed": 1, "xpassed": 1},
        )
        self.assertIn("assert 1 == 2", cases[1].message)

    def test_fallback_case_id_and_broken_junit(self) -> None:
        xml = (
            '<?xml version="1.0"?><testsuites><testsuite>'
            '<testcase classname="cases.a" name="t_only" time="0"/>'
            "</testsuite></testsuites>"
        )
        cases, counts = parse_junit(self._write(xml), [])
        self.assertEqual(cases[0].case_id, "cases.a::t_only")  # 无 nodeid 回退 classname::name
        self.assertEqual(counts["total"], 1)
        # 坏 / 缺 XML → 空结果不抛
        self.assertEqual(parse_junit(self.root / "nope.xml", []), ([], zero_counts()))
        self._write("not xml at all")
        self.assertEqual(parse_junit(self.root / "junit.xml", []), ([], zero_counts()))

    def test_message_truncated_at_2kib(self) -> None:
        xml = (
            '<?xml version="1.0"?><testsuites><testsuite>'
            '<testcase classname="c" name="t" time="0">'
            f'<failure message="{"x" * 5000}">t</failure></testcase>'
            "</testsuite></testsuites>"
        )
        cases, _counts = parse_junit(self._write(xml), ["c::t"])
        self.assertLessEqual(len(cases[0].message.encode("utf-8")), 2048)


class ExecutionIntegrationTests(ExecutorStackTestCase):
    """真子执行：组装 fixtures 工程 → pytest 子进程 → junit / 产物 / 清理（§10.3）。"""

    async def test_junit_status_counts_and_succeeded_determination(self) -> None:
        await self.boot()
        self.push_material("m-run-1", fixture_zip(), FIXTURE_RECIPE)
        self.plugin._on_job(
            self.job_payload(
                "j-full",
                params={"material_id": "m-run-1", "targets": ["tests/case_basic.py", "tests/case_params.py"]},
            ),
            None,
        )
        self.plugin._on_job(
            self.job_payload(
                "j-green",
                params={"material_id": "m-run-1", "targets": ["tests/case_params.py"]},
            ),
            None,
        )
        results = await self.wait_results(2)
        full = next(row for row in results if row["job_id"] == "j-full")
        green = next(row for row in results if row["job_id"] == "j-green")
        # 六 status 映射（真实 run 覆盖 5 种，error 见 JunitParseTests 合成件）
        self.assertEqual(full["status"], "failed")  # 有用例失败
        self.assertEqual(
            full["summary"],
            {"total": 11, "passed": 7, "failed": 1, "errors": 0, "skipped": 1, "xfailed": 1, "xpassed": 1},
        )
        status_map = {str(c["case_id"]): str(c["status"]) for c in cast(list[Mapping[str, Any]], full["cases"])}
        self.assertEqual(status_map["tests/case_basic.py::test_pass"], "passed")
        self.assertEqual(status_map["tests/case_basic.py::test_fail"], "failed")
        self.assertEqual(status_map["tests/case_basic.py::test_skip"], "skipped")
        self.assertEqual(status_map["tests/case_basic.py::test_xfail"], "xfailed")
        self.assertEqual(status_map["tests/case_basic.py::test_xpass"], "passed")  # 非严格 xpass 归 passed
        self.assertEqual(status_map["tests/case_basic.py::test_xpass_strict"], "xpassed")
        self.assertEqual(status_map["tests/case_params.py::test_square[1-1]"], "passed")
        failed_case = next(c for c in cast(list[Mapping[str, Any]], full["cases"]) if c["case_id"] == "tests/case_basic.py::test_fail")
        self.assertIn("assert 1 == 2", str(failed_case["message"]))
        # succeeded 判定：failed+errors==0 且非取消/超时
        self.assertEqual(green["status"], "succeeded")
        self.assertEqual(green["error"], "")
        self.assertEqual(green["summary"]["passed"], 5)

    async def test_targets_fallback_dot_runs_whole_project(self) -> None:
        """params 无 targets / selection_id / unit_refs：兜底 ``["."]`` 跑整个工程。"""
        await self.boot()
        self.push_material(
            "m-dot",
            _zip_bytes({"tests/test_one.py": "def test_one():\n    assert True\n"}),
        )
        self.plugin._on_job(self.job_payload("j-dot", params={"material_id": "m-dot"}), None)
        result = await self.wait_result()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["summary"]["passed"], 1)

    async def test_artifacts_with_digest_uri_and_project_cleanup(self) -> None:
        await self.boot()
        self.push_material("m-run-2", fixture_zip(), FIXTURE_RECIPE)
        self.plugin._on_job(
            self.job_payload(
                "j-art",
                params={"material_id": "m-run-2", "targets": ["tests/case_params.py"]},
            ),
            None,
        )
        result = await self.wait_result()
        artifacts = {str(a["name"]): a for a in cast(list[Mapping[str, Any]], result["artifacts"])}
        self.assertEqual(set(artifacts), {"junit.xml", "pytest.log"})
        self.assertEqual(artifacts["junit.xml"]["kind"], "report")
        self.assertEqual(artifacts["pytest.log"]["kind"], "log")
        self.assertEqual(artifacts["junit.xml"]["artifact_id"], "j-art-a1-report")
        self.assertEqual(artifacts["pytest.log"]["artifact_id"], "j-art-a1-log")
        for name, artifact in artifacts.items():
            path = self.artifact_path(artifact)
            self.assertTrue(path.is_file(), name)
            data = path.read_bytes()
            self.assertEqual(artifact["sha256"], hashlib.sha256(data).hexdigest(), name)
            self.assertEqual(artifact["size"], len(data), name)
            self.assertRegex(str(artifact["uri"]), r"^local:///[A-Za-z]:/")  # 盘符形态
            self.assertTrue(path.is_relative_to(self.root.resolve()), f"{name} 产物应落节点目录内")
        # 组装工程目录随 result 上报清理（§3.2）
        self.assertFalse((self.root / "workdir" / "j-art" / "a1").exists())
        # 相对 artifact_dir 锚定节点目录（§8.4：不是进程 cwd）
        self.assertTrue(
            (self.root / "data" / "plugins" / "test_core" / "artifacts" / "t-1" / "j-art" / "a1").is_dir()
        )

    async def test_cancel_kills_process_tree_and_keeps_artifacts(self) -> None:
        await self.boot()
        self.push_material("m-slow", fixture_zip(), FIXTURE_RECIPE)
        self.plugin._on_job(
            self.job_payload(
                "j-cancel",
                params={"material_id": "m-slow", "targets": ["tests/case_slow.py::test_sleep_with_child"]},
                deadline_ts=time.time() + 30.0,
            ),
            None,
        )
        child_file = self.root / "workdir" / "j-cancel" / "a1" / "child_pid.txt"
        ok = await wait_until(lambda: child_file.is_file(), timeout=60.0, interval=0.1)
        self.assertTrue(ok, "慢用例子进程未落 child_pid.txt")
        pid = int(child_file.read_text(encoding="utf-8").strip())
        self._stray_pids.append(pid)
        self.assertTrue(_pid_alive(pid))
        self.plugin._on_job_cancel({"job_id": "j-cancel", "attempt": 1, "reason": "测试取消"}, None)
        result = await self.wait_result()
        self.assertEqual(result["status"], "canceled")
        self.assertEqual(result["error"], "已取消")
        # D3 杀进程树：multiprocessing 子进程不残留
        ok = await wait_until(lambda: not _pid_alive(pid), timeout=15.0, interval=0.2)
        self.assertTrue(ok, f"子进程 {pid} 未被杀树清理")
        # 取消优先于超时（deadline 30s 未到即 canceled）；已产生产物仍上报
        self.assertIn("pytest.log", {str(a["name"]) for a in cast(list[Mapping[str, Any]], result["artifacts"])})

    async def test_timeout_reports_and_keeps_artifacts(self) -> None:
        await self.boot()
        self.push_material("m-slow-2", fixture_zip(), FIXTURE_RECIPE)
        self.plugin._on_job(
            self.job_payload(
                "j-timeout",
                params={"material_id": "m-slow-2", "targets": ["tests/case_slow.py::test_sleep"]},
                deadline_ts=time.time() + 5.0,
            ),
            None,
        )
        result = await self.wait_result()
        self.assertEqual(result["status"], "timeout")
        self.assertIn("pytest.log", {str(a["name"]) for a in cast(list[Mapping[str, Any]], result["artifacts"])})

    async def test_cancel_precedence_over_timeout(self) -> None:
        """取消优先于超时：deadline 尚有余量但取消先到 → canceled（并列时也归取消）。"""
        await self.boot()
        self.push_material("m-slow-3", fixture_zip(), FIXTURE_RECIPE)
        self.plugin._on_job(
            self.job_payload(
                "j-pri",
                params={"material_id": "m-slow-3", "targets": ["tests/case_slow.py::test_sleep"]},
                deadline_ts=time.time() + 20.0,
            ),
            None,
        )
        ok = await wait_until(
            lambda: any(p.get("event") == "started" for p in self.reports(TOPIC_JOB_PROGRESS)),
            timeout=30.0,
        )
        self.assertTrue(ok)
        self.plugin._on_job_cancel({"job_id": "j-pri", "attempt": 1}, None)
        result = await self.wait_result()
        self.assertEqual(result["status"], "canceled")

    async def test_cancel_event_wins_tie_against_timeout(self) -> None:
        """run_process 并列口径：取消事件先于超时触发 → canceled 而非 timeout。"""
        cancel_event = asyncio.Event()
        cancel_event.set()  # 进程启动前即取消
        outcome = await run_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=self.root,
            env=dict(os.environ),
            timeout_s=5.0,
            streamer=None,
            cancel_event=cancel_event,
        )
        self.assertTrue(outcome.canceled)
        self.assertFalse(outcome.timed_out)


# ================================================================ 6. 日志流
class LogStreamTests(ExecutorStackTestCase):
    """LogStreamer：seq 连续 / 双写一致 / 哨兵 case 归属 / 节流 / 截断（§10.4）。"""

    def _streamer(self, **options: object) -> tuple[LogStreamer, list[dict[str, Any]], Path]:
        chunks: list[dict[str, Any]] = []
        log_path = self.root / "pytest.log"
        streamer = LogStreamer(
            job_id="j-log",
            attempt=1,
            log_path=log_path,
            send_chunk=lambda chunk: chunks.append(dict(chunk)),
            **cast(dict[str, Any], options),
        )
        return streamer, chunks, log_path

    async def test_seq_from_zero_case_attribution_and_double_write(self) -> None:
        streamer, chunks, log_path = self._streamer(batch_interval=10.0)
        streamer.feed("stdout", f"{CASE_MARKER}cases/a.py::t1\x00")
        streamer.feed("stdout", "hello")
        streamer.feed("stderr", "warn")
        streamer.feed("stdout", f"{CASE_END_MARKER}cases/a.py::t1\x00")
        streamer.feed("stdout", "world")
        await streamer.close()
        self.assertEqual([chunk["seq"] for chunk in chunks], [0, 1, 2])  # 从 0 连续
        self.assertEqual(chunks[0]["case_id"], "cases/a.py::t1")  # 哨兵归属
        self.assertEqual(chunks[1]["case_id"], "cases/a.py::t1")
        self.assertEqual(chunks[2]["case_id"], "")
        self.assertEqual([line["msg"] for line in chunks[0]["lines"]], ["hello"])
        self.assertEqual([line["msg"] for line in chunks[1]["lines"]], ["warn"])
        self.assertEqual(chunks[0]["lines"][0]["level"], 20)  # stdout=INFO
        self.assertEqual(chunks[1]["lines"][0]["level"], 30)  # stderr=WARNING
        self.assertEqual(streamer.case_ids_seen, ["cases/a.py::t1"])
        # 双写一致：完整件含全部行 + 可读 [case] 边界；哨兵不入流
        text = log_path.read_text(encoding="utf-8")
        for fragment in ("[stdout] hello", "[stderr] warn", "[stdout] world", "[case] cases/a.py::t1", "[case end] cases/a.py::t1"):
            self.assertIn(fragment, text)
        self.assertNotIn("\x00", text)  # NUL 哨兵只用于流控、不落完整件

    async def test_line_truncation(self) -> None:
        streamer, chunks, _log_path = self._streamer(batch_interval=10.0, line_max_bytes=64)
        streamer.feed("stdout", "x" * 500)
        await streamer.close()
        message = str(chunks[0]["lines"][0]["msg"])
        self.assertTrue(message.endswith("…[TRUNCATED]"))
        self.assertLessEqual(len(message.encode("utf-8")), 64 + len("…[TRUNCATED]".encode("utf-8")))

    async def test_rate_limit_throttles_and_close_flushes_without_loss(self) -> None:
        with mock.patch("time.time", return_value=1000.0):
            streamer, chunks, _log_path = self._streamer(
                batch_interval=999.0, rate_limit=1, line_max_bytes=1024
            )
            streamer.feed("stdout", "l1")
            streamer._flush()  # pyright: ignore[reportPrivateUsage] — 限速窗内唯一名额
            streamer.feed("stdout", "l2")
            streamer.feed("stdout", "l3")
            streamer._flush()  # pyright: ignore[reportPrivateUsage] — 超出每秒包数：推迟合并
            self.assertEqual(len(chunks), 1)
            await streamer.close()  # close 语义优先于限速，冲掉尾批
        self.assertEqual([chunk["seq"] for chunk in chunks], [0, 1])
        self.assertEqual(
            [line["msg"] for chunk in chunks for line in chunk["lines"]],
            ["l1", "l2", "l3"],
        )

    async def test_batch_interval_flushes_background(self) -> None:
        streamer, chunks, _log_path = self._streamer(batch_interval=0.2, batch_lines=50)
        task = asyncio.create_task(streamer.run())
        try:
            streamer.feed("stdout", "tick")
            ok = await wait_until(lambda: len(chunks) >= 1, timeout=5.0, interval=0.05)
            self.assertTrue(ok, "行数不满时应按秒数阈值批量")
        finally:
            await streamer.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual([line["msg"] for line in chunks[0]["lines"]], ["tick"])

    async def test_integration_log_stream_covers_run_output(self) -> None:
        await self.boot()
        self.push_material("m-log-run", fixture_zip(), FIXTURE_RECIPE)
        self.plugin._on_job(
            self.job_payload(
                "j-log-run",
                params={"material_id": "m-log-run", "targets": ["tests/case_basic.py::test_fail"]},
            ),
            None,
        )
        result = await self.wait_result()
        chunks = self.reports(TOPIC_JOB_LOG)
        self.assertTrue(chunks)
        self.assertEqual([chunk["seq"] for chunk in chunks], list(range(len(chunks))))
        self.assertTrue(self.plugin.stats()["logs_sent"] >= len(chunks))
        artifacts = {str(a["name"]): a for a in cast(list[Mapping[str, Any]], result["artifacts"])}
        text = self.artifact_path(artifacts["pytest.log"]).read_text(encoding="utf-8")
        for chunk in chunks:
            for line in cast(list[Mapping[str, Any]], chunk["lines"]):
                self.assertIn(f"[{chunk['stream']}] {line['msg']}", text)  # 双写一致
        # 哨兵 case 归属（粘连哨兵拆分）：进度行挂对应 nodeid，CASE_END 归零后
        # 会话级失败摘要不再挂 case（pytest 失败详情在会话末 FAILURES 段输出）
        fail_lines = [
            str(line["msg"])
            for chunk in chunks
            if chunk["case_id"] == "tests/case_basic.py::test_fail"
            for line in cast(list[Mapping[str, Any]], chunk["lines"])
        ]
        self.assertTrue(any(message.strip() == "F" for message in fail_lines), fail_lines)
        session_lines = [
            str(line["msg"])
            for chunk in chunks
            if not chunk["case_id"]
            for line in cast(list[Mapping[str, Any]], chunk["lines"])
        ]
        self.assertTrue(any("assert 1 == 2" in message for message in session_lines))


# ================================================================ 7. 幂等 / proto
class IdempotencyTests(ExecutorStackTestCase):
    """(job_id, attempt) 去重表：重复消息只执行一次（D5 / §10.1）。"""

    async def test_duplicate_job_message_executes_once(self) -> None:
        await self.boot()
        self.push_material("m-dup", fixture_zip(), FIXTURE_RECIPE)
        payload = self.job_payload(
            "j-dup", params={"material_id": "m-dup", "targets": ["tests/case_params.py"]}
        )
        self.plugin._on_job(dict(payload), None)
        self.plugin._on_job(dict(payload), None)  # 重复消息（执行前）
        await self.wait_result()
        await wait_until(lambda: self.plugin.stats()["executed"] >= 1, timeout=60.0)
        await asyncio.sleep(0.5)  # 留窗口给误漏的第二次执行暴露
        self.plugin._on_job(dict(payload), None)  # 执行完后的重复消息
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.results()), 1)
        self.assertEqual(self.plugin.stats()["executed"], 1)
        self.assertEqual(self.plugin.stats()["deduped"], 2)


class ProtoRejectTests(ExecutorStackTestCase):
    """proto 拒收：proto ≠ TEST_PROTO 回 failed: proto mismatch 且不执行（D8）。"""

    async def test_proto_mismatch_reports_failure_without_execution(self) -> None:
        await self.boot()
        self.plugin._on_job(
            self.job_payload("j-proto", proto=99, params={"targets": ["cases"]}), None
        )
        result = await self.wait_result(timeout=10.0)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(str(result["error"]).startswith("proto mismatch"))
        self.assertEqual(result["cases"], [])
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(self.reports(TOPIC_JOB_PROGRESS), [])  # 未进入执行
        self.assertEqual(self.plugin.stats()["proto_rejected"], 1)
        self.assertEqual(self.plugin.stats()["executed"], 0)


if __name__ == "__main__":
    unittest.main()
