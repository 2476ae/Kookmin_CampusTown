"""점수 엔진. 결정적 — 같은 DB면 항상 같은 점수. LLM 안 씁니다.

출력은 contracts/score.example.json 과 같은 구조입니다.

규칙 (contracts/README.md 참조):
  S5  있는 것만 평균 낸다. 빠진 건 명시한다. 절반 이상 빠지면 점수를 내지 않는다.
      지표 층·축 층에 똑같이 적용.
  S6  이미 폐지된 종목은 점수를 내지 않는다. 폐지 예정(거래 중)은 점수 + 최대 경고.
  분모가 0 이하면 비율의 부호가 뒤집혀 '최고'로 읽힙니다 — 백분위 0점 고정.
      적자(PER) · 자본잠식(ROE·부채비율·PBR) 네 경우를 한 규칙이 덮습니다.
"""
import pathlib
import sqlite3
from typing import NamedTuple

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


class Engine:
    def __init__(self, db):
        self.conn = sqlite3.connect(db)
        self.conn.row_factory = sqlite3.Row
        self._load()

    def _load(self):
        self.company = {r["cik"]: dict(r) for r in self.conn.execute("SELECT * FROM company")}
        self.by_ticker = {c["ticker"]: c["cik"] for c in self.company.values()}

        facts = {}
        for r in self.conn.execute("SELECT cik, fiscal_year, metric, value FROM annual_fact"):
            facts.setdefault(r["cik"], {}).setdefault(r["fiscal_year"], {})[r["metric"]] = r["value"]

        self.fiscal_year = {cik: max(y) for cik, y in facts.items() if y}

        self.price = {}
        for r in self.conn.execute(
                "SELECT * FROM price_snapshot "
                "WHERE asof = (SELECT max(asof) FROM price_snapshot)"):
            self.price[r["ticker"]] = dict(r)

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
        lo, hi = min(arr), max(arr)
        if hi == lo:
            return [len(arr)] + [0] * 9
        out = [0] * 10
        for x in arr:
            out[min(9, int((x - lo) / (hi - lo) * 10))] += 1
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
        aligned = (self.price.get(ticker) or {}).get("aligned_days")
        if aligned and aligned >= 90:
            flags.append({"id": "flag.ma_late", "severity": "warning",
                          "aligned_days": aligned,
                          "text": f"정배열 {aligned}일째 · 오래 오른 구간입니다"})

        return {
            "ticker": ticker, "name": c["name"], "cik": cik, "asof": ASOF,
            "fiscal_year": self.fiscal_year.get(cik),
            "score": total,
            "rank_text": (f"동종업계 {n}개 중 {rank}위 (상위 {100 - total}%)"
                          if total is not None else None),
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

    root = pathlib.Path(__file__).resolve().parent.parent
    engine = Engine(root / "data" / "fake.db")
    print(json.dumps(engine.score(sys.argv[1] if len(sys.argv) > 1 else "35010"),
                     ensure_ascii=False, indent=2))
