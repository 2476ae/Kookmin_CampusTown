"""SEC 원본(sub/num) → contracts/db_schema.sql 형식으로 내보냅니다.

    python etl/export_to_contract.py --source <원본DB> --out data/stocks.db
    python etl/export_to_contract.py --from-zip 2026q1.zip --out data/stocks.db

담당 ① 의 DuckDB 를 SQLite 로 바꾸라는 뜻이 아닙니다. 둘은 역할이 다릅니다:
  DuckDB   분석용. 78.6M행 스캔, 지표 실험
  stocks.db 서빙용. 메모리에 올려 0.2ms 응답. 의존성 없음

이 스크립트는 **참고 구현**입니다. 실제 SEC 2026q1 데이터로 돌려서
scoring/check_db.py 를 통과하는 것까지 확인했지만, 담당 ① 의 DuckDB 스키마에
맞춰 SELECT 를 손봐야 할 수 있습니다. 바꿔야 할 건 _read_source() 하나입니다.

--- 실데이터로 확인한 함정 -------------------------------------------------
qtrs   0 = 시점 값(자산·부채·자본), 4 = 연간 기간 값(매출·이익·현금흐름).
       헷갈리면 분기 값을 연간으로 씁니다. 이게 제일 조용히 틀리는 곳입니다.
segments/coreg  비어 있는 행만. 아니면 사업부문별 값이 섞입니다.
cik    SEC 원본은 '6955' 처럼 패딩이 없습니다. 계약은 10자리 0패딩입니다.
       안 맞추면 계약의 sources 필드가 깨져 좌측 드릴다운이 원본을 못 찾습니다.
liabilities  아예 없는 회사가 있습니다 (KO). assets - equity 폴백 필수.
"""
import argparse
import csv
import io
import pathlib
import sqlite3
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 태그 우선순위 사다리. 앞에서부터 찾고, 있으면 멈춥니다.
# (회사마다 태그가 다릅니다 — TSLA 는 Revenue 계열이 17개)
METRIC_TAGS = {
    "revenue": ["Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "operating_cashflow": ["NetCashProvidedByUsedInOperatingActivities",
                           "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
}

# qtrs: 기간 값인가 시점 값인가. 이걸 틀리면 분기 숫자가 연간으로 들어갑니다.
FLOW = {"revenue", "operating_income", "net_income", "operating_cashflow"}   # qtrs = 4
STOCK = {"assets", "liabilities", "equity"}                                  # qtrs = 0

TAG_TO_METRIC = {t: m for m, tags in METRIC_TAGS.items() for t in tags}
TAG_RANK = {t: i for tags in METRIC_TAGS.values() for i, t in enumerate(tags)}


def _read_zip(path):
    """SEC Financial Statement Data Sets 분기 zip 에서 바로 읽습니다.

    담당 ① 처럼 이미 DuckDB 에 적재했다면 이 함수 대신 SELECT 를 쓰세요:
        con.execute("SELECT adsh, cik, name, sic, form, period, fy FROM sub").fetchall()
        con.execute("SELECT adsh, tag, ddate, qtrs, uom, segments, coreg, value FROM num")
    """
    z = zipfile.ZipFile(path)

    def rows(name):
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, "utf-8", errors="replace"),
                                      delimiter="\t")
    return rows("sub.txt"), rows("num.txt")


def build(sub_rows, num_rows, out, tickers=None):
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    conn = sqlite3.connect(out)
    conn.executescript((ROOT / "contracts" / "db_schema.sql").read_text(encoding="utf-8"))

    # --- 1) 10-K 제출만. adsh -> (cik, 회계연도) -----------------------
    subs, companies = {}, {}
    for r in sub_rows:
        if r["form"] != "10-K":
            continue
        cik = r["cik"].zfill(10)          # 계약은 10자리 0패딩
        subs[r["adsh"]] = cik
        companies[cik] = {"name": r["name"], "sic": (r["sic"] or "").strip() or None}

    # --- 2) num -> 정규화된 metric ------------------------------------
    # 같은 (cik, 연도, metric) 에 여러 태그가 오면 우선순위가 높은 태그가 이깁니다.
    facts, chosen = {}, {}
    for r in num_rows:
        cik = subs.get(r["adsh"])
        if cik is None or r["uom"] != "USD":
            continue
        if r["segments"] or r["coreg"]:   # 연결 전체 값만
            continue
        metric = TAG_TO_METRIC.get(r["tag"])
        if metric is None:
            continue
        want = "4" if metric in FLOW else "0"
        if r["qtrs"] != want:             # 기간/시점 구분. 틀리면 분기값이 섞입니다
            continue
        try:
            value = float(r["value"])
        except (ValueError, TypeError):
            continue
        year = int(r["ddate"][:4])
        key = (cik, year, metric)
        rank = TAG_RANK[r["tag"]]
        if key not in facts or rank < chosen[key]:
            facts[key] = (value, r["tag"])
            chosen[key] = rank

    # --- 3) liabilities 폴백 ------------------------------------------
    filled = 0
    for (cik, year, metric) in list(facts):
        if metric != "assets":
            continue
        if (cik, year, "liabilities") in facts:
            continue
        eq = facts.get((cik, year, "equity"))
        if eq:
            facts[(cik, year, "liabilities")] = (
                facts[(cik, year, "assets")][0] - eq[0], "assets-equity")
            filled += 1

    # --- 4) 쓰기 -------------------------------------------------------
    used = {cik for cik, _, _ in facts}
    for cik in used:
        c = companies[cik]
        t = (tickers or {}).get(cik, {})
        conn.execute("INSERT INTO company VALUES (?,?,?,?,?,?,?)",
                     (cik, t.get("ticker", cik), c["name"], c["sic"],
                      t.get("sic_desc"), t.get("exchange"), t.get("delisted_date")))
    conn.executemany(
        "INSERT INTO annual_fact VALUES (?,?,?,?,?)",
        [(cik, year, metric, v, tag) for (cik, year, metric), (v, tag) in facts.items()])
    conn.commit()
    conn.close()
    return {"companies": len(used), "facts": len(facts), "liabilities_filled": filled}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-zip", help="SEC Financial Statement Data Sets 분기 zip")
    ap.add_argument("--out", default=str(ROOT / "data" / "stocks.db"))
    a = ap.parse_args()
    if not a.from_zip:
        ap.error("--from-zip 을 주거나, DuckDB 를 쓰려면 _read_zip 을 SELECT 로 바꾸세요")
    sub, num = _read_zip(a.from_zip)
    stats = build(sub, num, a.out)
    print(f"{a.out}")
    for k, v in stats.items():
        print(f"  {k:20} {v:,}")
    print(f"\n다음: python scoring/check_db.py {a.out}")


if __name__ == "__main__":
    sys.exit(main())
