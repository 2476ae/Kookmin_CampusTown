"""점수 엔진 자체 검사. `python scoring/test_engine.py`

프레임워크 없습니다. 깨지면 assert가 터집니다.

여기 있는 것들은 전부 "틀리면 초보자가 돈을 잃는" 성질의 검사입니다.
특히 적자·자본잠식 종목이 높은 백분위를 받지 않는지가 핵심입니다.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fake_db                                    # noqa: E402
from engine import INDICATORS, MIN_SAMPLE, Engine  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "fake.db"

if not DB.exists():
    fake_db.build(DB)
E = Engine(DB)

# fake_db.py 가 결정적으로 배치한 케이스들
NORMAL, NEW_LISTING, LOSS = "35010", "35000", "35001"
DELISTED, DELISTING_SOON, NO_PRICE, NEG_EQUITY = "35003", "35004", "35005", "35006"
TINY_SIC, MID_SIC, ALL_MARKET = "38000", "36000", "09000"

passed = []


def check(name):
    def wrap(fn):
        fn()
        passed.append(name)
        return fn
    return wrap


def indicators(result):
    return {i["id"]: i for a in result["axes"] for i in a["indicators"]}


# ---------------------------------------------------------------- 결정성
@check("결정성 — 같은 DB면 항상 같은 점수")
def _():
    a = json.dumps(E.score(NORMAL), sort_keys=True, ensure_ascii=False)
    b = json.dumps(Engine(DB).score(NORMAL), sort_keys=True, ensure_ascii=False)
    assert a == b, "같은 입력에 다른 출력 — 금융 서비스에서 이건 버그입니다"


# ------------------------------------------------- 분모가 0 이하인 경우
@check("적자 기업의 PER 백분위는 0 — 음수 PER이 '제일 싸다'로 읽히면 안 됩니다")
def _():
    bad = []
    for ticker, cik in E.by_ticker.items():
        if E.company[cik]["delisted_date"]:
            continue
        net = E.score(ticker)
        if net.get("score") is None:
            continue
        ind = indicators(net).get("valuation.per")
        if ind is None or ind["status"] != "ok":
            continue
        # 엔진이 WORST로 판정했으면 value 가 None 이고 percentile 이 0 이어야 합니다
        if ind["value"] is None and ind["percentile"] != 0:
            bad.append((ticker, ind["percentile"]))
    assert not bad, f"적자인데 PER 백분위가 0이 아닌 종목: {bad[:5]}"


@check("자본잠식 — ROE·부채비율·PBR이 전부 백분위 0")
def _():
    ind = indicators(E.score(NEG_EQUITY))
    for key in ("profitability.roe", "stability.debt_to_equity", "valuation.pbr"):
        assert ind[key]["percentile"] == 0, f"{key} 가 자본잠식인데 {ind[key]['percentile']}점"
        assert ind[key]["value"] is None, f"{key} 에 뒤집힌 값이 그대로 남아 있습니다"


# ---------------------------------------------------------------- 방향
@check("역방향 지표 — 값이 낮을수록 백분위가 높습니다")
def _():
    lower = [i.key for i in INDICATORS if i.direction == "lower_better"]
    assert lower, "lower_better 지표가 하나도 없습니다"
    for ind_id in lower:
        rows = []
        for ticker, cik in E.by_ticker.items():
            v = E.values[cik][ind_id]
            if isinstance(v, (int, float)) and not E.company[cik]["delisted_date"]:
                r = indicators(E.score(ticker)).get(f"stability.{ind_id}") \
                    or indicators(E.score(ticker)).get(f"valuation.{ind_id}")
                if r and r["status"] == "ok" and r["value"] is not None:
                    rows.append((v, r["percentile"]))
        if len(rows) < 10:
            continue
        rows.sort()
        lo_half = sum(p for _, p in rows[:len(rows) // 3])
        hi_half = sum(p for _, p in rows[-len(rows) // 3:])
        assert lo_half > hi_half, f"{ind_id}: 값이 큰 쪽이 더 높은 백분위 — 방향이 뒤집혔습니다"


# ------------------------------------------------------- S5 결측 처리
@check("S5 — 신규 상장(2년치)은 성장성 축이 빠지고 3축으로 평균")
def _():
    r = E.score(NEW_LISTING)
    growth = next(a for a in r["axes"] if a["id"] == "growth")
    assert growth["status"] == "insufficient_data", "3년 CAGR이 없는데 성장성 축이 살아 있습니다"
    assert growth["score"] is None
    assert r["score_basis"]["axes_used"] == 3, r["score_basis"]
    assert r["score"] is not None, "3축이면 점수를 내야 합니다 (절반 이상 남음)"


@check("S5 — 시총 없음은 밸류에이션 축이 통째로 빠집니다")
def _():
    r = E.score(NO_PRICE)
    val = next(a for a in r["axes"] if a["id"] == "valuation")
    assert val["status"] == "insufficient_data"
    assert all(i["percentile"] is None for i in val["indicators"])


@check("S5 — 있는 축만 평균. 산술이 실제로 맞습니다")
def _():
    for ticker in (NORMAL, NEW_LISTING, LOSS, NO_PRICE, NEG_EQUITY):
        r = E.score(ticker)
        for a in r["axes"]:
            got = [i["percentile"] for i in a["indicators"] if i["status"] == "ok"]
            if a["status"] == "ok":
                assert a["score"] == round(sum(got) / len(got)), f"{ticker} {a['id']} 축 평균 불일치"
            else:
                assert len(got) * 2 < len(a["indicators"]), f"{ticker} {a['id']} 살릴 수 있는데 버렸습니다"
        ok = [a["score"] for a in r["axes"] if a["status"] == "ok"]
        assert r["score"] == round(sum(ok) / len(ok)), f"{ticker} 종합 점수 불일치"


# ------------------------------------------------------- S6 상장폐지
@check("S6 — 이미 폐지된 종목은 점수를 내지 않습니다")
def _():
    r = E.score(DELISTED)
    assert r["score"] is None, "살 수 없는 주식에 점수를 붙이면 살 수 있는 것처럼 보입니다"
    assert r["axes"] == []
    assert r["flags"][0]["severity"] == "critical"


@check("S6 — 폐지 예정(거래 중)은 점수를 내되 최대 경고")
def _():
    r = E.score(DELISTING_SOON)
    assert r["score"] is not None, "아직 거래되는 종목은 판단이 필요합니다"
    assert r["delisting"]["status"] == "delisting_soon"
    sev = {f["id"]: f["severity"] for f in r["flags"]}
    assert sev.get("flag.delisting") == "critical", sev


@check("상장폐지 종목은 비교군에서 빠집니다")
def _():
    peers = E.pick_peer_group("3571")[2]
    assert all(not E.company[c]["delisted_date"] for c in peers)


# ------------------------------------------------------- 폴백 사다리
@check("폴백 — 사다리 네 칸이 전부 실제로 밟힙니다")
def _():
    # 처음엔 가짜 DB 의 SIC 가 2자리 접두사를 공유하지 않아서 3자리·2자리 칸이
    # 한 번도 실행되지 않았습니다. 실데이터(3571/3572/3576)에서 처음 돌 뻔했습니다.
    seen = {}
    for ticker in E.by_ticker:
        pg = E.score(ticker).get("peer_group")
        if pg:
            seen.setdefault(pg["level"], ticker)
    for level in (4, 3, 2, 0):
        assert level in seen, f"폴백 {level}단계가 한 번도 실행되지 않습니다"


@check("폴백 — 단계마다 라벨이 달라집니다 (신뢰도가 다르니 화면에서 구분돼야 합니다)")
def _():
    for ticker, level, mark in ((TINY_SIC, 2, "대분류"), (MID_SIC, 3, "유사업종"),
                                (ALL_MARKET, 0, "전체 시장")):
        pg = E.score(ticker)["peer_group"]
        assert pg["level"] == level, f"{ticker}: level {pg['level']} (기대 {level})"
        assert mark in pg["label"], f"{ticker}: {pg['label']}"
        assert pg["n"] > MIN_SAMPLE


@check("폴백 — 모집단은 종목당 하나. 지표마다 다르면 '58개 중 26위'가 성립 안 합니다")
def _():
    r = E.score(NORMAL)
    n = r["peer_group"]["n"]
    for i in indicators(r).values():
        if i["status"] == "ok":
            assert sum(i["peer_deciles"]) <= n, f"{i['id']} 가 다른 모집단을 씁니다"


# --------------------------------------------------------- DB 연결
@check("DB — 못 쓸 상태면 알 수 없는 SQL 에러 대신 할 일을 알려줍니다")
def _():
    import sqlite3
    import tempfile

    schema = (ROOT / "contracts" / "db_schema.sql").read_text(encoding="utf-8")

    def expect(setup, must_contain):
        d = pathlib.Path(tempfile.mkdtemp()) / "t.db"
        setup(d)
        try:
            Engine(d)
        except RuntimeError as ex:
            assert must_contain in str(ex), f"{must_contain!r} 가 없습니다 / 실제: {ex!r}"
        except Exception as ex:
            raise AssertionError(f"RuntimeError 가 아니라 {type(ex).__name__}: {ex}")
        else:
            raise AssertionError("못 쓸 DB 인데 통과했습니다")

    # 담당 ① 이 ETL 도중이면 파일은 있는데 테이블이 없습니다. 반드시 밟는 상황입니다.
    expect(lambda p: None, "DB 파일이 없습니다")
    expect(lambda p: p.write_bytes(b""), "테이블이 없습니다")

    def schema_only(p):
        c = sqlite3.connect(p)
        c.executescript(schema)
        c.close()
    expect(schema_only, "종목이 하나도 없습니다")


@check("DB — 적재 후 연결을 닫습니다 (스레드 사고 방지 + 파일 잠금 해제)")
def _():
    import shutil
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp()) / "copy.db"
    shutil.copy(DB, tmp)
    e = Engine(tmp)
    assert not hasattr(e, "conn"), (
        "conn 이 남아 있습니다 — 다른 스레드에서 쓰면 터집니다 (실제로 한 번 터졌습니다)")
    tmp.unlink()   # 잠겨 있으면 Windows 에서 PermissionError
    assert e.score(sorted(e.by_ticker)[0])["score"] is not None, "파일을 지웠는데 엔진이 죽습니다"


# ------------------------------------------------------- 계약 준수
@check("계약 — 출력 키 구조가 contracts/score.example.json 과 같습니다")
def _():
    ex = json.loads((ROOT / "contracts" / "score.example.json").read_text(encoding="utf-8"))
    got = E.score(NORMAL)
    # _ 로 시작하는 키는 계약이 아니라 내부 표시입니다 (_note, _dropped_evidence).
    missing = {k for k in set(ex) - set(got) if not k.startswith("_")}
    assert not missing, f"계약에 있는데 엔진이 안 내는 키: {missing}"
    ax_missing = set(ex["axes"][0]) - set(got["axes"][0])
    assert not ax_missing, f"축에서 빠진 키: {ax_missing}"
    i_missing = set(ex["axes"][0]["indicators"][0]) - set(got["axes"][0]["indicators"][0])
    assert not i_missing, f"지표에서 빠진 키: {i_missing}"


@check("계약 — 근거 ID가 contracts/README.md 목록과 일치")
def _():
    doc = (ROOT / "contracts" / "README.md").read_text(encoding="utf-8")
    for ind_id in indicators(E.score(NORMAL)):
        assert f"`{ind_id}`" in doc, f"{ind_id} 가 계약 문서의 근거 ID 목록에 없습니다"


@check("rank_text 가 점수와 모순되지 않습니다")
def _():
    for ticker in (NORMAL, NEW_LISTING, LOSS, NEG_EQUITY):
        r = E.score(ticker)
        n, score = r["peer_group"]["n"], r["score"]
        assert f"{n}개" in r["rank_text"] and f"상위 {100 - score}%" in r["rank_text"], r["rank_text"]


@check("없는 종목은 조용히 에러를 냅니다 (예외로 죽지 않음)")
def _():
    assert E.score("NOPE")["error"] == "not_found"


if __name__ == "__main__":
    for name in passed:
        print(f"  ok  {name}")
    print(f"\n{len(passed)} passed")
