"""落库主键的生成规则（``repositories.ids``）。

纯逻辑，不需要任何外部服务。单独测它是因为"会话历史按什么排序"整件事都压在这
一个函数上：MySQL 的 ``DATETIME`` 只到秒，同一秒内写入的记录靠 ``created_at``
分不出先后，排序的稳定性只能由主键自己提供。
"""

from __future__ import annotations

import re
import time
from collections import defaultdict

from app.repositories.ids import new_id, new_ordered_id

ORDERED_ID_PATTERN = re.compile(r"^\d{13}[0-9a-f]{16}$")


def test_ordered_id_has_a_millisecond_timestamp_prefix() -> None:
    now_ms = int(time.time() * 1000)

    prefix = int(new_ordered_id()[:13])

    assert abs(prefix - now_ms) < 1000


def test_ordered_id_shape_is_within_the_column_width() -> None:
    """29 字符，远小于 ``VARCHAR(64)``。"""
    assert ORDERED_ID_PATTERN.match(new_ordered_id())
    assert len(new_ordered_id()) == 29


def test_ordered_ids_never_collide_even_within_one_millisecond() -> None:
    """同一毫秒内的并发写入靠随机后缀区分——它们的毫秒前缀是一样的。"""
    groups: dict[str, list[str]] = defaultdict(list)
    for _ in range(2000):
        value = new_ordered_id()
        groups[value[:13]].append(value)

    assert max(len(group) for group in groups.values()) > 1, "样本里应当出现同毫秒的写入"
    for group in groups.values():
        assert len(set(group)) == len(group), "同一毫秒内的主键撞车会直接写不进去"


def test_ordered_ids_sort_by_time_across_milliseconds() -> None:
    """跨毫秒时字典序 == 时间序，这是排序稳定性的来源。"""
    first = new_ordered_id()
    time.sleep(0.005)
    second = new_ordered_id()

    assert first < second


def test_plain_id_is_a_32_char_hex_uuid() -> None:
    value = new_id()

    assert len(value) == 32
    assert re.fullmatch(r"[0-9a-f]{32}", value)
    assert value != new_id()
