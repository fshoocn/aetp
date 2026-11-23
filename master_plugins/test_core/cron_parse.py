"""cron 表达式解析（测试计划编辑器 v2 设计 §5.1）。

标准 **5 段**（分 时 日 月 周）：``*``、``*/n``、``a``、``a-b``、``a-b/n`` 与
逗号列表（以上形式的任意组合）。字段范围：分 0-59、时 0-23、日 1-31、
月 1-12、周 0-6（0 = 周日，7 亦作周日，内部归一 0-6）。

日（第 3 段）与周（第 5 段）**双双限定**（原文均非 ``*``）时按标准 cron **OR**
语义（任一命中即算）；任一段为 ``*`` 时按 AND 语义（限定段单独生效）。
「限定」判定 = 段原文恰为 ``*``（``*/n`` 等派生写法视为限定，v1 简化口径）。

解析失败抛 :class:`ValueError`（中文消息，逐条指出段位与原因）。时区语义 =
服务器本地时间（调用方以 ``datetime.now()`` / ``datetime.fromtimestamp``
构造 naive 本地 datetime 传入，本模块不做时区换算）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

__all__ = ["CronExpr", "parse_cron"]

#: 5 段名（错误消息用）
_FIELD_NAMES: tuple[str, ...] = ("分钟", "小时", "日", "月", "周")
#: 5 段取值范围（周段输入放宽到 0-7：7 与 0 同为周日）
_FIELD_RANGES: tuple[tuple[int, int], ...] = (
    (0, 59),
    (0, 23),
    (1, 31),
    (1, 12),
    (0, 7),
)
#: 周字段段序号（构造集合后 7 归一 0）
_WEEKDAY_INDEX: int = 4
#: next_after 搜索上限（天）：覆盖 2 月 29 日类最长 8 年空档 + 余量
_SEARCH_DAYS: int = 3660


def _is_digits(text: str) -> bool:
    """纯 ASCII 数字串（拒绝符号 / 全角 / 空串）。"""
    return bool(text) and all("0" <= ch <= "9" for ch in text)


def _parse_value(text: str, index: int, lo: int, hi: int) -> int:
    """单个数值（段内）：非数字 / 越界抛 ValueError（中文消息）。"""
    name = _FIELD_NAMES[index]
    if not _is_digits(text):
        raise ValueError(f"第 {index + 1} 段（{name}）非法数字：{text!r}")
    value = int(text)
    if not lo <= value <= hi:
        raise ValueError(f"第 {index + 1} 段（{name}）值 {value} 超出 {lo}-{hi}")
    return value


def _parse_step(text: str, index: int) -> int:
    """步长 ``/n`` 的 ``n``：需 ≥ 1 的纯数字。"""
    name = _FIELD_NAMES[index]
    if not _is_digits(text):
        raise ValueError(f"第 {index + 1} 段（{name}）步长非法：{text!r}（需是 ≥ 1 的整数）")
    step = int(text)
    if step < 1:
        raise ValueError(f"第 {index + 1} 段（{name}）步长需 ≥ 1，收到 {step}")
    return step


def _parse_span(body: str, index: int, lo: int, hi: int) -> tuple[int, int]:
    """范围体 ``a-b``（或 ``*`` / 单值归一成闭区间）：起点大于终点报错。"""
    name = _FIELD_NAMES[index]
    if body == "*":
        return lo, hi
    left, dash, right = body.partition("-")
    if not dash:
        value = _parse_value(body, index, lo, hi)
        return value, value
    start = _parse_value(left, index, lo, hi)
    end = _parse_value(right, index, lo, hi)
    if start > end:
        raise ValueError(f"第 {index + 1} 段（{name}）范围 {start}-{end} 起点大于终点")
    return start, end


def _parse_piece(piece: str, index: int, lo: int, hi: int) -> set[int]:
    """段内单项：``*`` / ``*/n`` / ``a`` / ``a-b`` / ``a-b/n``（子集外报错）。"""
    name = _FIELD_NAMES[index]
    body, slash, step_text = piece.partition("/")
    if slash:
        if body != "*" and "-" not in body:
            raise ValueError(
                f"第 {index + 1} 段（{name}）写法不支持：{piece!r}"
                "（可选：*、*/n、a、a-b、a-b/n）"
            )
        step = _parse_step(step_text, index)
        start, end = _parse_span(body, index, lo, hi)
    else:
        step = 1
        start, end = _parse_span(body, index, lo, hi)
    return set(range(start, end + 1, step))


def _parse_field(raw: str, index: int) -> tuple[frozenset[int], bool]:
    """单段解析 → ``(取值集合, 原文恰为 *)``；周段集合 7 归一 0。"""
    name = _FIELD_NAMES[index]
    lo, hi = _FIELD_RANGES[index]
    text = raw.strip()
    if not text:
        raise ValueError(f"第 {index + 1} 段（{name}）为空")
    values: set[int] = set()
    for token in text.split(","):
        piece = token.strip()
        if not piece:
            raise ValueError(f"第 {index + 1} 段（{name}）含空项：{raw!r}")
        values |= _parse_piece(piece, index, lo, hi)
    if index == _WEEKDAY_INDEX:
        values = {value % 7 for value in values}
    return frozenset(values), text == "*"


@dataclass(frozen=True)
class CronExpr:
    """解析后的 5 段 cron 表达式（匹配集合 + 日 / 周限定标志）。"""

    #: 原始表达式（归一去首尾空白，诊断用）
    source: str
    #: 分（0-59）
    minutes: frozenset[int]
    #: 时（0-23）
    hours: frozenset[int]
    #: 日（1-31）
    days: frozenset[int]
    #: 月（1-12）
    months: frozenset[int]
    #: 周（0-6，0 = 周日）
    weekdays: frozenset[int]
    #: 第 3 段（日）原文恰为 ``*``（非限定）
    day_any: bool
    #: 第 5 段（周）原文恰为 ``*``（非限定）
    weekday_any: bool

    def matches(self, dt: datetime) -> bool:
        """dt（服务器本地时间）是否命中本表达式。

        分 / 时 / 月 AND；日 / 周双双限定时 OR（任一命中即算），任一为
        ``*`` 时只看限定段。
        """
        if dt.minute not in self.minutes or dt.hour not in self.hours or dt.month not in self.months:
            return False
        return self._day_ok(dt.day, (dt.weekday() + 1) % 7)

    def next_after(self, dt: datetime) -> datetime | None:
        """严格晚于 dt 的下一命中分钟槽（本地 naive datetime，秒清零）。

        逐日推进（命中日才展开时 / 分集合）：常规表达式一两步即中，
        2 月 29 日类最长 8 年空档也在 :data:`_SEARCH_DAYS` 内；无命中
        （如 2 月 30 日）返回 None。
        """
        start = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.date()
        for offset in range(_SEARCH_DAYS):
            current = day + timedelta(days=offset)
            if self._date_ok(current):
                found = self._first_slot(current, start)
                if found is not None:
                    return found
        return None

    def _day_ok(self, day: int, weekday: int) -> bool:
        """日 / 周判定（OR / AND 语义，见 :meth:`matches`）。"""
        day_hit = day in self.days
        weekday_hit = weekday in self.weekdays
        if self.day_any and self.weekday_any:
            return True
        if self.day_any:
            return weekday_hit
        if self.weekday_any:
            return day_hit
        return day_hit or weekday_hit

    def _date_ok(self, day: date) -> bool:
        """日期是否落在月 + 日 / 周命中面内。"""
        return day.month in self.months and self._day_ok(day.day, (day.weekday() + 1) % 7)

    def _first_slot(self, day: date, start: datetime) -> datetime | None:
        """命中日内第一个 ≥ start 的时:分槽（时 / 分集合升序遍历）。"""
        base = datetime(day.year, day.month, day.day)
        for hour in sorted(self.hours):
            for minute in sorted(self.minutes):
                candidate = base.replace(hour=hour, minute=minute)
                if candidate >= start:
                    return candidate
        return None


def parse_cron(expr: str) -> CronExpr:
    """解析标准 5 段 cron 表达式；失败抛 ValueError（中文消息）。"""
    text = expr.strip()
    if not text:
        raise ValueError("cron 表达式为空")
    parts = text.split()
    if len(parts) != 5:
        raise ValueError(f"cron 段数应为 5（分 时 日 月 周），收到 {len(parts)} 段")
    parsed = [_parse_field(part, index) for index, part in enumerate(parts)]
    return CronExpr(
        source=text,
        minutes=parsed[0][0],
        hours=parsed[1][0],
        days=parsed[2][0],
        months=parsed[3][0],
        weekdays=parsed[4][0],
        day_any=parsed[2][1],
        weekday_any=parsed[4][1],
    )
