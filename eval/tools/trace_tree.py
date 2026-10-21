"""LangSmith에 기록된 요청 하나(run)의 계층 트리를 출력한다.

사용: .venv/bin/python eval/tools/trace_tree.py <langsmith_run_id> [--compact]
  - run id는 /chat 응답의 metadata.langsmith_run_id
  - --compact: 그래프 노드를 감싸는 같은 이름의 래퍼, *_node, _after_* 라우팅 함수는 접어서 보여 준다
.env의 LANGCHAIN_API_KEY를 프로세스 환경으로만 읽고, 키 값은 출력하지 않는다.
"""
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=ROOT / ".env")

from langsmith import Client   # noqa: E402


def duration(run) -> str:
    if run.start_time and run.end_time:
        return f"{(run.end_time - run.start_time).total_seconds():.2f}s"
    return "-"


def hidden(run, parent, compact: bool) -> bool:
    if not compact or parent is None:
        return False
    return run.name == parent.name or run.name.endswith("_node") or run.name.startswith("_after")


def walk(run, depth=0, parent=None, compact=False):
    if hidden(run, parent, compact):     # 접은 노드는 자식을 같은 깊이로 올린다
        for child in sorted(run.child_runs or [], key=lambda r: r.start_time):
            walk(child, depth, run, compact)
        return
    print(f"{'  ' * depth}{'└ ' if depth else ''}{run.name}  [{run.run_type}]  {duration(run)}")
    for child in sorted(run.child_runs or [], key=lambda r: r.start_time):
        walk(child, depth + 1, run, compact)


if __name__ == "__main__":
    run_id, compact = sys.argv[1], "--compact" in sys.argv[2:]
    client = Client()
    for _ in range(10):          # 서버 종료 직후면 업로드가 끝날 때까지 잠깐 기다린다
        try:
            root = client.read_run(run_id, load_child_runs=True)
            break
        except Exception:
            time.sleep(3)
    else:
        sys.exit(f"run {run_id}를 읽지 못했습니다")
    walk(root, compact=compact)
