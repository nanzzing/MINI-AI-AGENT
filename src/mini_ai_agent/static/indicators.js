// 동일한 조정 종가 이력으로 계산한다. 준비 기간에는 값을 만들지 않는다.
window.calculateIndicators = bars => {
  const result={rsi:[],macd:[],signal:[],histogram:[],upper:[],middle:[],lower:[]};
  let fast,slow,signal,gain=0,loss=0,sum=0;
  bars.forEach((bar,i)=>{
    const close=bar.close,time=bar.time;
    fast=i?fast+(close-fast)*2/13:close;
    slow=i?slow+(close-slow)*2/27:close;
    if(i>=25){const line=fast-slow;signal=i===25?line:signal+(line-signal)*2/10;
      result.macd.push({time,value:line});
      if(i>=33){result.signal.push({time,value:signal});result.histogram.push({time,value:line-signal,color:line>=signal?'#21835e88':'#d6757088'});}
    }
    if(i){const delta=close-bars[i-1].close,g=Math.max(delta,0),l=Math.max(-delta,0);
      if(i<=14){gain+=g/14;loss+=l/14;}else{gain=(gain*13+g)/14;loss=(loss*13+l)/14;}
      if(i>=14)result.rsi.push({time,value:loss?100-100/(1+gain/loss):gain?100:50});
    }
    sum+=close;if(i>=20)sum-=bars[i-20].close;
    if(i>=19){const mean=sum/20,sd=Math.sqrt(bars.slice(i-19,i+1).reduce((s,b)=>s+(b.close-mean)**2,0)/20);
      result.middle.push({time,value:mean});result.upper.push({time,value:mean+2*sd});result.lower.push({time,value:mean-2*sd});
    }
  });
  return result;
};
