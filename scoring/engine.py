"""점수 엔진. 결정적 — 같은 DB면 항상 같은 점수. LLM 안 씁니다.

출력은 contracts/score.example.json 과 같은 구조입니다.

규칙 (contracts/README.md 참조):
  S5  있는 것만 평균 낸다. 빠진 건 명시한다. 절반 이상 빠지면 점수를 내지 않는다.
      지표 층·축 층에 똑같이 적용.
  S6  이미 폐지된 종목은 점수를 내지 않는다. 폐지 예정(거래 중)은 점수 + 최대 경고.
  분모가 0 이하면 비율의 부호가 뒤집혀 '최고'로 읽힙니다 — 백분위 0점 고정.
      적자(PER) · 자본잠식(ROE·부채비율·PBR) 네 경우를 한 규칙이 덮습니다.
"""
import os
import pathlib
import sqlite3
from typing import NamedTuple

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"


def default_db():
    """쓸 DB를 고릅니다. 실DB가 있으면 그걸, 없으면 가짜 DB.

    담당 ①이 data/stocks.db 를 만들어 두면 코드를 한 줄도 안 고치고 바로 붙습니다.
    STOCKS_DB 환경변수로 덮어쓸 수 있습니다 (실DB를 두고 가짜로 돌려보고 싶을 때).

    data/ 는 통째로 .gitignore 되어 있습니다 — 커밋되지 않습니다.
    """
    env = os.environ.get("STOCKS_DB")
    if env:
        return pathlib.Path(env)
    real = DATA_DIR / "stocks.db"
    return real if real.exists() else DATA_DIR / "fake.db"


MIN_SAMPLE = 30       # 백분위를 내기 위한 최소 유효표본
MIN_INDICATORS = 5    # 이 개수 이상이 표본을 만족해야 그 SIC 레벨을 씁니다 (8개 중)
ASOF = "2026-09-20"


class _Worst:
    """분모가 0 이하 — 적자 또는 자본잠식. 백분위 0점 고정."""
    __slots__ = ()

    def __repr__(self):
        return "WORST"


WORST = _Worst()


def _ratio(a, b, nonpos_is_worst=True):
    """분모가 0 이하면 비율의 부호가 뒤집힙니다.

    그대로 두면 적자 기업의 PER이 음수가 되어 'lower_better' 기준으로 가장 싼 주식이
    됩니다. 자본잠식이면 부채비율·PBR·ROE가 전부 같은 덫에 걸립니다.
    """
    if a is None or b is None:
        return None
    if b <= 0:
        return WORST if nonpos_is_worst else None
    return a / b


def _last(f):
    return f[max(f)] if f else {}


def _cagr(f, metric, n=3):
    yrs = sorted(f)
    if len(yrs) < n + 1:
        return None
    a, b = f[yrs[-1 - n]].get(metric), f[yrs[-1]].get(metric)
    if a is None or b is None or a <= 0:   # 음수에서 CAGR은 정의되지 않습니다
        return None
    return (b / a) ** (1 / n) - 1


class Ind(NamedTuple):
    """지표 하나의 정의. 위치 튜플 8칸은 3시에 해독이 안 되므로 이름을 붙였습니다."""
    axis: str
    axis_label: str
    key: str
    label: str
    fn: object          # (facts, price) -> 값 | None | WORST
    direction: str      # higher_better | lower_better
    unit: str           # ratio | multiple
    sources: object     # (cik, ticker, fy) -> [근거 문자열]  좌측 드릴다운이 이걸 씁니다


def _fact_src(cik, fy, *metrics, back=0):
    return [f"annual_fact:{cik}:{fy - back}:{m}" for m in metrics] if fy else []


def _cap_src(ticker):
    return [f"price_snapshot:{ticker}:{ASOF}:market_cap"]


