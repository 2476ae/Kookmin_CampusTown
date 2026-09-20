"""실DB가 계약을 지켰는지 붙이기 전에 확인합니다.

    python scoring/check_db.py                 # default_db() 가 고른 DB
    python scoring/check_db.py data/stocks.db  # 특정 파일

담당 ① 이 DB를 넘기기 전에, 담당 ②③ 이 받은 DB를 쓰기 전에 돌리세요.
"붙여보고 안 되네" 를 "넘기기 전에 알았네" 로 바꾸는 게 목적입니다.

FAIL 이 하나라도 있으면 엔진이 못 씁니다. WARN 은 돌아가지만 화면이 이상해집니다.
"""
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from engine import INDICATORS, MIN_SAMPLE, Engine, default_db  # noqa: E402

METRICS = {"revenue", "operating_income", "net_income", "assets",
           "liabilities", "equity", "operating_cashflow"}
TABLES = {"company", "annual_fact", "price_snapshot", "macro"}

results = []


def report(level, name, detail=""):
    results.append((level, name, detail))
    mark = {"PASS": "  ok ", "WARN": " warn", "FAIL": " FAIL"}[level]
    print(f"{mark}  {name}" + (f"\n        {detail}" if detail else ""))


def check_schema(conn):
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = TABLES - have
    if missing:
        report("FAIL", "테이블", f"없음: {sorted(missing)} — contracts/db_schema.sql 로 만드세요")
        return False
    report("PASS", "테이블 4개")

    cols = {c[1] for c in conn.execute("PRAGMA table_info(annual_fact)")}
    need = {"cik", "fiscal_year", "metric", "value"}
    if need - cols:
        report("FAIL", "annual_fact 컬럼", f"없음: {sorted(need - cols)}")
        return False
    report("PASS", "annual_fact 컬럼")
    return True


def check_metrics(conn):
    found = {r[0] for r in conn.execute("SELECT DISTINCT metric FROM annual_fact")}
    unknown = found - METRICS
    missing = METRICS - found
    if unknown:
        report("WARN", "metric 이름", f"계약에 없는 것이 섞였습니다: {sorted(unknown)} (엔진은 무시합니다)")
    if missing:
        report("FAIL", "metric 이름", f"없음: {sorted(missing)} — 이 지표는 계산이 안 됩니다")
    else:
        report("PASS", f"metric 7개 전부 존재")


def check_coverage(conn):
    n_co = conn.execute("SELECT count(*) FROM company").fetchone()[0]
    n_sic = conn.execute("SELECT count(*) FROM company WHERE sic IS NOT NULL").fetchone()[0]
    if n_co == 0:
        report("FAIL", "종목 수", "0개 — ETL 이 안 끝났습니다")
        return
    report("PASS" if n_co >= 1000 else "WARN", f"종목 {n_co:,}개",
           "" if n_co >= 1000 else "실데이터라기엔 적습니다 (NASDAQ+NYSE 는 5천 종목대)")
    ratio = n_sic / n_co
    report("PASS" if ratio > 0.9 else "WARN", f"SIC 있는 종목 {n_sic:,}개 ({ratio:.0%})",
           "" if ratio > 0.9 else "SIC 없으면 백분위 비교군을 못 만듭니다 (submissions.zip 확인)")

    for table, label in (("annual_fact", "재무"), ("price_snapshot", "가격"), ("macro", "거시")):
        n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        report("PASS" if n else "FAIL", f"{label} 행 {n:,}개",
               "" if n else f"{table} 가 비었습니다")


