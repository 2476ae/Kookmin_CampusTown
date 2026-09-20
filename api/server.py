"""프로토타입 API 서버. `python api/server.py` 로 띄웁니다.

프레임워크 없이 stdlib 만 씁니다. 엔드포인트 2개에 의존성 2개(fastapi+uvicorn)를
더할 이유가 없습니다. 담당 ③는 pip install 없이 바로 띄울 수 있습니다.

S3 결정 — API를 둘로 쪼갭니다
------------------------------
  GET /api/score?ticker=X     단순 JSON · 즉시 (점수 엔진은 0.2ms, LLM 안 씀)
  GET /api/opinion?ticker=X   SSE 스트리밍 · 10~30초 (LLM)

화면은 점수·순위·근거를 즉시 그리고, 글만 그 위에서 흘러나옵니다.
30초 빈 화면이 생기지 않으니 로딩 화면 설계가 거의 필요 없어집니다.
"""
import json
import pathlib
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scoring"))
sys.path.insert(0, str(ROOT / "llm"))

from engine import Engine, default_db  # noqa: E402
import opinion as opinion_mod   # noqa: E402

PORT = 8000
# data/stocks.db 가 있으면 그걸, 없으면 data/fake.db. STOCKS_DB 로 덮어쓸 수 있습니다.
DB = default_db()
ENGINE = Engine(DB)             # 한 번만 적재.


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- 응답 헬퍼 ---------------------------------------------------------
    def _cors(self):
        # 담당 ③가 file:// 나 다른 포트에서 열어도 되게. 프로토타입 한정.
        self.send_header("Access-Control-Allow-Origin", "*")

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # Content-Length 도 chunked 도 없으므로 EOF 로 끝을 알립니다.
        # 안 닫으면 클라이언트가 스트림이 끝난 줄 모르고 계속 기다립니다.
        self.send_header("Connection", "close")
        self.close_connection = True
        self._cors()
        self.end_headers()

    def _sse(self, event, data):
        chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    # -- 라우팅 ------------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        ticker = (parse_qs(url.query).get("ticker") or [""])[0].strip().upper()

        if url.path == "/api/score":
            return self._json(ENGINE.score(ticker)) if ticker else \
                self._json({"error": "ticker_required"}, 400)

        if url.path == "/api/opinion":
            if not ticker:
                return self._json({"error": "ticker_required"}, 400)
            return self._opinion(ticker)

        if url.path in ("/", "/index.html"):
            return self._json({
                "endpoints": ["/api/score?ticker=", "/api/opinion?ticker="],
                "tickers_sample": sorted(ENGINE.by_ticker)[:10],
                "note": "담당 ③: contracts/*.example.json 대신 이 엔드포인트를 쓰면 됩니다.",
            })

        self._json({"error": "not_found"}, 404)

    def _opinion(self, ticker):
        score = ENGINE.score(ticker)
        if score.get("error"):
            return self._json(score, 404)

        self._sse_open()
        try:
            # 점수는 즉시 보냅니다. 화면이 여기서 이미 다 채워집니다.
            self._sse("score", score)

            if score.get("score") is None:
                # 상장폐지 — 의견을 생성하지 않습니다 (S6)
                self._sse("done", {"skipped": "delisted"})
                return

            _, macro, price = opinion_mod.load_context(ENGINE, ticker)
            self._sse("status", {"text": "의견을 쓰는 중…"})
            result = opinion_mod.generate(
                score, macro, price,
                on_progress=lambda t: self._sse("delta", {"text": t}))
            self._sse("opinion", result)
            self._sse("done", {})
        except Exception:
            traceback.print_exc()
            self._sse("error", {"detail": "의견 생성에 실패했습니다"})

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    live = opinion_mod.has_key()
    print(f"http://localhost:{PORT}")
    print(f"  LLM: {opinion_mod.MODEL + ' 실제 호출' if live else '목 모드 ('
                    + opinion_mod.API_KEY_ENV + ' 없음)'}")
    print(f"  DB : {DB.name} ({'실데이터' if DB.name != 'fake.db' else '가짜 데이터'})")
    print(f"  종목 {len(ENGINE.by_ticker)}개 · 예: {sorted(ENGINE.by_ticker)[:5]}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
