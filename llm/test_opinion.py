"""의견 생성 + API 자체 검사. `python llm/test_opinion.py`

API 키 없이 검사할 수 있는 것만 봅니다. 실제 LLM 호출 경로는 키가 생긴 뒤에
누군가 한 번 돌려봐야 합니다 — 아직 아무도 안 돌려봤습니다.

여기서 제일 중요한 건 sanitize 입니다. LLM이 없는 근거를 지어내도
화면까지 가면 안 됩니다. 초보자는 그게 근거 없는 줄 모릅니다.
"""
import json
import pathlib
import sys
import threading
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scoring"))
sys.path.insert(0, str(ROOT / "llm"))

import fake_db          # noqa: E402
import opinion as O     # noqa: E402
from engine import Engine  # noqa: E402

DB = ROOT / "data" / "fake.db"
if not DB.exists():
    fake_db.build(DB)
E = Engine(DB)

NORMAL, NEW_LISTING, DELISTED = "35010", "35000", "35003"
passed = []


def check(name):
    def wrap(fn):
        fn()
        passed.append(name)
        return fn
    return wrap


# ------------------------------------------------- 근거 위조 방어 (핵심)
@check("sanitize — LLM이 지어낸 evidence ID를 버립니다")
def _():
    score = E.score(NORMAL)
    fake = {"sections": [{"id": "x", "title": "x", "sentences": [
        {"text": "진짜 근거", "evidence": ["profitability.operating_margin"], "terms": []},
        {"text": "지어낸 근거", "evidence": ["profitability.secret_sauce"], "terms": []},
        {"text": "섞인 것", "evidence": ["valuation.per", "growth.made_up"], "terms": []},
    ]}], "glossary": []}
    out = O.sanitize(fake, score)
    sents = out["sections"][0]["sentences"]
    assert sents[0]["evidence"] == ["profitability.operating_margin"]
    assert sents[1]["evidence"] == [], "지어낸 ID가 살아남았습니다"
    assert sents[2]["evidence"] == ["valuation.per"], sents[2]["evidence"]
    assert set(out["_dropped_evidence"]) == {"profitability.secret_sauce", "growth.made_up"}


@check("sanitize — macro.* 는 통과시킵니다 (거시는 점수에 없지만 근거로는 유효)")
def _():
    out = O.sanitize({"sections": [{"id": "c", "title": "c", "sentences": [
        {"text": "금리", "evidence": ["macro.fed_funds"], "terms": []}]}], "glossary": []},
        E.score(NORMAL))
    assert out["sections"][0]["sentences"][0]["evidence"] == ["macro.fed_funds"]


@check("sanitize — 면책 문구를 서버가 붙입니다 (LLM에 맡기지 않음)")
def _():
    out = O.sanitize({"sections": [], "glossary": []}, E.score(NORMAL))
    assert "투자 자문이 아닙니다" in out["disclaimer"]


# ------------------------------------------------------------ 프롬프트
@check("프롬프트 — 허용 ID 목록이 실제 점수에서 나옵니다")
def _():
    score = E.score(NEW_LISTING)
    prompt = O.build_prompt(score, {"FEDFUNDS": 4.25}, {})
    for i in O.collect_evidence_ids(score):
        assert f"- {i}" in prompt, f"{i} 가 허용 목록에 없습니다"
    assert "바꾸지 마세요" in prompt, "점수를 바꾸지 말라는 지시가 빠졌습니다"


@check("프롬프트 — 결측 축이 있으면 그 사실이 점수 JSON에 실려 갑니다")
def _():
    prompt = O.build_prompt(E.score(NEW_LISTING), {}, {})
    assert "insufficient_data" in prompt
    assert '"axes_used": 3' in prompt


# -------------------------------------------------------------- 목 모드
@check("목 모드 — 키 없이도 계약 구조에 맞는 결과가 나옵니다")
def _():
    ex = json.loads((ROOT / "contracts" / "opinion.example.json").read_text(encoding="utf-8"))
    got = O.mock_opinion(E.score(NORMAL))
    for key in ("headline", "sections", "glossary", "disclaimer"):
        assert key in got, f"목 출력에 {key} 가 없습니다"
    assert set(ex["sections"][0]["sentences"][0]) - set(got["sections"][0]["sentences"][0]) \
        <= {"terms"}, "문장 키 구조가 계약과 다릅니다"


@check("목 모드 — 축이 빠진 종목이면 그 사실을 문장으로 알립니다")
def _():
    texts = " ".join(s["text"] for sec in O.mock_opinion(E.score(NEW_LISTING))["sections"]
                     for s in sec["sentences"])
    assert "4개 축 중 3개" in texts, texts


@check("generate — 키가 없으면 목 모드로 떨어지고 예외로 죽지 않습니다")
def _():
    s, m, p = O.load_context(E, NORMAL)
    assert "headline" in O.generate(s, m, p)


# ------------------------------------------------------------ 스키마
@check("스키마 — OpenAI strict 모드 요건을 만족합니다")
def _():
    # strict 모드는 additionalProperties:false 와 "모든 프로퍼티가 required" 를 요구합니다.
    # 나중에 필드 하나 늘리고 required 에 안 넣으면 런타임에 400 이 납니다.
    def walk(node, path="root"):
        if isinstance(node, dict):
            if node.get("type") == "object":
                props = set(node.get("properties", {}))
                req = set(node.get("required", []))
                assert node.get("additionalProperties") is False, f"{path}: additionalProperties"
                assert props == req, f"{path}: required 누락 {props - req}"
            for k, v in node.items():
                walk(v, f"{path}.{k}")
    walk(O.OPINION_SCHEMA)


@check("제공자 설정이 한 군데에 모여 있습니다 (교체 비용)")
def _():
    assert O.PROVIDER == "openai" and O.API_KEY_ENV == "OPENAI_API_KEY"
    src = (ROOT / "llm" / "opinion.py").read_text(encoding="utf-8")
    assert "anthropic" not in src.lower(), "Anthropic 잔재가 남아 있습니다"
    assert "claude" not in src.lower(), "Anthropic 잔재가 남아 있습니다"


# --------------------------------------------------------------- API
@check("API — /api/score 는 즉시 JSON, /api/opinion 은 SSE")
def _():
    import api.server as srv  # noqa: E402
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/score?ticker={NORMAL}", timeout=10) as r:
            score = json.loads(r.read())
        assert score["score"] is not None and score["ticker"] == NORMAL

        with urllib.request.urlopen(f"{base}/api/opinion?ticker={NORMAL}", timeout=30) as r:
            assert r.headers["Content-Type"].startswith("text/event-stream")
            body = r.read().decode("utf-8")
        events = [ln[7:] for ln in body.splitlines() if ln.startswith("event: ")]
        assert events[0] == "score", f"점수가 제일 먼저 가야 합니다: {events[:3]}"
        assert "opinion" in events and events[-1] == "done", events

        # S6 — 상폐 종목은 의견 생성을 건너뜁니다
        with urllib.request.urlopen(f"{base}/api/opinion?ticker={DELISTED}", timeout=15) as r:
            body = r.read().decode("utf-8")
        assert '"skipped": "delisted"' in body, body[-200:]
        assert "event: opinion" not in body, "상폐 종목에 의견을 붙였습니다"
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    for name in passed:
        print(f"  ok  {name}")
    print(f"\n{len(passed)} passed")
    print("\n  !!  실제 LLM 호출 경로는 아직 아무도 안 돌려봤습니다 (API 키 없음)")
