"""SEC 원본(sub/num) → contracts/db_schema.sql 형식으로 내보냅니다.

    python etl/export_to_contract.py --from-duckdb sec.duckdb --out data/stocks.db
    python etl/export_to_contract.py --from-zip 2026q1.zip 2025q4.zip --out data/stocks.db

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


TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SIC_URL = ("https://www.sec.gov/corpfin/division-of-corporation-finance-"
           "standard-industrial-classification-sic-code-list")
def _user_agent():
    """SEC 는 User-Agent 에 연락처를 요구합니다. 없으면 403 입니다.

    저장소에 개인 이메일을 박지 않으려고 .env 에서 읽습니다 (.env 는 gitignore).
        SEC_CONTACT_EMAIL=you@example.com
    """
    import os
    sys.path.insert(0, str(ROOT / "llm"))
    try:
        from opinion import load_dotenv    # 프로젝트의 .env 로더를 그대로 씁니다
        load_dotenv()
    except Exception:
        pass
    email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if "@" not in email:
        raise SystemExit("\n".join([
            "SEC 는 User-Agent 에 연락처 이메일을 요구합니다 (없으면 403).",
            "  .env 에 한 줄 넣으세요:  SEC_CONTACT_EMAIL=you@example.com",
            "  또는 --no-fetch 로 티커·SIC 설명 없이 내보내세요"]))
    return f"Kookmin CampusTown Prototype {email}"


def _fetch(url, cache):
    """SEC 공개 파일을 받아 data/raw/ 에 캐시합니다 (523KB + 109KB)."""
    cache = pathlib.Path(cache)
    if cache.exists():
        return cache.read_bytes()
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(body)
    return body


def load_tickers(cache_dir):
    """cik -> {ticker, exchange, name}.

    이게 없으면 화면에 CIK('0000320193')가 티커 자리에 뜹니다.
    담당 ① 의 ticker_exchange 테이블과 같은 소스입니다 (10,438행).
    """
    import json
    d = json.loads(_fetch(TICKERS_URL, pathlib.Path(cache_dir) / "company_tickers_exchange.json"))
    idx = {f: i for i, f in enumerate(d["fields"])}
    out = {}
    for row in d["data"]:
        cik = str(row[idx["cik"]]).zfill(10)
        # 한 회사에 여러 티커가 있으면 먼저 온 것을 씁니다 (보통 보통주)
        out.setdefault(cik, {"ticker": row[idx["ticker"]],
                             "exchange": row[idx["exchange"]],
                             "name": row[idx["name"]]})
    return out


def load_sic_desc(cache_dir):
    """sic 코드 -> 설명. 없으면 비교군 라벨이 None 이 됩니다."""
    import re
    html = _fetch(SIC_URL, pathlib.Path(cache_dir) / "sic_codes.html").decode("utf-8", "replace")
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 3 and cells[0].isdigit():
            out[cells[0]] = cells[-1].replace("&amp;", "&").title()
    return out


def load_delisted(path):
    """상장폐지 목록. cik,delisted_date 두 컬럼짜리 CSV.

    담당 ① 이 Form 25-NSE 에서 1,103건을 수집해뒀습니다. 그쪽이 ETF·우선주·SPAC 을
    이미 걸러냈으니 여기서 다시 만들지 않습니다.
    이게 없으면 **S6 상장폐지 경고 배지가 통째로 죽습니다.**
    """
    if not path:
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cik = (row.get("cik") or "").strip()
            date = (row.get("delisted_date") or row.get("form25_date") or "").strip()
            if cik and date:
                out[cik.zfill(10)] = date
    return out


def _read_duckdb(path, sub_table="sub", num_table="num"):
    """담당 ① 의 DuckDB 에서 직접 읽습니다.

        python etl/export_to_contract.py --from-duckdb sec.duckdb --out data/stocks.db

    SEC Financial Statement Data Sets 를 그대로 적재했다면 테이블 이름만 맞으면
    됩니다. 다르면 --sub-table / --num-table 로 알려주세요.

    전 컬럼 VARCHAR 로 적재돼 있어도 됩니다 — 여기서 문자열로 받아 처리합니다.
    3.2GB 를 통째로 메모리에 올리지 않게 청크로 가져옵니다.
    """
    import duckdb

    con = duckdb.connect(str(path), read_only=True)

    def rows(table, cols):
        cur = con.execute(f"SELECT {', '.join(cols)} FROM {table}")
        while True:
            batch = cur.fetchmany(50_000)
            if not batch:
                return
            for r in batch:
                yield {c: ("" if v is None else str(v)) for c, v in zip(cols, r)}

    return (rows(sub_table, ["adsh", "cik", "name", "sic", "form", "filed"]),
            rows(num_table, ["adsh", "tag", "ddate", "qtrs", "uom",
                             "segments", "coreg", "value"]))


def _read_zips(paths):
    """SEC Financial Statement Data Sets 분기 zip 들에서 읽습니다.

    분기 하나로도 과거 비교수치 덕에 18년치가 나오지만, **회사 수**는 그 분기에
    10-K 를 낸 곳으로 제한됩니다 (2026q1 단독 = 4,244개). 회계연도 말이 다른
    회사들을 잡으려면 최근 4개 분기를 같이 넣는 게 좋습니다.

    담당 ① 처럼 이미 DuckDB 에 적재했다면 이 함수 대신 SELECT 를 쓰세요:
        con.execute("SELECT adsh, cik, name, sic, form, period, filed FROM sub").fetchall()
        con.execute("SELECT adsh, tag, ddate, qtrs, uom, segments, coreg, value FROM num")
    """
    zips = [zipfile.ZipFile(p) for p in paths]

    def rows(name):
        for z in zips:
            with z.open(name) as f:
                yield from csv.DictReader(io.TextIOWrapper(f, "utf-8", errors="replace"),
                                          delimiter="\t")
    return rows("sub.txt"), rows("num.txt")


def build(sub_rows, num_rows, out, tickers=None, sic_desc=None, delisted=None):
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    conn = sqlite3.connect(out)
    conn.executescript((ROOT / "contracts" / "db_schema.sql").read_text(encoding="utf-8"))

    # --- 1) 10-K 제출만. adsh -> (cik, 회계연도) -----------------------
    subs, companies, filed = {}, {}, {}
    for r in sub_rows:
        if r["form"] != "10-K":
            continue
        cik = r["cik"].zfill(10)          # 계약은 10자리 0패딩
        subs[r["adsh"]] = cik
        filed[r["adsh"]] = r.get("filed", "")
        # 여러 분기를 넣으면 같은 회사가 여러 번 옵니다. 최신 제출을 씁니다.
        prev = companies.get(cik)
        if prev is None or r.get("filed", "") >= prev["filed"]:
            companies[cik] = {"name": r["name"], "sic": (r["sic"] or "").strip() or None,
                              "filed": r.get("filed", "")}

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
        # (최신 제출일, 태그 우선순위) 로 고릅니다. 여러 분기를 넣으면 같은
        # (회사·연도·지표) 가 여러 번 오는데, 나중 제출이 재작성을 반영합니다.
        rank = (filed.get(r["adsh"], ""), -TAG_RANK[r["tag"]])
        if key not in facts or rank > chosen[key]:
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
    tickers, sic_desc, delisted = tickers or {}, sic_desc or {}, delisted or {}
    used = {cik for cik, _, _ in facts}
    matched = 0
    for cik in used:
        c = companies[cik]
        t = tickers.get(cik, {})
        if t:
            matched += 1
        conn.execute("INSERT INTO company VALUES (?,?,?,?,?,?,?)",
                     (cik,
                      t.get("ticker") or cik,          # 매핑이 없으면 CIK 가 그대로 들어갑니다
                      t.get("name") or c["name"],      # 티커 파일 쪽 이름이 더 읽기 좋습니다
                      c["sic"],
                      sic_desc.get(c["sic"] or ""),
                      t.get("exchange"),
                      delisted.get(cik)))
    conn.executemany(
        "INSERT INTO annual_fact VALUES (?,?,?,?,?)",
        [(cik, year, metric, v, tag) for (cik, year, metric), (v, tag) in facts.items()])
    conn.commit()
    conn.close()
    return {"companies": len(used), "facts": len(facts), "liabilities_filled": filled,
            "ticker_matched": matched,
            "sic_desc_filled": sum(1 for c in used if sic_desc.get(companies[c]["sic"] or "")),
            "delisted_marked": sum(1 for c in used if c in delisted)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-zip", nargs="+",
                    help="SEC Financial Statement Data Sets 분기 zip (여러 개 가능)")
    ap.add_argument("--from-duckdb", help="담당 ① 의 DuckDB 파일")
    ap.add_argument("--sub-table", default="sub", help="DuckDB 의 sub 테이블 이름")
    ap.add_argument("--num-table", default="num", help="DuckDB 의 num 테이블 이름")
    ap.add_argument("--out", default=str(ROOT / "data" / "stocks.db"))
    ap.add_argument("--delisted", help="상장폐지 목록 CSV (cik,delisted_date). "
                                       "없으면 S6 경고 배지가 안 뜹니다")
    ap.add_argument("--no-fetch", action="store_true",
                    help="티커·SIC 설명을 SEC 에서 받지 않습니다 (오프라인)")
    a = ap.parse_args()
    if not (a.from_zip or a.from_duckdb):
        ap.error("--from-zip 또는 --from-duckdb 중 하나를 주세요")

    raw = ROOT / "data" / "raw"
    tickers = sic_desc = {}
    if not a.no_fetch:
        print("SEC 공개 파일 받는 중 (티커 매핑 523KB · SIC 설명 109KB, 캐시됨)…")
        tickers, sic_desc = load_tickers(raw), load_sic_desc(raw)

    sub, num = (_read_duckdb(a.from_duckdb, a.sub_table, a.num_table)
                if a.from_duckdb else _read_zips(a.from_zip))
    stats = build(sub, num, a.out, tickers, sic_desc, load_delisted(a.delisted))
    print(f"{a.out}")
    for k, v in stats.items():
        print(f"  {k:20} {v:,}")
    print(f"\n다음: python scoring/check_db.py {a.out}")


if __name__ == "__main__":
    sys.exit(main())