INDICATORS = [
    Ind("profitability", "수익성", "operating_margin", "영업이익률",
        lambda f, p: _ratio(_last(f).get("operating_income"), _last(f).get("revenue")),
        "higher_better", "ratio",
        lambda cik, tk, fy: _fact_src(cik, fy, "operating_income", "revenue")),

    Ind("profitability", "수익성", "roe", "ROE",
        lambda f, p: _ratio(_last(f).get("net_income"), _last(f).get("equity")),
        "higher_better", "ratio",
        lambda cik, tk, fy: _fact_src(cik, fy, "net_income", "equity")),

    Ind("growth", "성장성", "revenue_cagr3", "매출 3년 CAGR",
        lambda f, p: _cagr(f, "revenue"),
        "higher_better", "ratio",
        lambda cik, tk, fy: _fact_src(cik, fy, "revenue", back=3) + _fact_src(cik, fy, "revenue")),

    Ind("growth", "성장성", "operating_income_cagr3", "영업이익 3년 CAGR",
        lambda f, p: _cagr(f, "operating_income"),
        "higher_better", "ratio",
        lambda cik, tk, fy: (_fact_src(cik, fy, "operating_income", back=3)
                             + _fact_src(cik, fy, "operating_income"))),

    Ind("stability", "안정성", "debt_to_equity", "부채비율",
        lambda f, p: _ratio(_last(f).get("liabilities"), _last(f).get("equity")),
        "lower_better", "ratio",
        lambda cik, tk, fy: _fact_src(cik, fy, "liabilities", "equity")),

    Ind("stability", "안정성", "cashflow_quality", "영업현금흐름 / 순이익",
        # 적자면 이 비율은 '나쁨'이 아니라 '의미 없음'입니다 — 0점이 아니라 결측
        lambda f, p: _ratio(_last(f).get("operating_cashflow"), _last(f).get("net_income"),
                            nonpos_is_worst=False),
        "higher_better", "multiple",
        lambda cik, tk, fy: _fact_src(cik, fy, "operating_cashflow", "net_income")),

    Ind("valuation", "밸류에이션", "per", "PER",
        lambda f, p: _ratio(p.get("market_cap") if p else None, _last(f).get("net_income")),
        "lower_better", "multiple",
        lambda cik, tk, fy: _cap_src(tk) + _fact_src(cik, fy, "net_income")),

    Ind("valuation", "밸류에이션", "pbr", "PBR",
        lambda f, p: _ratio(p.get("market_cap") if p else None, _last(f).get("equity")),
        "lower_better", "multiple",
        lambda cik, tk, fy: _cap_src(tk) + _fact_src(cik, fy, "equity")),
]

AXIS_ORDER = ["profitability", "growth", "stability", "valuation"]
AXIS_LABEL = {i.axis: i.axis_label for i in INDICATORS}


def _fmt(v, unit):
    if v is WORST:
        return "적자 또는 자본잠식"
    if v is None:
        return None
    return f"{v * 100:.1f}%" if unit == "ratio" else f"{v:.1f}배"


REQUIRED_TABLES = {"company", "annual_fact", "price_snapshot", "macro"}


