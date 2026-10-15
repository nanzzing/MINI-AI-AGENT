"""전체 가격 이력으로 지표를 계산하고 모델에는 수치와 판정 근거를 전달한다."""
from datetime import datetime
from zoneinfo import ZoneInfo
import math

import pandas as pd


def analyze(prices, today=None):
    """price_history의 bars를 받아 모델용 지표 사전을 반환한다.

    today를 주입할 수 있게 한 이유는 테스트에서 '오늘'을 고정하기 위해서다.
    브라우저 indicators.js와 달리 당일 봉·현재 월봉을 제외하고 분석하므로
    화면의 최신 지표와 값이 다를 수 있다. 계산 불가능한 항목은 None(JSON null)이다.
    """
    bars = prices.get("bars", [])
    if not bars:
        return {"status": "unavailable", "reason": "가격 데이터 없음"}
    today = today or datetime.now(ZoneInfo(prices.get("timezone") or "UTC")).date()
    frame = pd.DataFrame(bars).drop_duplicates("time").sort_values("time")
    # rolling/ewm 계산은 행 순서에 영향을 받으므로 중복 날짜를 제거하고 시간순으로
    # 정렬한다. 충분한 과거 봉이 없으면 계산 결과 NaN을 아래 value가 None으로 바꾼다.
    # 당일 일봉은 장중일 수 있으므로 확정된 이전 거래일까지 사용한다.
    frame = frame[frame.time < today.isoformat()]
    if frame.empty:
        return {"status": "unavailable", "reason": "완료된 일봉 없음"}
    close = frame.close.astype(float)

    def value(x):
        # pandas의 NaN을 None으로 바꾸고 정상 숫자의 과도한 소수 자릿수를 줄인다.
        return round(float(x), 6) if pd.notna(x) and math.isfinite(float(x)) else None

    def change(series, days):
        # 현재 값과 days개 전 값을 비교한 변화율(%). 필요한 이력이 없으면 None이다.
        return value((series.iloc[-1] / series.iloc[-days-1] - 1) * 100) if len(series) > days and series.iloc[-days-1] else None

    ma = {}
    for window in (20, 60, 120, 200):
        # SMA는 최근 window개 종가의 단순평균이다. slope는 이평선 자체의 5일 변화,
        # distance는 현재 종가가 그 이평선에서 얼마나 떨어져 있는지 나타낸다.
        average = close.rolling(window).mean()
        latest = value(average.iloc[-1])
        ma[str(window)] = {"value": latest, "slope_5_sessions_pct": change(average, 5),
                           "distance_pct": value((close.iloc[-1] / latest - 1) * 100) if latest else None}
    values = [ma[str(w)]["value"] for w in (20, 60, 120, 200)]
    ordered = all(a > b for a, b in zip(values, values[1:])) if all(v is not None for v in values) else None
    # 정배열은 20 > 60 > 120 > 200을 모두 충족해야 True다. 데이터 부족은 False와 다르다.

    # Wilder RSI: 첫 14개 변화의 단순평균으로 시작하고 이후 1/14로 평활한다.
    rsi = None
    delta = close.diff().dropna()
    if len(delta) >= 14:
        gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
        avg_gain, avg_loss = gain.iloc[:14].mean(), loss.iloc[:14].mean()
        for g, l in zip(gain.iloc[14:], loss.iloc[14:]):
            avg_gain, avg_loss = (avg_gain * 13 + g) / 14, (avg_loss * 13 + l) / 14
        rsi = value(100 - 100 / (1 + avg_gain / avg_loss)) if avg_loss else 100.0 if avg_gain else 50.0
    macd = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(span=26, adjust=False, min_periods=26).mean()
    # MACD=12일 EMA-26일 EMA, signal=MACD의 9일 EMA, histogram=둘의 차이다.
    # EMA는 최근 값에 더 큰 가중치를 주는 이동평균이며 SMA와 계산 방식이 다르다.
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    middle, std = close.rolling(20).mean(), close.rolling(20).std(ddof=0)
    upper, lower = middle + 2 * std, middle - 2 * std
    # 볼린저 밴드는 20일 평균 ± 표준편차 2배다. ddof=0으로 모집단 표준편차를 쓴다.
    last = float(close.iloc[-1])
    band_position = None if pd.isna(upper.iloc[-1]) else "above_upper" if last > upper.iloc[-1] else "below_lower" if last < lower.iloc[-1] else "inside"

    # 각 월 마지막 거래일 종가로 월봉을 구성하고 현재 월은 무조건 제외한다.
    monthly = frame.assign(month=frame.time.str[:7]).groupby("month").close.last()
    monthly = monthly[monthly.index < today.strftime("%Y-%m")]
    monthly_ma = monthly.rolling(5).mean()
    months = [{"month": month, "close": value(c), "sma5": value(m), "above": bool(c > m) if pd.notna(m) else None}
              for month, c, m in zip(monthly.index, monthly, monthly_ma)]
    streak = 0
    for row in reversed(months):
        if row["above"] is not True:
            break
        streak += 1
    qualified = streak >= 3 if len(months) >= 7 else None
    prior = frame.iloc[-21:-1]
    # 최신 봉을 제외한 직전 20거래일로 거래량 평균과 지지·저항 참고 구간을 만든다.
    # 지지·저항은 최저/최고가의 단순 대용치이며 추세선 분석이나 매매 신호가 아니다.
    volume_mean = prior.volume.mean() if len(prior) == 20 else None
    return {"status": "ok" if len(frame) >= 205 and qualified is not None else "partial",
            "as_of": frame.time.iloc[-1], "source": prices.get("source"), "currency": prices.get("currency"),
            "basis": "배당·분할 조정 일봉. 당일 일봉 제외. 모든 변화율은 %. 월봉은 현재 월 제외.",
            "bars_used": len(frame), "close": value(last), "moving_averages": ma, "bullish_ma_alignment": ordered,
            "momentum_returns_pct": {str(w): change(close, w) for w in (5, 20, 60, 120, 252)},
            "rsi14": rsi, "macd_12_26_9": {"line": value(macd.iloc[-1]), "signal": value(signal.iloc[-1]), "histogram": value((macd-signal).iloc[-1])},
            "bollinger_20_2": {"upper": value(upper.iloc[-1]), "middle": value(middle.iloc[-1]), "lower": value(lower.iloc[-1]), "position": band_position},
            "volume": {"latest": value(frame.volume.iloc[-1]), "prior20_average": value(volume_mean), "ratio": value(frame.volume.iloc[-1]/volume_mean) if volume_mean else None},
            "volatility_20_annualized_pct": value(close.pct_change().rolling(20).std(ddof=1).iloc[-1] * math.sqrt(252) * 100),
            "support_resistance_proxy": {"method": "직전 20거래일 저가 최솟값·고가 최댓값. 확정 지지·저항이 아닌 참고 구간.",
                                         "support": value(prior.low.min()) if len(prior)==20 else None,
                                         "resistance": value(prior.high.max()) if len(prior)==20 else None},
            "monthly_settlement": {"satisfied": qualified, "consecutive_months": streak, "last_three": months[-3:],
                                   "rule": "최근 완료 월봉 3개 종가가 각 해당 월 포함 5개월 SMA보다 엄격히 높음. 같으면 불충족."}}
