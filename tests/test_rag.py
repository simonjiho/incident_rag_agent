"""rag 패키지 단위 테스트 — 네트워크 없이 결정론적으로 돈다 (Ollama 대신 FakeEmbedder)."""
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import numpy as np
import pytest

from rag import (BM25Okapi, EmbeddingUnavailable, OllamaEmbedder, RunbookIndex,
                 parse_chunks, rrf_scores, tokenize)

NEW_FORMAT = """# Runbook: 인증서 만료 대응 절차

## 증상
- x509 certificate has expired 오류가 로그에 반복된다
- TLS handshake 실패로 외부 호출이 끊긴다

## 조치
1. 인증서 만료일을 확인하고 갱신한다.
"""

OLD_FORMAT = """# Runbook: 디스크 용량 부족 대응 절차

1. disk usage가 90% 이상인지 확인한다.
2. 오래된 로그 파일을 정리해 공간을 확보한다.
"""

MEMORY_FORMAT = """# Runbook: 메모리 누수 대응 절차

## 증상
- memory usage climbing steadily
- heap near limit 경고
"""


class FakeEmbedder:
    """문자 trigram을 해시 버킷에 세어 정규화한 벡터 — 글자가 많이 겹칠수록 cosine이 크다."""
    dim = 512

    def __init__(self):
        self.doc_calls = 0

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim)
        t = text.lower()
        for i in range(len(t) - 2):
            v[zlib.crc32(t[i:i + 3].encode()) % self.dim] += 1
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed(self, texts):
        self.doc_calls += 1
        return np.vstack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


class BrokenEmbedder:
    def embed(self, texts):
        raise EmbeddingUnavailable("ollama down")

    def embed_query(self, text):
        raise EmbeddingUnavailable("ollama down")


@pytest.fixture
def runbook_dir(tmp_path):
    (tmp_path / "cert-expired.md").write_text(NEW_FORMAT, encoding="utf-8")
    (tmp_path / "disk-full.md").write_text(OLD_FORMAT, encoding="utf-8")
    (tmp_path / "memory-leak.md").write_text(MEMORY_FORMAT, encoding="utf-8")
    return tmp_path


def test_tokenize_ascii_and_hangul_bigrams():
    assert tokenize("X509 인증서가 만료") == ["x509", "인증서가", "인증", "증서", "서가", "만료"]
    assert tokenize("get_monitoring_status p95") == ["get", "monitoring", "status", "p95"]


def test_bm25_ranks_relevant_doc_first():
    corpus = [tokenize(t) for t in ["디스크 용량 부족 disk full",
                                    "인증서 만료 x509 certificate expired",
                                    "메모리 누수 heap"]]
    scores = BM25Okapi(corpus).get_scores(tokenize("인증서가 만료됐어요"))
    assert int(np.argmax(scores)) == 1
    assert scores[0] == 0.0 and scores[2] == 0.0


def test_parse_chunks_new_format_symptom_bullets():
    chunks = parse_chunks(NEW_FORMAT)
    assert len(chunks) == 3   # bullet 2개 + 묶음 1개 ('## 조치'는 제외)
    assert all(c.startswith("Runbook: 인증서 만료 대응 절차\n") for c in chunks)
    assert chunks[0].endswith("x509 certificate has expired 오류가 로그에 반복된다")
    assert "갱신" not in chunks[-1]


def test_parse_chunks_old_format_single_chunk():
    chunks = parse_chunks(OLD_FORMAT)
    assert len(chunks) == 1
    assert chunks[0].startswith("Runbook: 디스크 용량 부족 대응 절차\n1. disk usage")


def test_rrf_ordering():
    vector = {"a": 0.9, "b": 0.5, "c": 0.1}   # 순위 a1 b2 c3
    bm25 = {"c": 1.0, "b": 0.4, "a": 0.0}     # 순위 c1 b2, a는 무매칭이라 순위 없음
    fused = rrf_scores(vector, bm25)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert fused["b"] == pytest.approx(2 / 62)
    assert fused["a"] == pytest.approx(1 / 61)
    assert sorted(fused, key=fused.get, reverse=True) == ["c", "b", "a"]


def test_search_aggregates_per_type(runbook_dir):
    index = RunbookIndex.from_dir(runbook_dir, FakeEmbedder())
    result = index.search("x509 certificate has expired", top_k=5, mode="vector")
    types = [h["incident_type"] for h in result["hits"]]
    assert len(types) == len(set(types))   # 청크가 여러 개여도 유형당 1개
    assert types[0] == "cert-expired"
    # 유형 점수 = 그 유형 청크 cosine의 최댓값
    emb = FakeEmbedder()
    q = emb.embed_query("x509 certificate has expired")
    best = max(float(emb._vec(t) @ q) for typ, t in index.chunks if typ == "cert-expired")
    assert result["hits"][0]["vector_score"] == pytest.approx(best, abs=1e-4)
    assert "x509" in result["hits"][0]["snippet"]