class Engine:
    """DB를 시작할 때 통째로 메모리에 올리고, 연결은 닫습니다.

    연결을 들고 있을 이유가 없습니다 — 적재 후에는 DB를 다시 읽지 않습니다.
    닫아두면 두 가지가 공짜로 해결됩니다:
      - sqlite3 연결을 다른 스레드에서 쓰는 사고가 구조적으로 불가능해집니다
        (실제로 한 번 터졌습니다: 서버가 요청마다 스레드를 바꿉니다)
      - Windows 에서 파일이 잠기지 않아, 서버를 켜둔 채로 담당 ①이 DB를 다시
        만들 수 있습니다. 서버는 재시작 전까지 옛 데이터를 그대로 서빙합니다.

    5,800종목 실측 추정: 적재 약 1초, 메모리 약 36MB.
    """

    def __init__(self, db):
        self.db = pathlib.Path(db)
        self._load()

    def _open(self):
        """DB를 열되, 못 쓸 상태면 무슨 일인지 알려주고 멈춥니다.

        ETL 도중인 파일을 열면 'no such table: company' 라는 알 수 없는 SQL
        에러로 서버가 죽습니다. 담당 ①이 반드시 밟을 상황이라 먼저 잡습니다.
        """
        if not self.db.exists():
            raise RuntimeError(
                f"DB 파일이 없습니다: {self.db}\n"
                f"  가짜 DB로 돌리려면 : python scoring/fake_db.py\n"
                f"  실DB를 쓰려면      : data/stocks.db 에 파일을 놓으세요")
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_TABLES - have
        if missing:
            conn.close()
            raise RuntimeError(
                f"DB에 테이블이 없습니다: {self.db}\n"
                f"  없는 테이블 : {sorted(missing)}\n"
                f"  ETL이 아직 안 끝났거나 contracts/db_schema.sql 로 만들어지지 "
                f"않은 파일입니다")
        return conn

    def _load(self):
        conn = self._open()
        try:
            self.company = {r["cik"]: dict(r) for r in conn.execute("SELECT * FROM company")}
            self.by_ticker = {c["ticker"]: c["cik"] for c in self.company.values()}

            facts = {}
            for r in conn.execute("SELECT cik, fiscal_year, metric, value FROM annual_fact"):
                facts.setdefault(r["cik"], {}).setdefault(
                    r["fiscal_year"], {})[r["metric"]] = r["value"]

            self.fiscal_year = {cik: max(y) for cik, y in facts.items() if y}

            self.price = {}
            for r in conn.execute(
                    "SELECT * FROM price_snapshot "
                    "WHERE asof = (SELECT max(asof) FROM price_snapshot)"):
                self.price[r["ticker"]] = dict(r)

            self.macro = {r["series_id"]: r["value"] for r in conn.execute(
                "SELECT series_id, value FROM macro "
                "WHERE date = (SELECT max(date) FROM macro)")}

            # meta 는 선택입니다. 없으면 빈 dict — 예전 DB 도 그대로 돕니다.
            try:
                self.meta = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM meta")}
            except sqlite3.OperationalError:
                self.meta = {}
        finally:
            conn.close()   # 적재 끝. 이후 DB를 다시 읽지 않습니다.

        if not self.company:
            raise RuntimeError(
                f"DB에 종목이 하나도 없습니다: {self.db}\n"
                f"  ETL이 아직 안 끝난 것 같습니다. 빈 DB로 서버를 띄우면 "
                f"모든 조회가 not_found 가 됩니다")

        # 전 종목 × 전 지표를 한 번에 계산해둡니다. 백분위는 이 행렬 위에서 냅니다.
        self.values = {}
        for cik, c in self.company.items():
            f = facts.get(cik, {})
            p = self.price.get(c["ticker"])
            self.values[cik] = {i.key: i.fn(f, p) for i in INDICATORS}

    # -- 모집단 -----------------------------------------------------------
    def _peers(self, prefix):
        """상장폐지 종목은 비교군에서 뺍니다 — 살 수 없는 주식과 비교할 이유가 없습니다."""
        return [cik for cik, c in self.company.items()
                if c["sic"] and c["sic"].startswith(prefix) and not c["delisted_date"]]

    def _numeric(self, peers, ind):
        return [self.values[c][ind] for c in peers
                if isinstance(self.values[c][ind], (int, float))]

    def pick_peer_group(self, sic):
        """모집단은 종목당 하나.

        지표마다 다른 레벨을 쓰면 '동종업계 58개 중 26위'라는 한 문장이 성립하지 않습니다.
        그 레벨에서 표본이 모자란 개별 지표는 S5 규칙으로 빠집니다.
        """
        for level in (4, 3, 2):
            prefix = sic[:level]
            peers = self._peers(prefix)
            ok = sum(1 for i in INDICATORS
                     if len(self._numeric(peers, i.key)) >= MIN_SAMPLE)
            if ok >= MIN_INDICATORS:
                return prefix, level, peers, ok
        peers = [cik for cik, c in self.company.items() if not c["delisted_date"]]
        return "", 0, peers, 0

    @staticmethod
    def rank_noun(level):
        """순위 문장의 주어. level 0 인데 '동종업계' 라고 쓰면 거짓말입니다 —
        전체 시장 4,584개가 동종업계일 리 없습니다. LLM 이 그 문장을 그대로
        받아 쓰기 때문에 화면과 글이 같이 틀립니다."""
        return {4: "동종업계", 3: "유사업종", 2: "같은 대분류"}.get(level, "전체 시장")

    @staticmethod
    def peer_label(level, sic_desc):
        """화면에 '동종업계 58개'와 '전체 시장 4,800개'는 신뢰도가 다릅니다. 구분해서 보여줘야 합니다."""
        return {4: sic_desc, 3: f"{sic_desc} (유사업종)",
                2: f"{sic_desc} (대분류)"}.get(level, "전체 시장 (동종업계 표본 부족)")

    # -- 백분위 -----------------------------------------------------------
    @staticmethod
    def _pct(v, arr, direction):
        if direction == "lower_better":
            better = sum(1 for x in arr if x > v)
        else:
            better = sum(1 for x in arr if x < v)
        return round(100 * better / len(arr))

    @staticmethod
    def _deciles(arr):
        """업종 분포 히스토그램 10칸. 양 끝 5%를 잘라낸 범위에서 나눕니다.

        최소~최대로 나누면 실데이터에서 막대 하나만 남습니다 — 재무비율에는
        영업이익률 -10000% 같은 극단값이 섞여 있어서 나머지가 전부 한 칸에
        뭉갭니다 (실측: 222개 중 218개가 한 칸). 가짜 데이터는 정규분포라
        안 보였습니다.

        잘라낸 바깥 값은 첫/마지막 칸에 넣습니다. 합계는 len(arr) 그대로라
        "이 막대에 몇 개" 가 계속 맞습니다.
        """
        if not arr:
            return [0] * 10
        srt = sorted(arr)
        q = lambda p: srt[min(len(srt) - 1, int(len(srt) * p))]
        # Tukey 울타리. 5~95% 절단으로는 부족했습니다 — 영업이익률처럼 한쪽으로
        # 심하게 쏠린 분포는 p5 자체가 극단값이라 나머지가 여전히 한 칸에 뭉갭니다.
        q1, q3 = q(0.25), q(0.75)
        iqr = q3 - q1
        lo, hi = (q1 - 1.5 * iqr, q3 + 1.5 * iqr) if iqr > 0 else (q(0.05), q(0.95))
        if hi == lo:
            return [len(arr)] + [0] * 9
        out = [0] * 10
        for x in arr:
            out[max(0, min(9, int((x - lo) / (hi - lo) * 10)))] += 1
        return out

    # -- 점수 -------------------------------------------------------------
    def _delisted_result(self, ticker, c, cik):
        """S6 — 살 수 없는 주식에 점수를 붙이면 살 수 있는 것처럼 보입니다."""
        return {
            "ticker": ticker, "name": c["name"], "cik": cik, "asof": ASOF,
            "fiscal_year": self.fiscal_year.get(cik),
            "score": None, "rank_text": None,
            "delisting": {"delisted_date": c["delisted_date"], "status": "delisted"},
            "peer_group": None,
            "score_basis": {"axes_used": 0, "axes_total": 4,
                            "reason": "상장폐지 종목 · 점수를 내지 않습니다"},
            "flags": [{"id": "flag.delisting", "severity": "critical",
                       "text": f"{c['delisted_date']} 상장폐지된 종목입니다"}],
            "axes": [],
        }

    def _indicator(self, cik, ticker, spec, peers):
        v = self.values[cik][spec.key]
        arr = self._numeric(peers, spec.key)
        fy = self.fiscal_year.get(cik)
        base = {"id": f"{spec.axis}.{spec.key}", "label": spec.label, "unit": spec.unit,
                "direction": spec.direction, "sources": spec.sources(cik, ticker, fy)}
        blank = {"value": None, "display": None, "percentile": None,
                 "peer_median": None, "peer_median_display": None, "peer_deciles": None}

        if len(arr) < MIN_SAMPLE:
            return base | blank | {
                "status": "insufficient_data",
                "reason": f"비교 가능한 동종업계 표본 {len(arr)}개 (최소 {MIN_SAMPLE})"}
        if v is None:
            return base | blank | {
                "status": "insufficient_data",
                "reason": "이 종목의 데이터가 부족해 계산할 수 없습니다"}

        med = sorted(arr)[len(arr) // 2]
        return base | {
            "value": None if v is WORST else round(v, 4),
            "display": _fmt(v, spec.unit),
            "percentile": 0 if v is WORST else self._pct(v, arr, spec.direction),
            "status": "ok", "reason": None,
            "peer_median": round(med, 4), "peer_median_display": _fmt(med, spec.unit),
            "peer_deciles": self._deciles(arr)}

    def score(self, ticker):
        cik = self.by_ticker.get(ticker)
        if cik is None:
            return {"ticker": ticker, "error": "not_found"}
        c = self.company[cik]

        delisted = c["delisted_date"]
        still_trading = bool(delisted and delisted > ASOF)
        if delisted and not still_trading:
            return self._delisted_result(ticker, c, cik)

        prefix, level, peers, ok_inds = self.pick_peer_group(c["sic"] or "")
        axes, used_i = [], 0

        for axis in AXIS_ORDER:
            inds = [self._indicator(cik, c["ticker"], i, peers)
                    for i in INDICATORS if i.axis == axis]
            got = [i["percentile"] for i in inds if i["status"] == "ok"]
            used_i += len(got)
            # S5 — 있는 것만 평균. 절반 이상 빠지면 그 축은 없는 것으로 봅니다.
            if len(got) * 2 >= len(inds):
                axes.append({"id": axis, "label": AXIS_LABEL[axis],
                             "score": round(sum(got) / len(got)),
                             "status": "ok", "reason": None, "indicators": inds})
            else:
                axes.append({"id": axis, "label": AXIS_LABEL[axis], "score": None,
                             "status": "insufficient_data",
                             "reason": "지표 절반 이상을 계산할 수 없습니다",
                             "indicators": inds})

        ok_axes = [a["score"] for a in axes if a["status"] == "ok"]
        total = round(sum(ok_axes) / len(ok_axes)) if len(ok_axes) * 2 >= len(axes) else None
        n = len(peers)
        rank = max(1, round(n * (1 - total / 100))) if total is not None else None

        flags = []
        if still_trading:
            flags.append({"id": "flag.delisting", "severity": "critical",
                          "text": f"{delisted} 상장폐지 예정 · 거래가 곧 중단됩니다"})
        if self.meta.get("price_source") == "dummy":
            # 가짜 가격으로 낸 PER·PBR 을 진짜처럼 보여주면 안 됩니다.
            flags.append({"id": "flag.dummy_price", "severity": "warning",
                          "text": "가격 데이터가 샘플입니다 — PER·PBR 은 실제 값이 아닙니다"})

        aligned = (self.price.get(ticker) or {}).get("aligned_days")
        if aligned and aligned >= 90:
            flags.append({"id": "flag.ma_late", "severity": "warning",
                          "aligned_days": aligned,
                          "text": f"정배열 {aligned}일째 · 오래 오른 구간입니다"})

        return {
            "ticker": ticker, "name": c["name"], "cik": cik, "asof": ASOF,
            "fiscal_year": self.fiscal_year.get(cik),
            "score": total,
            "rank_text": (f"{self.rank_noun(level)} {n}개 중 {rank}위 "
                          f"(상위 {100 - total}%)" if total is not None else None),
            "delisting": {"delisted_date": delisted,
                          "status": "delisting_soon" if still_trading else "listed"},
            "peer_group": {"sic": prefix, "label": self.peer_label(level, c["sic_desc"]),
                           "level": level, "n": n,
                           "selected_by": f"8개 지표 중 {ok_inds}개가 유효표본 {MIN_SAMPLE} 이상"},
            "score_basis": {"axes_used": len(ok_axes), "axes_total": len(axes),
                            "indicators_used": used_i, "indicators_total": len(INDICATORS)},
            "flags": flags,
            "axes": axes,
        }


if __name__ == "__main__":
    import json
    import sys

    engine = Engine(default_db())
    print(json.dumps(engine.score(sys.argv[1] if len(sys.argv) > 1 else "35010"),
                     ensure_ascii=False, indent=2))
