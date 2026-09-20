"""종합의견 생성. 점수 엔진의 결과를 읽어 문장으로 풀어씁니다.

왜 tool calling을 안 쓰는가
--------------------------
LLM이 필요로 하는 데이터는 점수·거시·가격 셋이고 매번 전부 필요합니다.
탐색할 게 없으니 LLM이 "무엇을 가져올지" 결정할 이유가 없습니다.
셋을 미리 가져와 프롬프트에 넣고 호출 한 번으로 끝냅니다.
(S7 꼬리질문에서 다른 종목을 묻게 되면 그때 툴이 필요해집니다.)

점수는 여기서 만들지 않습니다. 이미 결정적으로 계산돼 있고, LLM은 그걸 읽기만 합니다.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scoring"))
from engine import Engine  # noqa: E402

PROVIDER = "openai"
MODEL = "gpt-5.6"
API_KEY_ENV = "OPENAI_API_KEY"

# 계약(contracts/README.md)에 있는 것만. LLM이 만들어낸 ID는 버립니다.
FLAG_IDS = {"score.overall", "flag.delisting", "flag.ma_late"}

OPINION_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "sentences": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "evidence": {"type": "array", "items": {"type": "string"}},
                                "terms": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["text", "evidence", "terms"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "title", "sentences"],
                "additionalProperties": False,
            },
        },
        "glossary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "plain": {"type": "string"},
                    "formula": {"type": ["string", "null"]},
                },
                "required": ["term", "plain", "formula"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "sections", "glossary"],
    "additionalProperties": False,
}

SYSTEM = """당신은 주식이 어려운 사람에게 미국 주식 종목을 설명합니다.

읽는 사람은 재무제표를 한 번도 본 적이 없습니다. PER이 무엇인지 모릅니다.

규칙:
1. 점수를 다시 계산하거나 바꾸지 마세요. 주어진 점수는 이미 확정된 것입니다.
2. 모든 문장에 evidence ID를 답니다. 주어진 ID 목록 밖의 것을 만들어내지 마세요.
   근거를 댈 수 없는 문장은 아예 쓰지 마세요.
3. 전문용어를 쓰면 그 문장의 terms 에 넣고, glossary 에 쉬운 말로 풀어 쓰세요.
   "PER은 주가수익비율입니다" 는 설명이 아닙니다. 어려운 말을 어려운 말로 바꾼 것뿐입니다.
   "지금 주가가 이 회사 1년 이익의 몇 배인지" 처럼 초등학생도 알아들을 말로 쓰세요.
4. 매수·매도를 권하지 마세요. 이 회사가 어떤 상태인지만 말하세요.
5. 계산할 수 없었던 지표(status가 insufficient_data)를 '나쁘다'고 쓰지 마세요.
   데이터가 없는 것과 수치가 낮은 것은 다릅니다. 필요하면 "알 수 없다"고 쓰세요.
6. 축이 빠진 채로 낸 점수라면(score_basis) 그 사실을 한 문장으로 알려주세요.

