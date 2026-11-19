"""日志流批量器（DESIGN §10.4）：子进程输出双写 + LogChunk 批量上报。

* **双写**（决策 D4）：每行完整件追加进 ``pytest.log``（产物），实时流按批量
  打包成 :class:`common.test_contracts.LogChunk`（fire-and-forget 上报）；
* **批量**：每 :attr:`batch_lines` 行或 :attr:`batch_interval` 秒先到先发；
  每秒至多 :attr:`rate_limit` 包，超出**合并**进下一包（不丢行）；
* **截断**：单行超 :attr:`line_max_bytes` 字节截断；
* **seq**：每 ``(job_id, attempt)`` 从 0 单调递增，丢包 / 断层可被消费端检测；
* **case 归属**：marker 哨兵行 ``\\x00AETP_CASE <nodeid>\\x00`` 切换当前
  case_id，``\\x00AETP_CASE_END <nodeid>\\x00`` 归零——哨兵行**不入流**，
  ``pytest.log`` 里落成可读的 ``[case]`` 边界行。

``stream`` 口径（契约 ``LogChunk.stream``）：``stdout`` 行 level=20(INFO)、
``stderr`` 行 level=30(WARNING)；``case`` 留给将来合成的用例级消息。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, cast

from common.test_contracts import TEST_PROTO, LogChunk

#: marker 哨兵：logstart 切换当前用例（aetp_marker 插件打印）
CASE_MARKER = "\x00AETP_CASE "
#: marker 哨兵：logfinish 归零当前用例
CASE_END_MARKER = "\x00AETP_CASE_END "
#: 哨兵行结尾（成对出现，防输出里偶然命中）
_MARKER_TAIL = "\x00"

#: stdout 行的 level（logging.INFO）
_LEVEL_STDOUT = 20
#: stderr 行的 level（logging.WARNING）
_LEVEL_STDERR = 30

#: 批量触发时的轮询粒度（秒）——批量间隔 / 限速窗口都在这里推进
_TICK_S = 0.1


class LogStreamer:
    """单个 ``(job_id, attempt)`` 的日志流批量器。

    用法：``feed()`` 喂行（来自 stdout / stderr 读循环），后台 ``run()`` 定时
    批量发送，结束时 ``close()`` 冲掉尾批。``send_chunk`` 回调收到
    ``asdict(LogChunk)`` 形态的 dict，由插件层负责上报。
    """

    def __init__(
        self,
        *,
        job_id: str,
        attempt: int,
        log_path: Path,
        send_chunk: Callable[[Mapping[str, Any]], None],
        batch_lines: int = 50,
        batch_interval: float = 2.0,
        rate_limit: int = 20,
        line_max_bytes: int = 8192,
    ) -> None:
        self._job_id = job_id
        self._attempt = attempt
        self._send_chunk = send_chunk
        self._batch_lines = max(1, int(batch_lines))
        self._batch_interval = max(0.1, float(batch_interval))
        self._rate_limit = max(1, int(rate_limit))
        self._line_max_bytes = max(64, int(line_max_bytes))
        self._seq = 0
        self._case_id = ""
        #: marker 记录的 nodeid（logstart 顺序）——junit 对齐的首选 case_id 来源
        self.case_ids_seen: list[str] = []
        self._pending: list[tuple[str, str, dict[str, Any]]] = []
        self._pending_started = 0.0
        #: 最近一秒内已发包时刻（滑动窗口限速）
        self._sent_stamps: list[float] = []
        self._closed = False
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = log_path.open("a", encoding="utf-8", errors="replace")

    # -- 喂入 ---------------------------------------------------------------
    def feed(self, stream: Literal["stdout", "stderr"], raw_line: str) -> None:
        """喂一行原始输出（不含换行符）；哨兵行切换 case 归属、不入流。"""
        if self._closed:
            return
        line = raw_line.rstrip("\r\n")
        nodeid = _parse_marker(line)
        if nodeid is not None:
            self._on_marker(nodeid)
            return
        message = self._truncate(line)
        now = time.time()
        self._log_file.write(f"[{now:.3f}] [{stream}] {message}\n")
        level = _LEVEL_STDOUT if stream == "stdout" else _LEVEL_STDERR
        if not self._pending:
            self._pending_started = now
        self._pending.append((stream, self._case_id, {"ts": now, "level": level, "msg": message}))

    def _on_marker(self, marker: str) -> None:
        """处理哨兵行：CASE 切换当前用例并记录 nodeid，CASE_END 归零。"""
        if marker.startswith(CASE_MARKER):
            nodeid = marker[len(CASE_MARKER) : -len(_MARKER_TAIL)]
            self._case_id = nodeid
            self.case_ids_seen.append(nodeid)
            self._log_file.write(f"[case] {nodeid}\n")
        elif marker.startswith(CASE_END_MARKER):
            nodeid = marker[len(CASE_END_MARKER) : -len(_MARKER_TAIL)]
            self._case_id = ""
            self._log_file.write(f"[case end] {nodeid}\n")

    def _truncate(self, line: str) -> str:
        """单行按 UTF-8 字节数截断（不劈开多字节字符）。"""
        encoded = line.encode("utf-8", errors="replace")
        if len(encoded) <= self._line_max_bytes:
            return line
        head = encoded[: self._line_max_bytes].decode("utf-8", errors="ignore")
        return f"{head}…[TRUNCATED]"

    # -- 批量发送 -----------------------------------------------------------
    async def run(self) -> None:
        """后台批量循环：行数 / 秒数阈值先到先发；每秒至多 ``rate_limit`` 包。"""
        try:
            while not self._closed:
                await asyncio.sleep(_TICK_S)
                self._maybe_flush()
        finally:
            self._flush()

    def _maybe_flush(self) -> None:
        if not self._pending:
            return
        now = time.time()
        if len(self._pending) >= self._batch_lines:
            self._flush()
        elif now - self._pending_started >= self._batch_interval:
            self._flush()

    def _flush(self) -> None:
        """把待发行按 (stream, case_id) 连续段切包发送；限速窗口满则推迟。

        推迟不丢行：保持在 ``_pending`` 里，下一轮继续尝试（合并即「少发包」）。
        """
        if not self._pending:
            return
        if not self._rate_ok():
            return
        runs = _split_runs(self._pending)
        self._pending = []
        self._pending_started = time.time()
        for stream, case_id, lines in runs:
            self._emit(stream, case_id, lines)

    def _rate_ok(self) -> bool:
        """一秒滑动窗口内不足 ``rate_limit`` 包才允许发（超出推迟合并）。"""
        now = time.time()
        self._sent_stamps = [stamp for stamp in self._sent_stamps if now - stamp < 1.0]
        return len(self._sent_stamps) < self._rate_limit

    def _emit(self, stream: str, case_id: str, lines: Sequence[Mapping[str, Any]]) -> None:
        chunk = LogChunk(
            proto=TEST_PROTO,
            job_id=self._job_id,
            attempt=self._attempt,
            seq=self._seq,
            stream=cast(Literal["stdout", "stderr", "case"], stream),
            case_id=case_id,
            lines=[dict(line) for line in lines],
        )
        self._seq += 1
        self._sent_stamps.append(time.time())
        self._send_chunk(asdict(chunk))

    async def close(self) -> None:
        """收尾：冲掉尾批并关日志文件。"""
        if self._closed:
            return
        self._closed = True
        self._flush()
        # 限速推迟的尾批：封顶再冲一轮（close 语义优先于限速）
        self._sent_stamps.clear()
        self._flush()
        self._log_file.close()


def _parse_marker(line: str) -> str | None:
    """整行是 marker 哨兵则返回原文（调用方再细分），否则 ``None``。"""
    if not (line.startswith(CASE_MARKER) or line.startswith(CASE_END_MARKER)):
        return None
    return line if line.endswith(_MARKER_TAIL) else None


def _split_runs(
    pending: Sequence[tuple[str, str, dict[str, Any]]],
) -> list[tuple[str, str, list[Mapping[str, Any]]]]:
    """把待发行按连续的 (stream, case_id) 段切成多个包。"""
    runs: list[tuple[str, str, list[Mapping[str, Any]]]] = []
    for stream, case_id, line in pending:
        if runs and runs[-1][0] == stream and runs[-1][1] == case_id:
            runs[-1][2].append(line)
        else:
            runs.append((stream, case_id, [line]))
    return runs


__all__: list[str] = ["CASE_END_MARKER", "CASE_MARKER", "LogStreamer"]
