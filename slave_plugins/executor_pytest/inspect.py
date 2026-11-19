"""脚本分析 + 用例解析（DESIGN §6 / §7）：``pytest.inspect`` 的执行面。

* **请求**：``{proto, request_id, material_id, mode: "analyze"|"collect", node_id?}``；
  ``request_id`` 幂等（应答缓存，重复请求重发同一应答——缓存由插件层持有）；
* **analyze**：临时目录组装后跑 §6.2 五项检查——包完整性（``material.corrupt``）、
  配方校验（``recipe.invalid``）、语法（``syntax.error``）、依赖声明
  （``deps.missing``，warning）、可收集性（``collect.error``）；
  问题模型 ``{code, path, line?, message, severity}``；
* **collect**：组装后经 ``aetp_marker`` 插件导出结构化用例清单（§7，
  ``AETP_MARKER_OUT``）；``params`` 为 nodeid 近似值；
* 内部单项检查超时 120s（整体超时由主端管，本端不设限）。

临时目录随检查结束清理；``setup`` 命令不执行（决策 D15），材料按内部可信
处理（决策 D16）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from .assemble import AssembleError, apply_layout, extract_zip, validate_recipe
from .config import PytestExecutorConfig
from .executor import MARKER_DIR, LineSink, run_process

#: 单项内部检查超时（秒，§6.2）
CHECK_TIMEOUT_S = 120.0
#: 收集清单导出文件名（临时目录内）
_COLLECT_OUT = "collect.json"

type ProblemSeverity = Literal["error", "warning"]
type InspectMode = Literal["analyze", "collect"]


class _Capture(LineSink):
    """子进程输出收集器（检查用；只留尾部，防问题消息爆炸）。"""

    def __init__(self, limit: int = 40) -> None:
        self._limit = limit
        self.tail: list[str] = []

    def feed(self, stream: Literal["stdout", "stderr"], raw_line: str) -> None:
        self.tail.append(raw_line.rstrip("\r\n"))
        if len(self.tail) > self._limit:
            del self.tail[0]


def make_problem(
    code: str,
    message: str,
    *,
    path: str = "",
    line: int = 0,
    severity: ProblemSeverity = "error",
) -> dict[str, Any]:
    """构造问题条目（``line`` 仅在已知时出现，§6.2 问题模型）。"""
    problem: dict[str, Any] = {"code": code, "path": path, "message": message, "severity": severity}
    if line > 0:
        problem["line"] = line
    return problem


async def run_inspect(
    mode: InspectMode,
    *,
    zip_path: Path,
    recipe: Mapping[str, Any],
    temp_dir: Path,
    config: PytestExecutorConfig,
) -> dict[str, Any]:
    """在临时目录组装后执行检查 / 解析；返回应答核心
    ``{ok, problems, cases, duration_s}``（调用方补 proto / request_id / node_id）。
    """
    started = time.time()
    problems: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    try:
        problems.extend(_check_package(zip_path))
        extract_ok = False
        if not any(item["severity"] == "error" for item in problems):
            try:
                extract_zip(zip_path, temp_dir)
                extract_ok = True
            except AssembleError as error:
                problems.append(make_problem("material.corrupt", f"材料解压失败：{error}"))
        if extract_ok:
            problems.extend(_check_recipe(recipe, temp_dir))
            layout_ok = not any(item["severity"] == "error" for item in problems)
            if layout_ok:
                try:
                    apply_layout(temp_dir, recipe)
                except AssembleError as error:
                    problems.append(make_problem("recipe.invalid", f"配方铺排失败：{error}"))
                    layout_ok = False
            if layout_ok:
                problems.extend(_check_entry(recipe, temp_dir))
                if mode == "analyze":
                    problems.extend(await _check_syntax(temp_dir, config.python))
                    problems.extend(await _check_deps(temp_dir, recipe, config.python))
                collected, collect_problems = await _check_collect(temp_dir, recipe, config.python, export=(mode == "collect"))
                problems.extend(collect_problems)
                if mode == "collect" and not collect_problems:
                    cases = collected
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    ok = not any(item["severity"] == "error" for item in problems)
    return {
        "ok": ok,
        "problems": problems,
        "cases": cases,
        "duration_s": round(time.time() - started, 3),
    }


# -- 1. 包完整性 -------------------------------------------------------------


def _check_package(zip_path: Path) -> list[dict[str, Any]]:
    """检查项 1（``material.corrupt``）：zip 可读 + CRC 全量校验。"""
    if not zip_path.is_file():
        return [make_problem("material.corrupt", f"材料包不存在：{zip_path.name}")]
    try:
        with zipfile.ZipFile(zip_path) as package:
            bad = package.testzip()
    except zipfile.BadZipFile as error:
        return [make_problem("material.corrupt", f"材料包不是合法 zip：{error}")]
    if bad is not None:
        return [make_problem("material.corrupt", f"材料包 CRC 校验失败：{bad}")]
    return []


# -- 2. 配方校验 -------------------------------------------------------------


def _check_recipe(recipe: Mapping[str, Any], root: Path) -> list[dict[str, Any]]:
    """检查项 2（``recipe.invalid``）：结构合法 + layout src 存在 + env 合法。"""
    problems = [
        make_problem("recipe.invalid", message) for message in validate_recipe(recipe)
    ]
    layout = recipe.get("layout")
    if isinstance(layout, list):
        for index, entry in enumerate(cast(list[object], layout)):
            if not isinstance(entry, Mapping):
                continue
            src = cast(Mapping[object, object], entry).get("src")
            if isinstance(src, str) and src.strip():
                source = (root / src).resolve()
                if not source.is_relative_to(root.resolve()) or not source.exists():
                    problems.append(make_problem("recipe.invalid", f"layout[{index}] 源不存在或越界：{src}"))
    return problems


def _check_entry(recipe: Mapping[str, Any], root: Path) -> list[dict[str, Any]]:
    """检查项 2 续（``recipe.invalid``）：entry 铺排后可达。"""
    entry = recipe.get("entry")
    if not isinstance(entry, str) or not entry.strip():
        return []  # 无 entry 时执行端兜底 targets，不算问题
    target = (root / entry.strip()).resolve()
    if not target.is_relative_to(root.resolve()) or not target.exists():
        return [make_problem("recipe.invalid", f"entry 不可达：{entry}")]
    return []


# -- 3. 语法 -----------------------------------------------------------------

#: 逐 .py 编译的探针脚本（真实解释器执行，版本敏感的语法如实暴露）；
#: 内置 ``compile()`` 与 ``py_compile`` 同源同检，但不落字节码（不污染材料目录）
_COMPILE_PROBE = """
import json, sys
problems = []
for path in sys.argv[1:]:
    try:
        with open(path, "rb") as handle:
            compile(handle.read(), path, "exec")
    except SyntaxError as error:
        problems.append({
            "path": path,
            "line": error.lineno or 0,
            "message": error.msg or str(error),
        })
    except OSError as error:
        problems.append({"path": path, "line": 0, "message": str(error)})
