"""候选项与排序工具（检索层的公共词汇）。

单独一个模块，是因为两路检索器、融合、精排都要产出与消费同一种候选项；
``Candidate`` 放进任何一个检索器里，另一个就只能反向依赖它。
``app.retrievers.retrieval`` 仍然重新导出这个名字，历史上从那里导入的调用点
（State、rendering、filter_metric）不必跟着改。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(slots=True)
class Candidate:
    """一路召回的候选项。

    ``kind`` 决定哪些字段有意义：

    - ``column``：``name`` 是字段名，``table_name`` 是所属表
    - ``value`` ：``name`` 是取值本身，``column_name`` 是它属于哪个字段，
      此时 ``id`` 是**该字段**的 ``column_info.id``——取值不是独立实体，跟着字段走
    - ``metric``：``name`` 是指标名

    三种都带 ``id``，且都是 MySQL 里的主键（阶段 4 让 Qdrant 点 id 与 ES
    ``column_id`` 直接复用了同一批 uuid），所以召回结果不必再做 id 映射即可回查。

    ``source`` 记录**是哪几路找到它的**（``vector`` / ``fulltext`` / ``hybrid``）。
    它是排查"这条候选项凭什么排在这里"的第一手线索：两路都命中却排在一路命中之后，
    一眼就能看出是融合或精排出了问题，而不是检索本身。

    ``score`` 的量纲随阶段变化（向量余弦 / BM25 / RRF 分 / 交叉编码器分），
    下游只能拿它比大小，不能当阈值用。
    """

    kind: str
    id: str
    name: str
    score: float
    table_name: str | None = None
    column_name: str | None = None
    alias: str | None = None
    description: str | None = None
    source: str = "vector"
    text: str = ""
    """重排用的文档文本，建库时写入（与向量化、全文索引用的是同一份）。

    为空时由检索层按字段拼一个兜底版本——重建知识库之前跑的代码也不该崩。
    """

    def identity(self) -> tuple[str, str, str]:
        """跨路去重用的身份。

        含 ``name`` 而不只是 ``id``：取值的 ``id`` 是**所属字段**的 id，
        同一个字段下的不同取值会撞成一条（"2025"与"2026"都是年份字段的取值）。
        字段与指标的 ``name`` 由 ``id`` 唯一决定，多带这一项不改变去重结果，
        但让三种 kind 共用同一条规则。
        """
        return (self.kind, self.id, self.name)


def rank_by_score(candidates: Iterable[Candidate]) -> list[Candidate]:
    """按分数降序；同分按 id 升序。

    加 id 这个次级键是为了让排序**完全确定**——否则同分项的顺序取决于字典的
    插入顺序，也就取决于上游返回顺序，测试会偶发失败、线上结果会莫名抖动。
    """
    return sorted(candidates, key=lambda c: (-c.score, c.id))
