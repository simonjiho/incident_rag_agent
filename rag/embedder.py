"""Ollama 임베딩 클라이언트 — 문서 청크는 디스크 캐시, 질의는 메모리 캐시.

runbook 청크는 거의 바뀌지 않으므로 sha1(text) → 벡터를 data/.rag_cache/<model>.json에
저장해 두고, MCP 서버가 재기동돼도 Ollama를 다시 부르지 않는다.

Ollama가 멈춰도 진단 전체가 멈추지 않게 두 가지를 둔다.
  - 질의 임베딩은 짧은 타임아웃(5s)으로 부른다 — 청크 일괄 임베딩만 긴 타임아웃(60s)
  - 한 번 실패하면 cooldown(30s) 동안은 요청 없이 바로 실패한다 (서킷 브레이커)
"""
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
import numpy as np

DEFAULT_CACHE_DIR = Path(__file__).parent.parent / "data" / ".rag_cache"
BATCH_SIZE = 32
QUERY_CACHE_SIZE = 256
QUERY_TIMEOUT_SEC = 5.0   # 콜드 로딩(bge-m3 약 1.3s)은 넉넉히 넘기고, 멈춘 서버는 빨리 포기한다
COOLDOWN_SEC = 30.0       # 실패 후 이 시간 동안은 Ollama를 부르지 않는다


class EmbeddingUnavailable(RuntimeError):
    """Ollama 연결 실패·모델 없음 등 — 호출 측은 BM25로 폴백한다."""


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1.0, norms)


class OllamaEmbedder:
    def __init__(self, model: str | None = None, base_url: str | None = None,
                 cache_dir: Path | None = DEFAULT_CACHE_DIR, timeout: float = 60.0,
                 query_timeout: float = QUERY_TIMEOUT_SEC, cooldown: float = COOLDOWN_SEC):
        self.model = model or os.getenv("RAG_EMBED_MODEL", "bge-m3")
        self.base_url = (base_url or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.timeout, self.query_timeout, self.cooldown = timeout, query_timeout, cooldown
        self._down_until = 0.0   # 서킷 브레이커 — 이 시각(monotonic) 전에는 요청하지 않는다
        safe_name = self.model.replace(":", "_").replace("/", "_")
        self.cache_path = Path(cache_dir) / f"{safe_name}.json" if cache_dir else None
        self._disk = self._load_disk()
        self._query_cache: dict[str, np.ndarray] = {}

    def embed(self, texts: list[str]) -> np.ndarray:
        """문서 청크 임베딩 (행 단위 L2 정규화). 캐시에 없는 것만 배치로 요청한다."""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        keys = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
        missing = {k: t for k, t in zip(keys, texts) if k not in self._disk}   # 중복 제거
        items = list(missing.items())
        for i in range(0, len(items), BATCH_SIZE):
            batch = items[i:i + BATCH_SIZE]
            vectors = self._request([t for _, t in batch], self.timeout)
            self._disk.update({k: v for (k, _), v in zip(batch, vectors)})
        if missing:
            self._save_disk()
        return _normalize(np.array([self._disk[k] for k in keys], dtype=np.float32))

    def embed_query(self, text: str) -> np.ndarray:
        """질의 임베딩 — 같은 질의 반복(평가 루프 등)은 메모리 캐시로 처리."""
        if text not in self._query_cache:
            if len(self._query_cache) >= QUERY_CACHE_SIZE:
                self._query_cache.pop(next(iter(self._query_cache)))   # 가장 오래된 것 제거
            vector = np.array(self._request([text], self.query_timeout), dtype=np.float32)
            self._query_cache[text] = _normalize(vector)[0]
        return self._query_cache[text]

    def _request(self, texts: list[str], timeout: float) -> list[list[float]]:
        """POST /api/embed — 네트워크·HTTP 오류는 전부 EmbeddingUnavailable로 바꾼다.
        실패하면 cooldown 동안은 Ollama를 부르지 않고 바로 실패한다 (호출 측은 BM25로 계속)."""
        if time.monotonic() < self._down_until:
            raise EmbeddingUnavailable(f"{self.model} @ {self.base_url}: 최근 실패로 요청 생략")
        try:
            resp = httpx.post(f"{self.base_url}/api/embed",
                              json={"model": self.model, "input": texts}, timeout=timeout)
            resp.raise_for_status()
            return resp.json()["embeddings"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            self._down_until = time.monotonic() + self.cooldown
            raise EmbeddingUnavailable(f"{self.model} @ {self.base_url}: {e}") from e

    def _load_disk(self) -> dict[str, list[float]]:
        if self.cache_path and self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text())
            except ValueError:
                return {}   # 깨진 캐시는 버리고 다시 계산
        return {}

    def _save_disk(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(f".{os.getpid()}.tmp")   # 동시 실행 대비 원자적 교체
        tmp.write_text(json.dumps(self._disk))
        os.replace(tmp, self.cache_path)
