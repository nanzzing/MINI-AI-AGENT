// 동일한 조정 종가 이력으로 계산한다. 준비 기간에는 값을 만들지 않는다.
window.calculateIndicators = (bars) => {
  // 입력: 시간 오름차순 OHLCV 목록. 출력: 지표별 [{time, value}] 목록.
  // 이 계산은 차트 표시용이다. 모델에게 전달하는 기술적 분석은 technicals.py가 계산한다.
  // 월봉 모드에서는 '기간'이 일수가 아니라 월봉 개수로 해석된다.
  const result = {
    rsi: [],
    macd: [],
    signal: [],
    histogram: [],
    upper: [],
    middle: [],
    lower: [],
  };
  let fast,
    slow,
    signal,
    gain = 0,
    loss = 0,
    sum = 0;
  bars.forEach((bar, i) => {
    // EMA 갱신식: 이전 EMA + (현재 종가 - 이전 EMA) * 2/(기간+1).
    const close = bar.close,
      time = bar.time;
    fast = i ? fast + ((close - fast) * 2) / 13 : close;
    slow = i ? slow + ((close - slow) * 2) / 27 : close;
    if (i >= 25) {
      // MACD는 26개 봉 이후, signal/histogram은 9개 MACD 준비 기간 이후 표시한다.
      const line = fast - slow;
      signal = i === 25 ? line : signal + ((line - signal) * 2) / 10;
      result.macd.push({ time, value: line });
      if (i >= 33) {
        result.signal.push({ time, value: signal });
        result.histogram.push({
          time,
          value: line - signal,
          color: line >= signal ? "#21835e88" : "#d6757088",
        });
      }
    }
    if (i) {
      // RSI는 상승분과 하락분의 평균 비율이다. 최초 14개 변화는 단순평균,
      // 그 뒤는 Wilder 방식으로 이전 평균에 13/14 가중치를 준다.
      const delta = close - bars[i - 1].close,
        g = Math.max(delta, 0),
        l = Math.max(-delta, 0);
      if (i <= 14) {
        gain += g / 14;
        loss += l / 14;
      } else {
        gain = (gain * 13 + g) / 14;
        loss = (loss * 13 + l) / 14;
      }
      if (i >= 14)
        result.rsi.push({
          time,
          value: loss ? 100 - 100 / (1 + gain / loss) : gain ? 100 : 50,
        });
    }
    sum += close;
    // 최근 20개 종가 합을 유지하는 슬라이딩 윈도. 매번 전체 합을 다시 구하지 않는다.
    if (i >= 20) sum -= bars[i - 20].close;
    if (i >= 19) {
      const mean = sum / 20,
        sd = Math.sqrt(
          bars
            .slice(i - 19, i + 1)
            .reduce((s, b) => s + (b.close - mean) ** 2, 0) / 20,
        );
      result.middle.push({ time, value: mean });
      // 볼린저 중간선은 평균, 위/아래 선은 평균 ± 모집단 표준편차 2배다.
      result.upper.push({ time, value: mean + 2 * sd });
      result.lower.push({ time, value: mean - 2 * sd });
    }
  });
  return result;
};