섹션은 overall(한 줄로 말하면) · strength(잘하는 것) · concern(걸리는 것) ·
context(지금 시장은) 순서로, 각 2~4문장."""


def build_prompt(score, macro, price):
    """LLM에 넣을 입력. 점수는 이미 계산된 것을 그대로 넣습니다."""
    allowed = sorted(collect_evidence_ids(score))
    return (
        f"## 종목\n{score['name']} ({score['ticker']})\n\n"
        f"## 점수 (확정됨 · 바꾸지 마세요)\n```json\n"
        f"{json.dumps(score, ensure_ascii=False, indent=2)}\n```\n\n"
        f"## 거시 지표 (참고용 · 종목 점수에는 안 들어갑니다)\n```json\n"
        f"{json.dumps(macro, ensure_ascii=False)}\n```\n\n"
        f"## 가격·이동평균\n```json\n{json.dumps(price, ensure_ascii=False)}\n```\n\n"
        f"## 쓸 수 있는 evidence ID (이 목록 밖은 금지)\n"
        + "\n".join(f"- {i}" for i in allowed)
    )


def collect_evidence_ids(score):
    ids = set(FLAG_IDS)
    ids |= {i["id"] for a in score.get("axes", []) for i in a["indicators"]}
    return ids


def sanitize(opinion, score):
    """계약에 없는 evidence ID를 버립니다.

    contracts/README.md 는 "UI가 모르는 ID를 무시한다"고 적었지만, 서버에서 거르는 게
    맞습니다. 근거 없는 문장이 화면까지 가면 초보자는 그게 근거 없는 줄 모릅니다.
    """
    allowed = collect_evidence_ids(score)
    dropped = []
    for section in opinion.get("sections", []):
        for s in section.get("sentences", []):
            bad = [e for e in s.get("evidence", []) if e not in allowed
                   and not e.startswith("macro.")]
            if bad:
                dropped += bad
                s["evidence"] = [e for e in s["evidence"] if e not in bad]
    opinion["_dropped_evidence"] = dropped
    opinion["disclaimer"] = ("정보 제공 목적이며 투자 자문이 아닙니다. "
                            "투자 판단과 그 결과의 책임은 이용자에게 있습니다.")
    return opinion


# ---------------------------------------------------------------- 목 모드
def mock_opinion(score):
    """API 키 없이도 담당 ③가 서버를 띄울 수 있게. 실제 LLM 호출 아님."""
    n = score.get("peer_group", {}).get("n", 0)
    basis = score.get("score_basis", {})
    partial = basis.get("axes_used", 4) < basis.get("axes_total", 4)
    return sanitize({
        "headline": f"[목 데이터] {score['name']} 종합의견입니다.",
        "sections": [
            {"id": "overall", "title": "한 줄로 말하면", "sentences": [
                {"text": f"같은 업종 {n}개 중 {score.get('rank_text', '-')}입니다.",
                 "evidence": ["score.overall"], "terms": []},
            ] + ([{"text": f"다만 {basis['axes_total']}개 축 중 {basis['axes_used']}개로만 "
                           f"낸 점수라 참고로만 보세요.",
                   "evidence": ["score.overall"], "terms": []}] if partial else [])},
            {"id": "concern", "title": "걸리는 것", "sentences": [
                {"text": f"이것은 목 데이터입니다. {API_KEY_ENV} 를 설정하면 "
                         f"{MODEL} 이 여기에 글을 씁니다.",
                 "evidence": ["score.overall"], "terms": []},
            ]},
        ],
        "glossary": [{"term": "목 데이터", "plain": "실제가 아닌 자리 채우기용 가짜 데이터.",
                      "formula": None}],
    }, score)


# ---------------------------------------------------------------- 실제 호출
def load_dotenv(path=None):
    """프로젝트 루트의 .env 를 os.environ 에 채웁니다.

    python-dotenv 를 안 쓰는 이유: 키 하나 읽자고 의존성을 늘릴 이유가 없습니다.
    이미 설정된 환경변수가 이깁니다 (.env 가 셸 설정을 덮으면 디버깅이 괴로워집니다).
    """
    path = pathlib.Path(path or pathlib.Path(__file__).resolve().parent.parent / ".env")
    if not path.exists():
        return {}
    loaded = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.removeprefix("export ").split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        loaded[key] = val
        os.environ.setdefault(key, val)
    return loaded


load_dotenv()


def has_key():
    """실제 키가 있는지. .env.example 의 플레이스홀더는 키가 아닙니다.

    'sk-...' 를 키로 받아들이면 목 모드로 안 떨어지고 인증 에러로 터집니다.
    .env.example 만 복사하고 안 채운 사람이 반드시 생깁니다.
    """
    key = (os.environ.get(API_KEY_ENV) or "").strip()
    return bool(key) and "..." not in key


def require_sdk():
    """openai >= 1.0 을 확인합니다.

    0.28 같은 구버전이 깔려 있으면 `pip install openai` 는 "already satisfied" 라며
    아무것도 안 하고, 나중에 ImportError 만 납니다. 여기서 먼저 잡습니다.
    """
    import openai

    if not hasattr(openai, "OpenAI"):
        import importlib.metadata as md
        try:
            found = md.version("openai")
        except Exception:
            found = "?"
        raise RuntimeError(
            f"openai {found} 은 너무 낡았습니다. Responses API 에는 1.0 이상이 필요합니다.\n"
            f"  pip install -U openai\n"
            f"  (-U 를 빼면 '이미 설치됨' 이라며 아무것도 하지 않습니다)")
    return openai.OpenAI


def generate(score, macro, price, on_progress=None):
    """OpenAI Responses API로 의견을 생성합니다.

    on_progress(텍스트조각) 이 SSE의 delta 이벤트로 흘러나갑니다.
    구조화 출력이라 흘러나오는 건 JSON 문자열입니다 — UI는 delta 를 진행 표시로만 쓰고,
    렌더링은 마지막 opinion 이벤트의 파싱된 객체로 하세요.

    키가 없으면 목 모드로 떨어집니다.
    """
    if not has_key():
        return mock_opinion(score)

    client = require_sdk()()
    text, refusal = [], []
    stream = client.responses.create(
        model=MODEL,
        instructions=SYSTEM,
        input=build_prompt(score, macro, price),
        # strict 모드는 모든 프로퍼티가 required 여야 합니다. OPINION_SCHEMA 는 그렇게 짜여 있습니다.
        text={"format": {"type": "json_schema", "name": "stock_opinion",
                         "strict": True, "schema": OPINION_SCHEMA}},
        stream=True,
    )
    for event in stream:
        kind = getattr(event, "type", "")
        if kind == "response.output_text.delta":
            text.append(event.delta)
            if on_progress:
                on_progress(event.delta)
        elif kind == "response.refusal.delta":
            refusal.append(event.delta)

    if refusal:
        return {"error": "refusal", "detail": "".join(refusal)}
    return sanitize(json.loads("".join(text)), score)


def load_context(engine, ticker):
    """점수 + 거시 + 가격. 셋 다 엔진이 시작할 때 올려둔 메모리에서 나옵니다.

    여기서 engine.conn 을 쓰면 안 됩니다 — 서버가 요청마다 스레드를 바꾸는데
    sqlite3 연결은 생성한 스레드에서만 유효합니다.
    """
    return engine.score(ticker), engine.macro, engine.price.get(ticker) or {}


if __name__ == "__main__":
    root = pathlib.Path(__file__).resolve().parent.parent
    e = Engine(root / "data" / "fake.db")
    s, m, p = load_context(e, sys.argv[1] if len(sys.argv) > 1 else "35010")
    print(json.dumps(generate(s, m, p), ensure_ascii=False, indent=2))
