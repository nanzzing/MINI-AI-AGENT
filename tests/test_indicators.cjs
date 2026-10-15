// Node에는 브라우저 window가 없으므로 빈 객체를 준비해 화면용 순수 계산 함수를 불러온다.
// 상승/횡보 합성 종가로 초기 준비 기간, RSI 극값, 볼린저 계산을 확인한다.
const assert=require('node:assert/strict');
global.window={};require('../src/mini_ai_agent/static/indicators.js');
const bars=Array.from({length:80},(_,i)=>({time:String(i),close:100+i}));
const data=window.calculateIndicators(bars);
assert.equal(data.rsi.length,66);assert.equal(data.rsi.at(-1).value,100);
assert.equal(data.macd.length,55);assert.equal(data.signal.length,47);
assert.equal(data.middle[0].value,109.5);
assert(Math.abs(data.upper[0].value-(109.5+2*Math.sqrt(33.25)))<1e-9);
const flat=window.calculateIndicators(bars.map(b=>({...b,close:100})));
assert.equal(flat.rsi.at(-1).value,50);assert.equal(flat.histogram.at(-1).value,0);
assert.equal(window.calculateIndicators(bars.slice(0,5)).rsi.length,0);
console.log('Indicator warmup, RSI, MACD and Bollinger calculations passed.');