def check_traps(conn):
    """실데이터로 확인한 함정들. 여기서 걸리면 화면의 숫자가 틀립니다."""
    with_eq = {r[0] for r in conn.execute(
        "SELECT DISTINCT cik FROM annual_fact WHERE metric='equity'")}
    with_li = {r[0] for r in conn.execute(
        "SELECT DISTINCT cik FROM annual_fact WHERE metric='liabilities'")}
    gap = with_eq - with_li
    report("PASS" if not gap else "WARN", f"Liabilities 폴백 ({len(gap)}개 누락)",
           "" if not gap else
           f"자본은 있는데 부채가 없는 종목 {len(gap)}개 — assets - equity 폴백을 안 쓴 것 같습니다 "
           f"(예: {sorted(gap)[:3]}). 이 종목들은 안정성 축이 통째로 빕니다")

    # 중복 행을 세는 건 의미가 없습니다 — 스키마의 PRIMARY KEY 가 애초에 막습니다.
    # 대신 그 PK 가 실제로 있는지를 봅니다. 직접 만든 테이블이면 빠져 있을 수 있고,
    # 그러면 세그먼트 값이 조용히 중복으로 쌓입니다.
    pk = [c[1] for c in conn.execute("PRAGMA table_info(annual_fact)") if c[5]]
    want = ["cik", "fiscal_year", "metric"]
    report("PASS" if pk == want else "FAIL", f"annual_fact PRIMARY KEY {pk or '없음'}",
           "" if pk == want else
           f"{want} 여야 합니다. 없으면 세그먼트 값이 중복으로 쌓여도 안 걸립니다 "
           f"— contracts/db_schema.sql 을 그대로 쓰세요")

    neg = conn.execute(
        "SELECT count(*) FROM annual_fact WHERE metric='revenue' AND value < 0").fetchone()[0]
    report("PASS" if not neg else "WARN", f"음수 매출 {neg}개",
           "" if not neg else "매출이 음수인 행이 있습니다 — 태그를 잘못 골랐을 가능성")

    yrs = conn.execute("SELECT min(fiscal_year), max(fiscal_year) FROM annual_fact").fetchone()
    span = (yrs[1] - yrs[0] + 1) if yrs[0] else 0
    report("PASS" if span >= 4 else "FAIL", f"연도 범위 {yrs[0]}~{yrs[1]} ({span}년)",
           "" if span >= 4 else "3년 CAGR 에는 최소 4개 연도가 필요합니다")


def check_engine(db):
    """계약을 지켰어도 점수가 안 나오면 소용없습니다. 실제로 돌려봅니다."""
    try:
        e = Engine(db)
    except RuntimeError as ex:
        report("FAIL", "엔진 적재", str(ex).splitlines()[0])
        return

    scored = levels = 0
    lv = {}
    for t in e.by_ticker:
        r = e.score(t)
        if r.get("score") is not None:
            scored += 1
            pg = r.get("peer_group")
            if pg:
                lv[pg["level"]] = lv.get(pg["level"], 0) + 1
    total = len(e.by_ticker)
    ratio = scored / total if total else 0
    report("PASS" if ratio > 0.7 else "WARN", f"점수가 나오는 종목 {scored:,}/{total:,} ({ratio:.0%})",
           "" if ratio > 0.7 else "절반 넘게 점수가 안 나옵니다 — 재무 데이터가 부족합니다")

    names = {4: "SIC 4자리", 3: "3자리", 2: "2자리", 0: "전체 시장"}
    detail = " · ".join(f"{names[k]} {lv.get(k, 0):,}" for k in (4, 3, 2, 0))
    deep = lv.get(4, 0) / scored if scored else 0
    report("PASS" if deep > 0.5 else "WARN", "비교군 정밀도", detail +
           ("" if deep > 0.5 else "  <- 4자리로 비교되는 종목이 절반도 안 됩니다"))

    ind_ok = sum(1 for t in list(e.by_ticker)[:200]
                 for a in e.score(t)["axes"] for i in a["indicators"]
                 if i["status"] == "ok")
    ind_tot = 200 * len(INDICATORS)
    report("PASS" if ind_ok / ind_tot > 0.6 else "WARN",
           f"지표 계산 성공률 {ind_ok / ind_tot:.0%} (앞 200종목)",
           "" if ind_ok / ind_tot > 0.6 else "결측 지표가 많으면 화면이 빈칸투성이가 됩니다")


def main():
    db = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else default_db()
    print(f"\n검사 대상: {db}")
    if not db.exists():
        print(f"\n FAIL  파일이 없습니다.")
        print(f"       가짜 DB: python scoring/fake_db.py")
        print(f"       실DB   : data/stocks.db 에 파일을 놓으세요")
        return 1
    print(f"크기: {db.stat().st_size / 1e6:.1f} MB\n")

    conn = sqlite3.connect(db)
    try:
        if check_schema(conn):
            check_metrics(conn)
            check_coverage(conn)
            check_traps(conn)
    finally:
        conn.close()

    if not any(lv == "FAIL" for lv, _, _ in results):
        print()
        check_engine(db)

    fails = [n for lv, n, _ in results if lv == "FAIL"]
    warns = [n for lv, n, _ in results if lv == "WARN"]
    print(f"\n{'-' * 60}")
    if fails:
        print(f"FAIL {len(fails)}개 — 엔진이 이 DB를 못 씁니다: {fails}")
    elif warns:
        print(f"통과. 다만 WARN {len(warns)}개 — 돌아가지만 화면이 이상해질 수 있습니다")
    else:
        print("전부 통과. 이 DB로 붙여도 됩니다.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
