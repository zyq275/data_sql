"""检索层（阶段 6：混合检索）。

    candidate.py  候选项与排序工具（公共词汇）
    dense.py      Qdrant 稠密向量检索      —— 两路之一
    fulltext.py   Elasticsearch 全文检索    —— 两路之二
    fusion.py     RRF 融合
    retrieval.py  网关：编排两路 → 融合 → 精排

``Candidate`` 从 ``retrieval`` 也重新导出，历史上写的是
``from app.retrievers.retrieval import Candidate``（State、rendering、filter_metric），
那些调用点不必跟着改。
"""

from app.retrievers.candidate import Candidate, rank_by_score
from app.retrievers.dense import DenseRetriever
from app.retrievers.fulltext import FullTextRetriever
from app.retrievers.fusion import RRF_K, reciprocal_rank_fusion
from app.retrievers.retrieval import RetrievalGateway, document_text

__all__ = [
    "RRF_K",
    "Candidate",
    "DenseRetriever",
    "FullTextRetriever",
    "RetrievalGateway",
    "document_text",
    "rank_by_score",
    "reciprocal_rank_fusion",
]
