"""상승·횡보·부족 이력과 미완료 월 제외를 검증한다."""
import unittest
from datetime import date
import json
import pandas as pd
from mini_ai_agent.technicals import analyze, _regular_divergence, _rsi_series


def prices(flat=False):
    # 외부 시세 대신 상승/횡보 합성 일봉을 만든다. 계산 결과를 예측할 수 있어 경계값 검증에 좋다.
    bars = []
    for i, day in enumerate(pd.bdate_range('2023-01-01', '2025-10-15')):
        close = 100 if flat else 100 + i
        bars.append(dict(time=day.date().isoformat(), open=close, high=close+1, low=close-1, close=close, volume=1000))
    return {'bars': bars, 'timezone': 'Asia/Seoul', 'source': 'fixture', 'currency': 'KRW'}


class TechnicalTests(unittest.TestCase):
    def test_rising_history(self):
        data = analyze(prices(), date(2025, 10, 15))
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(data['bullish_ma_alignment'])
        self.assertEqual(data['rsi14'], 100)
        self.assertTrue(data['monthly_settlement']['satisfied'])
        self.assertEqual(data['monthly_settlement']['last_three'][-1]['month'], '2025-09')
        self.assertEqual(data['as_of'], '2025-10-14')
        self.assertEqual(data['volume']['ratio'], 1)
        self.assertAlmostEqual(data['close'] - data['moving_averages']['20']['value'], 9.5)
        json.dumps(data, allow_nan=False)

    def test_equal_is_not_above(self):
        data = analyze(prices(True), date(2025, 10, 15))
        self.assertFalse(data['monthly_settlement']['satisfied'])
        self.assertFalse(data['bullish_ma_alignment'])
        self.assertEqual(data['rsi14'], 50)
        self.assertEqual(data['macd_12_26_9']['histogram'], 0)
        self.assertEqual(data['volatility_20_annualized_pct'], 0)

    def test_incomplete_month_does_not_change_settlement(self):
        source = prices()
        original = analyze(source, date(2025, 10, 15))['monthly_settlement']
        for bar in source['bars']:
            if bar['time'].startswith('2025-10'):
                bar['close'] = 1
        self.assertEqual(analyze(source, date(2025, 10, 15))['monthly_settlement'], original)

    def test_short_history_is_partial(self):
        source = prices()
        source['bars'] = source['bars'][:5]
        data = analyze(source, date(2025, 10, 15))
        self.assertEqual(data['status'], 'partial')
        self.assertIsNone(data['bullish_ma_alignment'])
        self.assertIsNone(data['rsi14'])
        self.assertIsNone(data['monthly_settlement']['satisfied'])
        for key in ('rsi_recent', 'macd_recent'):
            self.assertEqual(data[key]['status'], 'unavailable')
            for window in data[key]['windows'].values():
                self.assertEqual(window['sessions_examined'], 5)
        for key in ('rsi_divergence', 'macd_divergence'):
            for direction in ('bullish', 'bearish'):
                self.assertEqual(data[key][direction]['status'], 'unavailable')
                self.assertIsNone(data[key][direction]['detected'])
        json.dumps(data, allow_nan=False)
        self.assertEqual(analyze({'bars': []})['status'], 'unavailable')

    def test_rsi_series_preserves_wilder_seed_and_smoothing(self):
        close = pd.Series([100 + i + 3 * (-1)**i for i in range(40)], dtype=float)
        series = _rsi_series(close)
        self.assertTrue(series.iloc[:14].isna().all())
        # 기존 최신값 계산식과 각 날짜 결과를 비교해 전체 이력으로 바꿔도 값이 같은지 검사한다.
        for end in range(15, len(close)+1):
            delta = close.iloc[:end].diff().dropna()
            gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
            avg_gain, avg_loss = gain.iloc[:14].mean(), loss.iloc[:14].mean()
            for g, l in zip(gain.iloc[14:], loss.iloc[14:]):
                avg_gain, avg_loss = (avg_gain * 13 + g) / 14, (avg_loss * 13 + l) / 14
            self.assertAlmostEqual(series.iloc[end-1], 100 - 100 / (1 + avg_gain / avg_loss))

    def test_analysis_divergences_use_completed_bars_and_correct_indicator(self):
        source = prices()
        for index, bar in enumerate(source['bars']):
            close = 100 + (index % 20 - 10)**2
            bar.update(close=close, high=close+1, low=close-1)
        today = date(2025, 10, 15)
        data = analyze(source, today)
        frame = pd.DataFrame(source['bars'][:-1])
        close = frame.close.astype(float)
        series = {'rsi_divergence': _rsi_series(close), 'macd_divergence':
                  close.ewm(span=12, adjust=False, min_periods=12).mean()
                  - close.ewm(span=26, adjust=False, min_periods=26).mean()}
        for key, indicator in series.items():
            for direction in ('bullish', 'bearish'):
                result = data[key][direction]
                self.assertEqual(result['status'], 'ok')
                self.assertIn(result['lookback_sessions'], (20, 40))
                self.assertEqual(result['max_lookback_sessions'], 40)
                for pivot in result['pivots']:
                    index = frame.index[frame.time == pivot['time']][0]
                    self.assertEqual(pivot['indicator'], round(indicator.iloc[index], 6))
                    self.assertLess(pivot['confirmed_at'], today.isoformat())
        source['bars'][-1].update(close=100000, high=100000, low=0)
        new = analyze(source, today)
        for key in series:
            self.assertEqual(new[key], data[key])
        json.dumps(data, allow_nan=False)

    def test_recent_summaries_limit_interpretation_but_preserve_full_indicator_calculation(self):
        source = prices()
        for index, bar in enumerate(source['bars']):
            bar['close'] = 100 + index * 0.1 + 15 * (-1)**(index // 9)
        frame = pd.DataFrame(source['bars'][:-1])
        close = frame.close.astype(float)
        rsi = _rsi_series(close)
        macd = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(span=26, adjust=False, min_periods=26).mean()
        signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
        indicators = {'rsi_recent': {'rsi14': rsi}, 'macd_recent': {'line': macd, 'signal': signal, 'histogram': macd-signal}}
        data = analyze(source, date(2025, 10, 15))
        self.assertEqual(data['rsi14'], round(rsi.iloc[-1], 6))
        self.assertEqual(data['macd_12_26_9']['line'], round(macd.iloc[-1], 6))
        self.assertEqual(data['bars_used'], len(frame))
        for key, series_by_name in indicators.items():
            self.assertEqual(data[key]['status'], 'ok')
            self.assertEqual(set(data[key]['windows']), {'20', '40'})
            for size in (20, 40):
                window = data[key]['windows'][str(size)]
                self.assertEqual(window['from'], frame.time.iloc[-size])
                self.assertEqual(window['to'], frame.time.iloc[-1])
                self.assertEqual(window['sessions_examined'], size)
                for name, series in series_by_name.items():
                    recent = series.iloc[-size:]
                    self.assertEqual(window['values'][name], {
                        'valid_sessions': size, 'first': round(recent.iloc[0], 6),
                        'latest': round(recent.iloc[-1], 6), 'min': round(recent.min(), 6),
                        'max': round(recent.max(), 6)})
        # 분석 범위가 짧아져도 SMA200과 RSI/MACD 준비 이력을 잘라내지 않는다.
        self.assertEqual(data['moving_averages']['200']['value'], round(close.iloc[-200:].mean(), 6))
        self.assertNotEqual(data['rsi14'], round(_rsi_series(close.iloc[-40:]).iloc[-1], 6))
        source['bars'][-1]['close'] = 100000
        changed = analyze(source, date(2025, 10, 15))
        for key in indicators:
            self.assertEqual(changed[key], data[key])

    def test_recent_summaries_report_warmup_and_missing_sessions(self):
        source = prices()
        source['bars'] = source['bars'][:30]
        data = analyze(source, date(2025, 10, 15))
        self.assertEqual(data['rsi_recent']['status'], 'partial')
        self.assertEqual(data['rsi_recent']['windows']['20']['values']['rsi14']['valid_sessions'], 16)
        self.assertEqual(data['rsi_recent']['windows']['40']['sessions_examined'], 30)
        macd = data['macd_recent']['windows']['20']['values']
        self.assertEqual(macd['line']['valid_sessions'], 5)
        self.assertEqual(macd['signal']['valid_sessions'], 0)
        self.assertIsNone(macd['signal']['latest'])
        json.dumps(data, allow_nan=False)


class DivergenceTests(unittest.TestCase):
    def fixture(self, direction='bullish', count=22):
        # 지표 계산과 전환점 판정을 분리해 두 저점/고점의 조건을 직접 검사한다.
        frame = pd.DataFrame({'time': pd.bdate_range('2025-01-01', periods=count).strftime('%Y-%m-%d'),
                              'low': [10.0]*count, 'high': [20.0]*count})
        indicator = pd.Series([50.0]*count)
        field = 'low' if direction == 'bullish' else 'high'
        frame.loc[[4, 12], field] = [9, 8] if direction == 'bullish' else [21, 22]
        indicator.iloc[[4, 12]] = [30, 40] if direction == 'bullish' else [70, 60]
        return frame, indicator, field

    def test_regular_bullish_and_bearish(self):
        for direction in ('bullish', 'bearish'):
            with self.subTest(direction=direction):
                frame, indicator, field = self.fixture(direction)
                result = _regular_divergence(frame, indicator, direction)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['lookback_sessions'], 20)
                self.assertFalse(result['expanded_for_insufficient_pivots'])
                self.assertIs(result['detected'], True)
                self.assertEqual(result['price_field'], field)
                self.assertEqual([p['time'] for p in result['pivots']], frame.time.iloc[[4, 12]].tolist())
                self.assertEqual(result['pivots'][1]['confirmed_at'], frame.time.iloc[15])
                self.assertEqual(result['sessions_since_confirmation'], 6)

    def test_equal_or_same_direction_moves_are_not_divergence(self):
        for direction in ('bullish', 'bearish'):
            for case in ('equal_price', 'equal_indicator', 'same_direction'):
                with self.subTest(direction=direction, case=case):
                    frame, indicator, field = self.fixture(direction)
                    if case == 'equal_price':
                        frame.loc[12, field] = frame.loc[4, field]
                    else:
                        indicator.iloc[12] = indicator.iloc[4] + (0 if case == 'equal_indicator' else -10 if direction == 'bullish' else 10)
                    result = _regular_divergence(frame, indicator, direction)
                    self.assertEqual(result['status'], 'ok')
                    self.assertIs(result['detected'], False)

    def test_latest_unconfirmed_pivot_is_excluded_then_replaces_old_pair(self):
        frame, indicator, _ = self.fixture(count=24)
        frame.loc[20, 'low'], indicator.iloc[20] = 7, 20
        before = _regular_divergence(frame.iloc[:22], indicator.iloc[:22], 'bullish')
        self.assertIs(before['detected'], True)
        confirmed = _regular_divergence(frame, indicator, 'bullish')
        self.assertIs(confirmed['detected'], False)
        self.assertEqual([p['time'] for p in confirmed['pivots']], frame.time.iloc[[12, 20]].tolist())

    def test_old_pivots_plateaus_and_missing_indicator_are_unavailable(self):
        frame, indicator, _ = self.fixture(count=150)
        self.assertIsNone(_regular_divergence(frame, indicator, 'bullish')['detected'])
        frame, indicator, _ = self.fixture()
        frame.loc[13, 'low'] = frame.loc[12, 'low']
        self.assertIsNone(_regular_divergence(frame, indicator, 'bullish')['detected'])
        frame, indicator, _ = self.fixture()
        indicator.iloc[12] = float('nan')
        result = _regular_divergence(frame, indicator, 'bullish')
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['detected'])
        json.dumps(result, allow_nan=False)

    def test_expands_to_40_only_when_recent_20_lacks_two_pivots(self):
        for direction in ('bullish', 'bearish'):
            with self.subTest(direction=direction):
                # 마지막 20일에는 12번 전환점만, 40일에는 4번과 12번이 모두 있다.
                frame, indicator, _ = self.fixture(direction, count=30)
                result = _regular_divergence(frame, indicator, direction)
                self.assertIs(result['detected'], True)
                self.assertEqual(result['lookback_sessions'], 40)
                self.assertEqual(result['sessions_examined'], 30)
                self.assertTrue(result['expanded_for_insufficient_pivots'])

    def test_negative_or_missing_indicator_does_not_expand_to_seek_old_signal(self):
        frame, indicator, _ = self.fixture(count=24)
        frame.loc[20, 'low'], indicator.iloc[20] = 7, 20
        for reading in (20, float('nan')):
            with self.subTest(reading=reading):
                indicator.iloc[20] = reading
                result = _regular_divergence(frame, indicator, 'bullish')
                self.assertEqual(result['lookback_sessions'], 20)
                self.assertFalse(result['expanded_for_insufficient_pivots'])
                self.assertEqual([p['time'] for p in result['pivots']], frame.time.iloc[[12, 20]].tolist())
                self.assertIsNot(result['detected'], True)

    def test_40_session_boundary_excludes_old_pivots_even_when_only_one_remains(self):
        for direction in ('bullish', 'bearish'):
            for first in (39, 40):
                with self.subTest(direction=direction, first=first):
                    frame, indicator, field = self.fixture(direction, count=80)
                    frame.loc[[first, 60], field] = [9, 8] if direction == 'bullish' else [21, 22]
                    indicator.iloc[[first, 60]] = [30, 40] if direction == 'bullish' else [70, 60]
                    result = _regular_divergence(frame, indicator, direction)
                    self.assertEqual(result['lookback_sessions'], 40)
                    self.assertEqual(result['window_start'], frame.time.iloc[40])
                    self.assertEqual(result['window_end'], frame.time.iloc[79])
                    self.assertEqual(result['confirmed_pivot_count'], 2 if first == 40 else 1)
                    self.assertIs(result['detected'], True if first == 40 else None)
                    for pivot in result['pivots']:
                        self.assertGreaterEqual(pivot['time'], frame.time.iloc[40])