print(json.dumps({"problems": problems}, ensure_ascii=False))
""".strip()


async def _check_syntax(root: Path, python: str) -> list[dict[str, Any]]:
    """检查项 3（``syntax.error``）：配置的解释器逐 .py 编译（单项 120s 超时）。"""
    files = sorted(str(path) for path in root.rglob("*.py"))
    if not files:
        return []
    capture = _Capture()
    argv = [python or sys.executable, "-c", _COMPILE_PROBE, *files]
    outcome = await _run_probe(argv, root, capture)
    problems: list[dict[str, Any]] = []
    for item in _probe_rows(capture):
        path = str(item.get("path") or "")
        rel = _relative(path, root)
        problems.append(
            make_problem(
                "syntax.error",
                str(item.get("message") or "语法错误"),
                path=rel,
                line=int(item.get("line") or 0),
            )
        )
    if outcome != 0 and not problems:
        problems.append(make_problem("syntax.error", f"语法检查失败：退出码 {outcome}"))
    return problems


async def _run_probe(argv: Sequence[str], root: Path, capture: _Capture) -> int:
    """跑一个探针子进程（无 marker、无超时外等待），返回退出码。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    outcome = await run_process(
        argv,
        cwd=root,
        env=env,
        timeout_s=CHECK_TIMEOUT_S,
        streamer=capture,
    )
    return outcome.exit_code if outcome.exit_code is not None else -1


