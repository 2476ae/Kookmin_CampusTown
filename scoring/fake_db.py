"""가짜 DB 생성기. 담당 ①의 실DB를 기다리지 않고 점수 엔진을 짜기 위한 것.

contracts/db_schema.sql 을 그대로 읽어서 만듭니다 — 계약이 바뀌면 여기도 같이 바뀝니다.

일부러 섞는 예외 케이스 (나중에 실데이터에서 터지는 것보다 지금 터지는 게 낫습니다):
  - 신규 상장     : 2년치만 → 3년 CAGR 계산 불가 → 성장성 축 결측 (S5)
  - 적자 기업     : PER·ROE 백분위 0점 고정 대상
  - 3년 전 적자   : 영업이익 CAGR 계산 불가 (음수에서 CAGR 못 냄)
  - 상장폐지      : delisted_date 있음 → 점수 없음 (S6)
  - 폐지 예정     : Form 25 접수됐지만 아직 거래 중 → 점수 + 최대 경고
  - 시총 없음     : 밸류에이션 축 통째로 결측
  - 자본잠식      : equity<0 -> 부채비율·PBR·ROE가 전부 뒤집히는 가장 위험한 덫
  - 표본 부족 업종 : 12개뿐인 SIC → 폴백 사다리 작동 확인
"""
import random, sqlite3, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEED = 20260920  # 고정. 결정성 테스트가 이것에 의존합니다.

# (sic, 설명, 회사수, 티커접두사)
#
# SIC 폴백 사다리(4자리 -> 3자리 -> 2자리 -> 전체)의 네 칸을 전부 밟게 배치합니다.
# 처음엔 SIC 다섯 개가 2자리 접두사를 공유하지 않아서 4자리에서 곧장 전체 시장으로
# 떨어졌고, 중간 두 칸이 한 번도 실행되지 않았습니다 — 실데이터에는 3571·3572·3576
# 처럼 붙어 있는 업종이 널렸으니 거기서 처음 돌아갈 뻔했습니다.
#
# 티커 접두사는 SIC 와 무관하게 유일해야 합니다. sic[:2] 를 쓰면 3571 과 3572 가
# 둘 다 "35" 가 되어 충돌합니다.
SICS = [("3571", "Electronic Computers", 58, "35"),          # 4자리로 충분
        ("7372", "Prepackaged Software", 80, "73"),
        ("2834", "Pharmaceutical Preparations", 45, "28"),
        ("1311", "Crude Petroleum & Natural Gas", 35, "13"),
        # 357x: 개별로는 30 미만이지만 3자리(357)로 묶으면 넘습니다 -> 3자리 폴백
        ("3572", "Computer Storage Devices", 14, "36"),
        ("3576", "Computer Communications Equipment", 12, "37"),
        # 38xx: 3자리로도 모자라고 2자리(38)에서야 넘습니다 -> 2자리 폴백
        ("3826", "Laboratory Analytical Instruments", 12, "38"),
        ("3812", "Search & Navigation Equipment", 11, "39"),
        ("3844", "X-Ray Apparatus", 10, "40"),
        ("3861", "Photographic Equipment", 10, "41"),
        # 어디에도 안 붙는 업종 -> 전체 시장 폴백
        ("0912", "Fish & Seafood", 9, "09")]

YEARS = (2022, 2023, 2024, 2025)


def build(path=None):
    path = pathlib.Path(path or ROOT / "data" / "fake.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "contracts" / "db_schema.sql").read_text(encoding="utf-8"))

    rnd = random.Random(SEED)
    n = 0
    for sic, desc, count, prefix in SICS:
        for i in range(count):
            n += 1
            cik = f"{n:010d}"
            tick = f"{prefix}{i:03d}"
            # 예외 케이스를 결정적으로 배치
            kind = "normal"
            if i == 0 and sic == "3571": kind = "new_listing"      # 2년치만
            elif i == 1 and sic == "3571": kind = "loss"            # 적자
            elif i == 2 and sic == "3571": kind = "was_loss"        # 3년 전 적자
            elif i == 3 and sic == "3571": kind = "delisted"        # 폐지됨
            elif i == 4 and sic == "3571": kind = "delisting_soon"  # 폐지 예정
            elif i == 5 and sic == "3571": kind = "no_price"        # 시총 없음
            elif i == 6 and sic == "3571": kind = "negative_equity"  # 자본잠식

            delisted = {"delisted": "2025-03-14", "delisting_soon": "2026-10-01"}.get(kind)
            conn.execute("INSERT INTO company VALUES (?,?,?,?,?,?,?)",
                         (cik, tick, f"Fake {tick} Inc.", sic, desc,
                          "Nasdaq" if n % 2 else "NYSE", delisted))

            scale = rnd.lognormvariate(20, 1.2)
            margin = rnd.gauss(0.12, 0.09)
            growth = rnd.gauss(0.06, 0.10)
            years = YEARS[-2:] if kind == "new_listing" else YEARS
            for y in years:
                k = (1 + growth) ** (y - YEARS[0])
                rev = scale * k
                op = rev * margin
                if kind == "loss": op = -abs(op)
                if kind == "was_loss" and y == YEARS[0]: op = -abs(op)
                net = op * rnd.uniform(0.6, 0.85)
                eq = rev * rnd.uniform(0.3, 1.5)
                if kind == "negative_equity": eq = -abs(eq) * 0.4
                assets = eq * rnd.uniform(1.4, 5.0)
                rows = {"revenue": rev, "operating_income": op, "net_income": net,
                        "assets": assets, "liabilities": assets - eq, "equity": eq,
                        "operating_cashflow": net * rnd.uniform(0.8, 1.6)}
                for m, v in rows.items():
                    conn.execute("INSERT INTO annual_fact VALUES (?,?,?,?,?)",
                                 (cik, y, m, v, "fake"))

            if kind != "no_price":
                close = rnd.uniform(8, 400)
                shares = scale / close * rnd.uniform(8, 30)
                aligned = rnd.choice([None, 3, 40, 112, 250])
                conn.execute(
                    "INSERT INTO price_snapshot "
                    "(ticker, asof, close, market_cap, ma20, ma60, ma110, aligned_days) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (tick, "2026-09-20", close, close * shares,
                     close * .98, close * .95, close * .92, aligned))

    for sid, val in (("FEDFUNDS", 4.25), ("CPIAUCSL", 3.1), ("UNRATE", 4.4)):
        conn.execute("INSERT INTO macro VALUES (?,?,?)", (sid, "2026-08-01", val))

    conn.commit()
    return conn, path


if __name__ == "__main__":
    conn, path = build()
    counts = {
        "companies":     "SELECT count(*) FROM company",
        "facts":         "SELECT count(*) FROM annual_fact",
        "prices":        "SELECT count(*) FROM price_snapshot",
        "delisted":      "SELECT count(*) FROM company WHERE delisted_date IS NOT NULL",
        "loss-making":   "SELECT count(DISTINCT cik) FROM annual_fact "
                         "WHERE metric='operating_income' AND value<0",
        "short-history": "SELECT count(*) FROM (SELECT cik FROM annual_fact "
                         "GROUP BY cik HAVING count(DISTINCT fiscal_year)<4)",
        "no-price":      "SELECT count(*) FROM company c WHERE NOT EXISTS "
                         "(SELECT 1 FROM price_snapshot p WHERE p.ticker=c.ticker)",
        "neg-equity":    "SELECT count(DISTINCT cik) FROM annual_fact "
                         "WHERE metric='equity' AND value<0",
    }
    print("built", path)
    for label, sql in counts.items():
        print(f"  {label:14} {conn.execute(sql).fetchone()[0]}")
