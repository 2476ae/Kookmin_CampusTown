# 셋 병합 — 지금 상태와 남은 것

2026-09-20 기준.

## git 병합은 이미 끝났습니다

셋 다 `main` 에 있고 충돌도 없었습니다. 서로 다른 파일을 건드려서입니다.

| | 어디에 | 상태 |
|---|---|---|
| 담당 ① | `전체 프로젝트 현재 실황` | **보고서만.** 코드·DB 미도착 |
| 담당 ② | `scoring/` `llm/` `api/` `etl/` `contracts/` | 검사 39개 통과 |
| 담당 ③ | `web/index.html` | 화면 완성. **API 미연결** |

남은 건 git 작업이 아니라 **배선 두 군데**입니다.

---

## 1. 담당 ③ 화면 ↔ API — 6줄

화면은 `stockData` 라는 객체에서 종목을 꺼내 쓰는데, 지금 3종목이 하드코딩돼
있습니다. **그 객체를 API 로 채우면** 4,584종목이 그대로 돕니다.
렌더링 코드는 한 줄도 안 고칩니다.

### 1-1. `<head>` 에 어댑터 한 줄

```html
<script src="adapter.js"></script>
```

### 1-2. `selectStockAndAnalyze` 를 async 로

지금:

```js
function selectStockAndAnalyze(ticker, customUserQuestion = null) {
  if (!stockData[ticker]) {
    /* 못 찾음 처리 */
    return;
  }
  currentStockKey = ticker;
  const data = stockData[ticker];
```

바꾼 뒤:

```js
async function selectStockAndAnalyze(ticker, customUserQuestion = null) {
  if (!stockData[ticker]) {
    try {
      stockData[ticker] = await loadStock(ticker);   // adapter.js 가 API 를 부릅니다
    } catch (e) {
      /* 기존 못 찾음 처리 그대로 */
      return;
    }
  }
  currentStockKey = ticker;
  const data = stockData[ticker];
```

부르는 쪽(`triggerSampleQuestion`, `routeAndAnalyze`)은 **안 고쳐도 됩니다** —
async 함수를 await 없이 불러도 동작합니다.

### 1-3. 의견 문장은 스트리밍으로

점수는 즉시 뜨고 글만 흘러나옵니다. 30초 빈 화면이 안 생깁니다.

```js
streamOpinion(ticker, {
  onOpinion: op => {
    // op.sections[].sentences[] 에 text 와 evidence 가 있습니다
    // evidence ID 를 클릭하면 좌측 근거 카드로 점프
  },
  onDelta: () => showTypingIndicator(),   // 진행 표시용. JSON 조각이라 사람이 읽을 글이 아닙니다
});
```

### 1-4. 화면에 꼭 띄워야 하는 것 3개

어댑터가 넘겨주지만 지금 화면에 자리가 없습니다. **숨기면 거짓말이 됩니다.**

| | 왜 |
|---|---|
| `flags` | `flag.dummy_price` = "가격이 샘플이라 PER·PBR 은 진짜가 아닙니다". 상폐 경고도 여기 |
| `scoreBasis` | "4축 중 3축으로 낸 점수". 3축 점수와 4축 점수는 신뢰도가 다릅니다 |
| `peerLabel` | "동종업계 58개" 와 "전체 시장 4,584개" 는 다른 말입니다 |

### 확인

```bash
python api/server.py     # http://localhost:8000
```

화면과 API 가 같은 주소에서 뜹니다. CORS 없이 상대경로로 부르면 됩니다.

---

## 2. 담당 ① DB — 아직 안 왔습니다

[handoff-checklist.md](handoff-checklist.md) 로 요청해둔 파일 2개:
`stocks.db` (약 8MB) + `delisted.csv` (수십 KB).

도착 전까지는 SEC 공개 데이터로 만든 DB 로 돌아갑니다:

```bash
python etl/export_to_contract.py --from-zip 2026q1.zip 2025q4.zip --out data/stocks.db
python etl/fetch_macro.py data/stocks.db
python etl/fill_dummy_prices.py data/stocks.db
python scoring/check_db.py data/stocks.db
```

---

## 지금 무엇이 진짜인가

| | 상태 |
|---|---|
| 수익성 · 성장성 · 안정성 (지표 6개) | ✅ 진짜 (SEC) |
| 밸류에이션 (PER · PBR) | ❌ **가짜** — 전종목 종가가 없습니다 |
| 거시 (금리 · 물가 · 실업률) | ✅ 진짜 (FRED) |
| 상장폐지 배지 | ❌ 담당 ① 의 `delisted.csv` 대기 |
| 이동평균 경고 | ❌ 가짜 |

막힌 건 **전종목 최신 종가 하나**입니다. 발행주식수는 SEC 가 주니
`종가 × 주식수 = 시가총액` 이고, 종가만 생기면 밸류에이션 축이 살아납니다.

---

## 커밋할 때 주의

**`git add -A` 를 쓰지 마세요.** 남이 편집 중인 파일이 내 커밋에 딸려 들어갑니다.
실제로 한 번 일어났습니다 — 담당 ③ 의 파일 이동이 담당 ② 의 FRED 커밋에
묶여버렸습니다. 경로를 명시하세요.

```bash
git add scoring/ llm/ api/        # 내가 건드린 것만
```
