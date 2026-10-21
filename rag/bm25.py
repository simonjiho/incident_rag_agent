"""한국어·영어 혼합 텍스트용 토크나이저 + BM25(Okapi) 직접 구현.

형태소 분석기 없이 한글은 어절 + 글자 bigram으로 쪼갠다 — "인증서가"와 "인증서"가
bigram("인증", "증서")을 공유해 조사가 붙어도 매칭된다.
"""
import math
import re
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+|[가-힣]+")


def tokenize(text: str) -> list[str]:
    tokens = []
    for word in _TOKEN.findall(text.lower()):
        tokens.append(word)
        if "가" <= word[0] <= "힣" and len(word) > 2:   # 2글자 어절은 bigram이 자기 자신
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
    return tokens


class BM25Okapi:
    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.term_freqs = [Counter(doc) for doc in corpus]
        self.doc_lens = [len(doc) for doc in corpus]
        self.avgdl = sum(self.doc_lens) / len(corpus) if corpus else 0.0
        doc_freq = Counter(term for tf in self.term_freqs for term in tf)
        n = len(corpus)
        # +1을 더한 IDF (Lucene 방식) — 거의 모든 문서에 있는 단어도 음수가 되지 않게
        self.idf = {t: math.log((n - df + 0.5) / (df + 0.5) + 1) for t, df in doc_freq.items()}

    def get_scores(self, query: list[str]) -> list[float]:
        scores = []
        for tf, dl in zip(self.term_freqs, self.doc_lens):
            norm = self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            scores.append(sum(self.idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm)
                              for t in query if t in tf))
        return scores
