/* 우리 API 응답 -> StockMate 화면이 기대하는 stockData 모양으로 변환합니다.
 *
 * 왜 있나
 *   담당 ③ 의 화면은 4종목(AAPL/NVDA/TSLA)이 하드코딩돼 있고 API 를 안 부릅니다.
 *   화면 구조를 다시 짜는 대신 어댑터를 끼우면 4,584종목이 그대로 돕니다.
 *   시간이 없을 때의 다리이지, 최종 구조는 아닙니다.
 *
 * 쓰는 법 — 화면의 stockData 를 통째로 안 고치고 덮어씁니다:
 *
 *   <script src="adapter.js"></script>
 *   const s = await loadStock('AAPL');
 *   stockData[s.ticker] = s;          // 그 다음 기존 렌더 함수를 그대로 호출
 *
 * 채워지는 것 / 안 채워지는 것은 아래 toStockData 주석 참고.
 */

async function loadStock(ticker) {
  const r = await fetch('/api/score?ticker=' + encodeURIComponent(ticker));
  const score = await r.json();
  if (score.error) throw new Error(ticker + ': ' + score.error);
  return toStockData(score);
}

/* 의견(문장)은 SSE 로 따로 옵니다. 점수 화면은 즉시 뜨고 글만 흘러나옵니다.
 * onScore/onDelta/onOpinion 중 필요한 것만 주면 됩니다. */
function streamOpinion(ticker, { onScore, onDelta, onOpinion, onDone, onError } = {}) {
  const es = new EventSource('/api/opinion?ticker=' + encodeURIComponent(ticker));
  es.addEventListener('score', e => onScore && onScore(toStockData(JSON.parse(e.data))));
  // delta 는 구조화 출력이라 JSON 문자열 조각입니다. 사람이 읽을 문장이 아니니
  // 진행 표시에만 쓰고, 렌더링은 opinion 이벤트의 파싱된 객체로 하세요.
  es.addEventListener('delta', e => onDelta && onDelta(JSON.parse(e.data).text));
  es.addEventListener('opinion', e => onOpinion && onOpinion(JSON.parse(e.data)));
  es.addEventListener('error', e => { es.close(); onError && onError(e); });
  es.addEventListener('done', () => { es.close(); onDone && onDone(); });
  return () => es.close();
}

const find = (score, id) => {
  for (const a of score.axes || []) {
    for (const i of a.indicators || []) if (i.id === id) return i;
  }
  return null;
};

/* peer_deciles 는 10칸인데 화면 히스토그램은 7칸입니다. 인접 칸을 합쳐 줄입니다. */
function toSevenBins(deciles) {
  if (!deciles) return [0, 0, 0, 0, 0, 0, 0];
  const out = new Array(7).fill(0);
  deciles.forEach((v, i) => { out[Math.min(6, Math.floor(i * 7 / 10))] += v; });
  return out;
}

const VERDICT = [
  [80, '재무는 튼튼', 'text-emerald-700 bg-emerald-100/80'],
  [60, '무난한 편', 'text-sky-700 bg-sky-100/80'],
  [40, '신중한 관망', 'text-amber-700 bg-amber-100/80'],
  [0, '약점이 보임', 'text-rose-700 bg-rose-100/80'],
];

function toStockData(score) {
  const op = find(score, 'profitability.operating_margin');
  const per = find(score, 'valuation.per');
  const band = VERDICT.find(v => (score.score ?? 0) >= v[0]);

  return {
    // --- API 에서 그대로 오는 것 ---
    ticker: score.ticker,
    name: score.name,
    shortName: score.name,
    score: score.score,
    temp: score.score,
    rank: score.rank_text,

    opProfit: op && op.status === 'ok' ? op.percentile : null,
    perPercent: per && per.status === 'ok' ? per.percentile : null,
    opDesc: op && op.status === 'ok'
      ? `${op.display} 입니다. 같은 업종 중앙값은 ${op.peer_median_display} 입니다.`
      : (op ? op.reason : '계산할 수 없습니다'),
    perDesc: per && per.status === 'ok'
      ? `${per.display} 입니다. 같은 업종 중앙값은 ${per.peer_median_display} 입니다.`
      : (per ? per.reason : '계산할 수 없습니다'),

    histogram: toSevenBins((op || per || {}).peer_deciles),
    myPositionIndex: Math.min(6, Math.floor(((op || per || {}).percentile ?? 50) * 7 / 100)),

    verdict: band[1],
    verdictColor: band[2],

    // --- 화면에 꼭 띄워야 하는 것 (계약에 있는데 기존 화면이 안 쓰던 것들) ---
    // 이걸 숨기면 거짓말이 됩니다. 담당 ③ 가 자리를 만들어 주세요.
    flags: score.flags || [],                    // 상장폐지·더미가격·정배열 경고
    scoreBasis: score.score_basis,               // "4축 중 3축으로 낸 점수"
    peerLabel: (score.peer_group || {}).label,   // "전체 시장" 과 "동종업계 58개" 는 다릅니다
    peerCount: (score.peer_group || {}).n,
    axes: score.axes || [],                      // 좌측 드릴다운 원본

    // --- API 가 안 주는 것 (화면 고유. 기본값을 둡니다) ---
    avatar: '📊',
    tagline: (score.peer_group || {}).label || '',
    phrase1: null,                               // streamOpinion 의 opinion 이벤트로 채우세요
    phrase2: null,
    summary: null,
    responses: null,                             // 챗봇 상용구는 화면 쪽 자산입니다
  };
}

if (typeof module !== 'undefined') {
  module.exports = { toStockData, toSevenBins, find };
}