def _probe_rows(capture: _Capture) -> list[dict[str, Any]]:
    """取探针输出末行的 JSON（``{"problems": [...]}``）。"""
    for line in reversed(capture.tail):
        try:
            raw: object = json.loads(line)
        except ValueError:
            continue
        source = cast("Mapping[str, Any]", raw) if isinstance(raw, Mapping) else None
        rows = source.get("problems") if source is not None else None
        if isinstance(rows, list):
            return [cast(dict[str, Any], row) for row in cast(list[object], rows) if isinstance(row, dict)]
    return []


def _relative(path: str, root: Path) -> str:
    """探针里的绝对路径 → 工程内相对路径（问题展示口径）。"""
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path


# -- 4. 依赖声明 -------------------------------------------------------------


async def _check_deps(root: Path, recipe: Mapping[str, Any], python: str) -> list[dict[str, Any]]:
    """检查项 4（``deps.missing``，warning）：requirements 与环境探测（pip show）。"""
    requirements: list[str] = []
    for source in (recipe.get("requirements"), _root_requirements(root)):
        if isinstance(source, list):
            requirements.extend(str(item).strip() for item in cast(list[object], source))
    problems: list[dict[str, Any]] = []
    checked: set[str] = set()
    for requirement in requirements:
        if not requirement or requirement.startswith(("#", "-")):
            continue
        name = _dist_name(requirement)
        if not name or name in checked:
            continue
        checked.add(name)
        capture = _Capture()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        outcome = await run_process(
            [python or sys.executable, "-m", "pip", "show", name],
            cwd=root,
            env=env,
            timeout_s=CHECK_TIMEOUT_S,
            streamer=capture,
        )
        if outcome.exit_code != 0:
            problems.append(
                make_problem("deps.missing", f"依赖未安装：{name}（{requirement}）", path="requirements.txt", severity="warning")
            )
    return problems


def _root_requirements(root: Path) -> list[str]:
    """工程根的 requirements.txt（材料自带声明，与 recipe.requirements 并检）。"""
    path = root / "requirements.txt"
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]


def _dist_name(requirement: str) -> str:
    """``pkg[extra]>=1`` → ``pkg``（pip show 的发行名近似）。"""
    head = requirement.split(";", 1)[0]
    for separator in ("[", "<", ">", "=", "!", "~", " "):
        head = head.split(separator, 1)[0]
    return head.strip()


# -- 5. 可收集性 / 用例解析 ---------------------------------------------------


async def _check_collect(
    root: Path,
    recipe: Mapping[str, Any],
    python: str,
    *,
    export: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """检查项 5（``collect.error``）：``--collect-only`` 经 marker 插件；export 时带清单。"""
    entry = recipe.get("entry")
    target = entry.strip() if isinstance(entry, str) and entry.strip() else "."
    collect_out = root / _COLLECT_OUT
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = f"{MARKER_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    if export:
        env["AETP_MARKER_OUT"] = str(collect_out)
    capture = _Capture()
    argv = [python or sys.executable, "-m", "pytest", "--collect-only", "-q", target, "-p", "aetp_marker"]
    outcome = await run_process(argv, cwd=root, env=env, timeout_s=CHECK_TIMEOUT_S, streamer=capture)
    problems: list[dict[str, Any]] = []
    if outcome.timed_out:
        problems.append(make_problem("collect.error", f"收集超时（>{CHECK_TIMEOUT_S:.0f}s）"))
    elif outcome.exit_code != 0:
        tail = " / ".join(capture.tail[-5:])
        problems.append(make_problem("collect.error", f"收集失败：退出码 {outcome.exit_code}；{tail}"))
    cases: list[dict[str, Any]] = []
    if export and not problems:
        cases = _read_cases(collect_out)
        if not cases:
            problems.append(make_problem("collect.error", "收集清单为空"))
    return cases, problems


def _read_cases(collect_out: Path) -> list[dict[str, Any]]:
    """读 marker 插件导出的结构化清单。"""
    try:
        raw: object = json.loads(collect_out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    source = cast("Mapping[str, Any]", raw) if isinstance(raw, Mapping) else None
    rows = source.get("cases") if source is not None else None
    if not isinstance(rows, list):
        return []
    return [cast(dict[str, Any], row) for row in cast(list[object], rows) if isinstance(row, dict)]


__all__: list[str] = ["CHECK_TIMEOUT_S", "InspectMode", "make_problem", "run_inspect"]
