"""web_demo_server.py — all_total_vue 에이전트와 브라우저 채팅창으로 대화해보는 데모.

web_chat.html이 바닐라 JS 대신 Vue 3(CDN 글로벌 빌드, 별도 빌드 단계 없음)로 되어
있다는 점만 all_total과 다르다. server.py(8100, 기존 통합 테스트 서버)는 전혀
건드리지 않는다. 이 스크립트는
- web_chat.html 한 장을 같은 출처(origin)로 띄우고
- /chat/stream·/chat/resume(사람 승인 후 재개) 요청을 그대로 8100으로 중계(프록시)해서

브라우저의 CORS 제약 없이 실제 에이전트와 대화할 수 있게 해주는 아주 얇은 레이어다.
server.py는 평소처럼(fake/ollama/openai 아무 백엔드로나) 8100에 미리 띄워 놓아야 한다.

실행:
    python3 web_demo_server.py        # http://localhost:8600 에서 채팅 화면 오픈
"""
import os
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

BACKEND = os.environ.get("BACKEND_URL", "http://127.0.0.1:8100")   # 백엔드 주소 (기본 8100)
PORT = int(os.environ.get("FRONTEND_PORT", "8600"))
HERE = Path(__file__).parent

app = FastAPI()


@app.get("/")
async def index():
    return FileResponse(HERE / "web_chat.html")


@app.get("/health")
async def proxy_health():
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BACKEND}/health")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/chat")
async def proxy_chat(req: Request):
    body = await req.body()
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BACKEND}/chat", content=body,
                              headers={"content-type": "application/json"})
    return JSONResponse(r.json(), status_code=r.status_code)


def _relay(path: str, params: dict) -> StreamingResponse:
    async def relay():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", f"{BACKEND}{path}", params=params) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk

    return StreamingResponse(relay(), media_type="text/event-stream")


@app.get("/chat/stream")
async def proxy_stream(req: Request):
    # message·thread_id·hitl 등 쿼리 파라미터를 그대로 넘긴다
    params = dict(req.query_params)
    params.setdefault("thread_id", "web-demo")
    return _relay("/chat/stream", params)


@app.get("/chat/resume")
async def proxy_resume(req: Request):
    """사람 승인(HITL) — 승인/거부 후 이어지는 실행을 SSE로 중계."""
    return _relay("/chat/resume", dict(req.query_params))


@app.post("/chat/approve")
async def proxy_approve(req: Request):
    body = await req.body()
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(f"{BACKEND}/chat/approve", content=body,
                              headers={"content-type": "application/json"})
    return JSONResponse(r.json(), status_code=r.status_code)


if __name__ == "__main__":
    print(f"브라우저에서 http://localhost:{PORT} 을 열어보세요 (server.py가 {BACKEND}에 떠 있어야 합니다)")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
