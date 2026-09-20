-- 데이터 레이어 → 점수 엔진 계약
--
-- 담당: DB 구축
-- 소비자: 점수 엔진
--
-- 이 스키마의 목적은 하나입니다: XBRL 태그 지옥을 데이터 레이어 안에 가두는 것.
-- 점수 엔진은 'RevenueFromContractWithCustomerExcludingAssessedTax' 같은 걸
-- 절대 보지 않습니다. 정규화된 metric 이름만 봅니다.

PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------
-- 기업 기본정보
--   출처: submissions.zip (1.56GB) — companyfacts.zip에는 SIC가 없습니다
-- ---------------------------------------------------------------
CREATE TABLE company (
    cik           TEXT PRIMARY KEY,        -- 10자리 0패딩. '0000320193'
    ticker        TEXT NOT NULL,           -- 'AAPL'
    name          TEXT NOT NULL,
    sic           TEXT,                    -- 4자리. '3571'  ※ NULL 가능
    sic_desc      TEXT,                    -- 'Electronic Computers'
    exchange      TEXT,                    -- 'Nasdaq' | 'NYSE'
    delisted_date TEXT                     -- Form 25 접수일. NULL이면 상장중
);
CREATE INDEX idx_company_ticker ON company(ticker);
CREATE INDEX idx_company_sic    ON company(sic);

-- ---------------------------------------------------------------
-- 연간 재무 팩트 (정규화 완료)
--
--   metric은 아래 7개만. 이 목록이 계약입니다. 추가하려면 먼저 합의하세요.
--     revenue             매출
--     operating_income    영업이익
--     net_income          순이익
--     assets              자산총계
--     liabilities         부채총계   ※ 아래 폴백 주의
--     equity              자본총계
--     operating_cashflow  영업활동현금흐름
--
--   [함정 1] 태그가 회사마다 다릅니다. TSLA는 Revenue 계열 태그가 17개입니다.
--            우선순위 사다리로 처리하고, 못 찾으면 행을 만들지 마세요(0을 넣지 마세요).
--              revenue: Revenues
--                     → RevenueFromContractWithCustomerExcludingAssessedTax
--                     → SalesRevenueNet
--
--   [함정 2] Liabilities 태그가 아예 없는 회사가 있습니다 (예: KO 코카콜라).
--            반드시 폴백: liabilities = assets - equity
--
--   [함정 3] 같은 태그에 세그먼트별 값이 섞여 들어옵니다.
--            연결 전체 값만 쓰세요 (프레임에 세그먼트 축이 없는 것).
-- ---------------------------------------------------------------
CREATE TABLE annual_fact (
    cik         TEXT NOT NULL REFERENCES company(cik),
    fiscal_year INTEGER NOT NULL,          -- 2025
    metric      TEXT NOT NULL,             -- 위 7개 중 하나
    value       REAL NOT NULL,             -- USD 원단위. 음수 허용(적자)
    source_tag  TEXT,                      -- 실제로 쓴 XBRL 태그. 디버깅·근거표시용
    PRIMARY KEY (cik, fiscal_year, metric)
);

-- ---------------------------------------------------------------
-- 가격 스냅샷 (yfinance 또는 야후 chart API)
--   밸류에이션(PER/PBR)과 이동평균 경고에 사용
--   실시간이 아니어도 됩니다. 짧은 캐시로 충분합니다.
-- ---------------------------------------------------------------
CREATE TABLE price_snapshot (
    ticker       TEXT NOT NULL,
    asof         TEXT NOT NULL,            -- ISO 날짜
    close        REAL NOT NULL,
    market_cap   REAL,                     -- NULL 가능 → PER/PBR 계산 불가 처리
    ma20         REAL,
    ma60         REAL,
    ma110        REAL,
    aligned_days INTEGER,                  -- 정배열(20>60>110) 지속일. 아니면 NULL
    PRIMARY KEY (ticker, asof)
);

-- ---------------------------------------------------------------
-- 거시 지표 (FRED)
--   종목 점수에는 들어가지 않습니다. 화면 상단 컨텍스트 + LLM 입력 전용.
-- ---------------------------------------------------------------
CREATE TABLE macro (
    series_id TEXT NOT NULL,               -- 'FEDFUNDS', 'CPIAUCSL', 'UNRATE'
    date      TEXT NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (series_id, date)
);