def test_hybrid_search_fields(runbook_dir):
    index = RunbookIndex.from_dir(runbook_dir, FakeEmbedder())
    result = index.search("heap near limit 메모리", top_k=2)
    assert result["mode"] == "hybrid" and result["degraded"] is False
    assert len(result["hits"]) == 2
    top = result["hits"][0]
    assert top["incident_type"] == "memory-leak"
    assert top["bm25_score"] == 1.0 and top["vector_score"] is not None
    assert all(0.0 <= h["bm25_score"] <= 1.0 for h in result["hits"])
    assert index.search("heap", mode="rag")["mode"] == "vector"
    with pytest.raises(ValueError):
        index.search("heap", mode="keyword")


def test_lazy_embedding_built_once(runbook_dir):
    emb = FakeEmbedder()
    index = RunbookIndex.from_dir(runbook_dir, emb)
    assert emb.doc_calls == 0          # 생성 시점에는 임베딩하지 않는다
    index.search("disk", mode="hybrid")
    index.search("heap", mode="hybrid")
    assert emb.doc_calls == 1
    index.reload()
    index.search("disk", mode="vector")
    assert emb.doc_calls == 2


def test_degraded_fallback_to_bm25(runbook_dir):
    index = RunbookIndex.from_dir(runbook_dir, BrokenEmbedder())
    result = index.search("디스크 용량이 부족합니다", mode="hybrid")
    assert result["mode"] == "bm25" and result["degraded"] is True
    assert result["hits"][0]["incident_type"] == "disk-full"
    assert result["hits"][0]["vector_score"] is None
    # bm25 모드는 애초에 임베딩을 부르지 않으므로 degraded가 아니고, 점수 0인 유형은 빠진다
    bm25_only = index.search("x509 인증서", top_k=3, mode="bm25")
    assert bm25_only["degraded"] is False
    assert [h["incident_type"] for h in bm25_only["hits"]] == ["cert-expired"]


def test_disk_cache_roundtrip(tmp_path, monkeypatch):
    calls = []

    def fake_request(self, texts, timeout):
        calls.append(list(texts))
        return [[float(len(t)), 1.0, 0.0] for t in texts]

    monkeypatch.setattr(OllamaEmbedder, "_request", fake_request)
    first = OllamaEmbedder(model="bge-m3:latest", cache_dir=tmp_path).embed(["가나다", "abc", "abc"])
    assert calls == [["가나다", "abc"]]                       # 중복은 한 번만 요청
    assert (tmp_path / "bge-m3_latest.json").exists()
    assert np.allclose(np.linalg.norm(first, axis=1), 1.0)   # 행 단위 L2 정규화

    def fail_request(self, texts, timeout):
        raise AssertionError("캐시에 있으면 요청하면 안 된다")

    monkeypatch.setattr(OllamaEmbedder, "_request", fail_request)
    second = OllamaEmbedder(model="bge-m3:latest", cache_dir=tmp_path).embed(["abc", "가나다"])
    assert np.allclose(second, first[[1, 0]])


def test_query_cache_and_connection_error(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json["input"])
        return httpx.Response(200, json={"embeddings": [[3.0, 4.0]]},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    emb = OllamaEmbedder(cache_dir=None)
    assert np.allclose(emb.embed_query("q"), [0.6, 0.8])
    emb.embed_query("q")
    assert calls == [["q"]]

    def refused(url, json, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", refused)
    with pytest.raises(EmbeddingUnavailable):
        OllamaEmbedder(cache_dir=None).embed(["x"])


def test_query_timeout_and_circuit_breaker(monkeypatch):
    """Ollama가 응답하지 않으면 질의는 짧은 타임아웃으로 한 번만 기다리고, cooldown 동안은 바로 실패."""
    timeouts = []

    def hang(url, json, timeout):
        timeouts.append(timeout)
        raise httpx.ReadTimeout("no answer")

    monkeypatch.setattr(httpx, "post", hang)
    emb = OllamaEmbedder(cache_dir=None, timeout=60.0, query_timeout=2.0, cooldown=30.0)
    for _ in range(3):
        with pytest.raises(EmbeddingUnavailable):
            emb.embed_query("q")
    assert timeouts == [2.0]          # 두 번째부터는 Ollama를 부르지 않는다
    emb._down_until = 0.0             # cooldown이 지나면 다시 시도하고, 청크 임베딩은 긴 타임아웃
    with pytest.raises(EmbeddingUnavailable):
        emb.embed(["x"])
    assert timeouts == [2.0, 60.0]
