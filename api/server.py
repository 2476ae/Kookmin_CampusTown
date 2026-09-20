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

# 한글 콘솔(cp949)에서 한글·em-dash 가 깨지거나 UnicodeEncodeError 로 죽습니다.
# 출력이 파일·파이프로 넘어갈 때도 마찬가지라 여기서 한 번에 고정합니다.
for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scoring"))
sys.path.insert(0, str(ROOT / "llm"))

from engine import Engine, default_db  # noqa: E402
import opinion as opinion_mod   # noqa: E402

PORT = 8000
WEB = ROOT / "web"          # 담당 ③ 의 화면. 있으면 같이 서빙합니다.
MIME = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
        ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".woff2": "font/woff2"}
# data/stocks.db 가 있으면 그걸, 없으면 data/fake.db. STOCKS_DB 로 덮어쓸 수 있습니다.
DB = default_db()
try:
    ENGINE = Engine(DB)         # 한 번만 적재.
except RuntimeError as _e:
    # 스택 트레이스는 개발 지식 없는 사람에게 최악입니다. 할 일만 보여줍니다.
    print("\n서버를 띄울 수 없습니다.\n")
    print(_e)
    print()
    raise SystemExit(1)


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

        # 나머지는 web/ 에서 찾습니다. 서버 하나로 화면까지 뜨게 하려는 것 —
        # 담당 ③ 가 index.html 을 따로 열 필요가 없습니다.
        if self._serve_static(url.path):
            return

        if url.path in ("/", "/index.html"):
            return self._json({
                "endpoints": ["/api/score?ticker=", "/api/opinion?ticker="],
                "tickers_sample": sorted(ENGINE.by_ticker)[:10],
                "note": "web/index.html 을 만들면 여기서 바로 뜹니다.",
            })

        self._json({"error": "not_found"}, 404)

    def _serve_static(self, path):
        if not WEB.is_dir():
            return False
        rel = path.lstrip("/") or "index.html"
        target = (WEB / rel).resolve()
        # 경로 탈출 방지 — ../.. 로 저장소 밖 파일을 읽히면 안 됩니다
        if not target.is_relative_to(WEB.resolve()) or not target.is_file():
            return False
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(target.suffix, "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")   # 프로토타입: 새로고침이 항상 먹게
        self._cors()
        self.end_headers()
        self.wfile.write(body)
        return True

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
    print(f"  화면: {'web/ 서빙 중' if WEB.is_dir() else 'web/ 없음 (API 만)'}")
    if ENGINE.meta.get("price_source") == "dummy":
        print("  !! 가격이 샘플입니다 — PER·PBR 은 진짜가 아닙니다 (화면에 경고 배지가 뜹니다)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
