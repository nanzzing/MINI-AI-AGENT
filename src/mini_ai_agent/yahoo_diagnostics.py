"""로컬/배포 환경 비교용 CLI. 서버와 별도 프로세스에서만 실행한다."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from io import StringIO
import json
import logging
import os
import platform
import re
import subprocess
import sys
from threading import Thread
from time import monotonic

import pandas as pd
import yfinance as yf

PROBES = ('history', 'info', 'yearly_income', 'yearly_balance', 'yearly_cashflow',
          'quarterly_income', 'quarterly_balance', 'quarterly_cashflow')
FIELDS = ('trailingPE', 'forwardPE', 'priceToBook', 'returnOnEquity',
          'enterpriseToEbitda', 'freeCashflow', 'marketCap', 'currency', 'financialCurrency')


def environment():
    packages = {}
    for name in ('yfinance', 'curl_cffi', 'pandas', 'requests'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return dict(python=platform.python_version(), os=platform.system(), packages=packages,
                render=bool(os.environ.get('RENDER')),
                proxy_configured=any(os.environ.get(key) for key in
                                     ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                      'http_proxy', 'https_proxy', 'all_proxy')))


def safe_signals(text):
    """임의 로그 원문 대신 허용된 코드/분류만 내보낸다."""
    statuses = sorted(set(int(code) for code in re.findall(
        r'(?:HTTP(?:\s+Error)?\s*[:=]?\s*|response code\s*=\s*)([45]\d\d)\b', text, re.I)))
    labels = {'invalid crumb': 'invalid_crumb', 'invalid cookie': 'invalid_cookie',
              'unable to access this feature': 'feature_access_denied',
              'too many requests': 'rate_limited', 'timed out': 'timeout',
              'could not resolve': 'dns_failure', 'certificate verify failed': 'tls_verification'}
    return dict(http_statuses=statuses,
                signals=[label for phrase, label in labels.items() if phrase in text.lower()])


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def probe(symbol, operation):
    started = monotonic()
    captured = Capture()
    logger = logging.getLogger('yfinance')
    original = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    logger.handlers, logger.propagate, logger.disabled = [captured], False, False
    logger.setLevel(logging.WARNING)
    stream = StringIO()
    result = dict(event='yahoo_diagnostic', symbol=symbol, operation=operation,
                  checked_at=datetime.now(timezone.utc).isoformat(), status='error')
    try:
        # 쿠키/crumb/URL이나 예외 본문은 진단 출력에 포함하지 않는다.
        with redirect_stdout(stream), redirect_stderr(stream):
            try:
                ticker = yf.Ticker(symbol)
                if operation == 'info':
                    data = ticker.get_info()
                    present = [field for field in FIELDS if isinstance(data, dict) and data.get(field) is not None]
                    result.update(field_count=len(data) if isinstance(data, dict) else 0,
                                  present_fields=present, missing_fields=[field for field in FIELDS if field not in present],
                                  status='ok' if len(present) == len(FIELDS) else 'partial' if present else 'empty')
                else:
                    if operation == 'history':
                        data = ticker.history(period='1mo', auto_adjust=True, timeout=15, raise_errors=True)
                    else:
                        frequency, statement = operation.split('_')
                        method = {'income': ticker.get_income_stmt, 'balance': ticker.get_balance_sheet,
                                  'cashflow': ticker.get_cashflow}[statement]
                        data = method(freq=frequency, pretty=True)
                    if not isinstance(data, pd.DataFrame):
                        raise TypeError('unexpected table')
                    cells = int(data.notna().sum().sum())
                    result.update(rows=len(data.index), columns=len(data.columns), non_null_cells=cells,
                                  status='ok' if cells else 'empty')
            except Exception as error:
                result['error_type'] = type(error).__name__
                # 문자열은 메모리에서 코드 분류에만 사용하고 출력하지 않는다.
                captured.messages.append(str(error))
    finally:
        logger.handlers, logger.level, logger.propagate, logger.disabled = original
    result.update(safe_signals('\n'.join(captured.messages) + stream.getvalue()))
    result['elapsed_ms'] = round((monotonic() - started) * 1000)
    return result


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def run_report(symbol='005930.KS', operation=None):
    emit(dict(event='yahoo_diagnostic_environment', **environment(),
              isolation='fresh process per probe; application cache bypassed'))
    # 각 기능을 독립 실행해 하나의 요청이 멈춰도 다음 진단을 진행한다.
    # 파일에 저장된 yfinance 쿠키는 삭제하거나 외부로 복사하지 않는다.
    for operation in ([operation] if operation else PROBES):
        try:
            child = subprocess.run([sys.executable, '-m', __spec__.name, '--worker',
                                    '--symbol', symbol, '--probe', operation],
                                   capture_output=True, text=True, timeout=45)
            # 라이브러리의 원본 stderr는 전달하지 않는다.
            result = json.loads(child.stdout) if child.returncode == 0 else None
            if not isinstance(result, dict):
                raise ValueError('worker failed')
        except subprocess.TimeoutExpired:
            result = dict(event='yahoo_diagnostic', symbol=symbol, operation=operation,
                          status='timeout', timeout_seconds=45)
        except (ValueError, OSError):
            result = dict(event='yahoo_diagnostic', symbol=symbol, operation=operation,
                          status='worker_error')
        emit(result)
    emit(dict(event='yahoo_diagnostic_complete', symbol=symbol))


def start_startup_diagnostics():
    """명시적으로 켰을 때만 시작 시 1회 실행. 평소에는 추가 조회를 하지 않는다."""
    enabled = os.environ.get('YAHOO_DIAGNOSTICS_ON_START', '0')
    if enabled.strip().lower() not in ('1', 'true', 'yes'):
        return None

    def background():
        try:
            # 서버 스레드에서 probe()를 직접 부르면 로그/표준출력 설정이 섞인다.
            # run_report는 각 probe를 별도 프로세스에서 실행하므로 서버와 격리된다.
            emit(dict(event='yahoo_diagnostic_start', symbol='005930.KS', probes=len(PROBES)))
            run_report()
        except Exception as error:
            emit(dict(event='yahoo_diagnostic_startup_error', error_type=type(error).__name__))

    thread = Thread(target=background, name='yahoo-startup-diagnostics', daemon=True)
    try:
        thread.start()
    except Exception as error:
        emit(dict(event='yahoo_diagnostic_startup_error', error_type=type(error).__name__))
        return None
    return thread


def main():
    parser = argparse.ArgumentParser(description='Yahoo read-only diagnostics; no LLM calls or credentials in output')
    parser.add_argument('--symbol', default='005930.KS')
    parser.add_argument('--probe', choices=PROBES)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9^][A-Za-z0-9.^=-]{0,29}', args.symbol):
        parser.error('Use a Yahoo ticker, for example 005930.KS or AAPL')
    if args.worker:
        if not args.probe:
            parser.error('--worker requires --probe')
        emit(probe(args.symbol, args.probe))
        return
    run_report(args.symbol, args.probe)


if __name__ == '__main__':
    main()
