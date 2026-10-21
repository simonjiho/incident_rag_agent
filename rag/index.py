"""runbook 검색 인덱스 — vector(임베딩 cosine) · bm25 · hybrid(RRF).

incident_type = runbook 파일명(stem). 점수는 청크 단위로 매기고, 유형별 점수는 그 유형
청크들의 최댓값을 쓴다 — 증상 bullet 하나만 딱 맞아도 해당 runbook이 올라오게.
"""
import re
from pathlib import Path

import numpy as np

from rag.bm25 import BM25Okapi, tokenize
from rag.embedder import EmbeddingUnavailable, OllamaEmbedder

DEFAULT_RUNBOOK_DIR = Path(__file__).parent.parent / "data" / "runbooks"
MODES = ("vector", "bm25", "hybrid")
RRF_K = 60
_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+)")


def parse_chunks(text: str) -> list[str]:
    """'## 증상' bullet마다 (제목 + bullet) 1개 + (제목 + bullet 전체) 1개.
    증상 섹션이 없는 구 형식이면 (제목 + 본문 전체) 1개."""
    lines = text.strip().splitlines() or [""]
    title = lines[0].lstrip("#").strip()
    bullets, in_symptoms = [], False
    for line in lines[1:]:
        if line.startswith("#"):
            in_symptoms = line.startswith("## ") and "증상" in line
        elif in_symptoms and (m := _BULLET.match(line)):
            bullets.append(m.group(1).strip())
    if not bullets:
        return [title + "\n" + "\n".join(lines[1:]).strip()]
    return [f"{title}\n{b}" for b in bullets] + [title + "\n" + "\n".join(bullets)]


def rrf_scores(*score_maps: dict[str, float], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion — 점수 크기는 버리고 순위만 써서 sum 1/(k+rank).
    점수가 0 이하인 항목은 그 목록에서 순위를 받지 않는다 (BM25 무매칭 등)."""
    fused: dict[str, float] = {}
    for scores in score_maps:
        ranked = sorted((t for t, s in scores.items() if s > 0), key=lambda t: (-scores[t], t))
        for rank, t in enumerate(ranked, start=1):
            fused[t] = fused.get(t, 0.0) + 1.0 / (k + rank)
    return fused


class RunbookIndex:
    def __init__(self, runbook_dir: Path, embedder=None):
        self.runbook_dir = Path(runbook_dir)
        self.embedder = embedder or OllamaEmbedder()
        self.reload()

    @classmethod
    def from_dir(cls, runbook_dir: Path, embedder=None) -> "RunbookIndex":
        return cls(runbook_dir, embedder)

    def reload(self) -> None:
        """runbook 파일을 다시 읽어 청크·BM25를 만든다. 임베딩은 첫 벡터 검색 때 계산(지연)."""
        self.chunks: list[tuple[str, str]] = [
            (path.stem, chunk)
            for path in sorted(self.runbook_dir.glob("*.md"))
            for chunk in parse_chunks(path.read_text(encoding="utf-8"))]
        self._bm25 = BM25Okapi([tokenize(text) for _, text in self.chunks])
        self._vectors: np.ndarray | None = None

    def search(self, query: str, top_k: int = 3, mode: str = "hybrid") -> dict:
        mode = "vector" if mode == "rag" else mode
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES} or 'rag', got {mode!r}")
        if not self.chunks:
            return {"mode": mode, "degraded": False, "hits": []}

        bm25_best = self._best_per_type(self._bm25.get_scores(tokenize(query)))
        bm25_max = max(s for s, _ in bm25_best.values()) or 1.0
        bm25 = {t: s / bm25_max for t, (s, _) in bm25_best.items()}   # 질의 내 최댓값으로 [0,1]

        vector_best, degraded = {}, False
        if mode != "bm25":
            try:
                vector_best = self._best_per_type(self._vector_scores(query))
            except EmbeddingUnavailable:
                mode, degraded = "bm25", True   # Ollama가 없으면 BM25만으로 계속
        vector = {t: s for t, (s, _) in vector_best.items()}

        final = {"vector": vector, "bm25": bm25, "hybrid": rrf_scores(vector, bm25)}[mode]
        order = sorted((t for t in bm25 if final.get(t, 0.0) > 0),   # 점수 0 = 무관, 제외
                       key=lambda t: (-final[t], -vector.get(t, 0.0), t))
        snippet_from = vector_best or bm25_best   # 스니펫은 벡터가 고른 청크 우선
        hits = [{"incident_type": t,
                 "score": round(final[t], 4),
                 "vector_score": round(vector[t], 4) if vector else None,
                 "bm25_score": round(bm25[t], 4),
                 "snippet": self._snippet(snippet_from[t][1])}
                for t in order[:top_k]]
        return {"mode": mode, "degraded": degraded, "hits": hits}

    def _vector_scores(self, query: str) -> np.ndarray:
        """청크별 cosine — 벡터가 정규화돼 있으므로 내적이 곧 cosine."""
        if self._vectors is None:
            self._vectors = self.embedder.embed([text for _, text in self.chunks])
        return self._vectors @ self.embedder.embed_query(query)

    def _best_per_type(self, chunk_scores) -> dict[str, tuple[float, int]]:
        """청크 점수 목록 → {incident_type: (최고 점수, 그 청크 번호)}."""
        best: dict[str, tuple[float, int]] = {}
        for i, ((incident_type, _), score) in enumerate(zip(self.chunks, chunk_scores)):
            if incident_type not in best or score > best[incident_type][0]:
                best[incident_type] = (float(score), i)
        return best

    def _snippet(self, chunk_idx: int, limit: int = 160) -> str:
        body = self.chunks[chunk_idx][1].split("\n", 1)[-1]   # 제목 줄 제외
        return " / ".join(line.strip() for line in body.splitlines() if line.strip())[:limit]


_default_index: RunbookIndex | None = None


def get_index() -> RunbookIndex:
    """프로세스당 1회만 만드는 기본 인덱스 (data/runbooks, bge-m3). MCP 서버가 사용."""
    global _default_index
    if _default_index is None:
        _default_index = RunbookIndex.from_dir(DEFAULT_RUNBOOK_DIR)
    return _default_index
