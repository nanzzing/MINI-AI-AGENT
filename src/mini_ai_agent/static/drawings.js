// 선은 날짜와 가격 좌표로 현재 페이지 메모리에만 보관한다.
window.createDrawingTools = (chart, series, root) => {
  const section=root.closest('.chart-section'),toolbar=document.createElement('div');
  toolbar.className='drawing-toolbar';toolbar.setAttribute('role','toolbar');toolbar.setAttribute('aria-label','차트 그리기 도구');
  root.before(toolbar);
  chart.timeScale().applyOptions({lockVisibleTimeRangeOnResize:true});
  const ns='http://www.w3.org/2000/svg',overlay=document.createElementNS(ns,'svg');
  overlay.classList.add('drawing-overlay');root.append(overlay);
  let mode='pointer',lines=[],history=[],pending=null,key='',color='#277bd0',expanded=false,focusBefore;
  const buttons={};
  const drawings=new Map();
  // 이전 버전의 선 데이터만 제거한다. 다른 브라우저 설정은 건드리지 않는다.
  try{for(const name of Object.keys(localStorage))if(name.startsWith('mini-drawings:'))localStorage.removeItem(name);}catch{}
  function button(id,icon,title,action){const b=document.createElement('button');b.type='button';b.title=title;b.setAttribute('aria-label',title);b.innerHTML=`<i data-lucide="${icon}"></i>`;b.addEventListener('click',action);toolbar.append(b);buttons[id]=b;return b;}
  function select(next){mode=next;pending=null;overlay.style.pointerEvents=mode==='pointer'?'none':'auto';overlay.style.cursor=mode==='erase'?'pointer':'crosshair';for(const id of ['pointer','trend','horizontal','erase'])buttons[id].setAttribute('aria-pressed',String(id===mode));render();}
  button('expand','maximize','차트 크게 보기',()=>{
    expanded=!expanded;section.classList.toggle('chart-expanded',expanded);document.body.classList.toggle('chart-is-expanded',expanded);
    if(!expanded)select('pointer');
    buttons.expand.setAttribute('aria-pressed',String(expanded));buttons.expand.title=expanded?'차트 축소 (Esc)':'차트 크게 보기';buttons.expand.setAttribute('aria-label',buttons.expand.title);
    buttons.expand.innerHTML=`<i data-lucide="${expanded?'minimize':'maximize'}"></i>`;window.lucide?.createIcons();
    if(expanded){focusBefore=document.activeElement;section.setAttribute('role','dialog');section.setAttribute('aria-modal','true');section.setAttribute('aria-label','확대 차트');}else{section.removeAttribute('role');section.removeAttribute('aria-modal');focusBefore?.focus();}
    requestAnimationFrame(render);
  });
  button('pointer','mouse-pointer-2','차트 이동',()=>select('pointer'));
  button('trend','waypoints','추세선: 시작점과 끝점 선택',()=>select('trend'));
  button('horizontal','minus','수평선: 가격 위치 선택',()=>select('horizontal'));
  button('erase','eraser','선 선택 삭제',()=>select('erase'));
  function undo(){if(pending){pending=null;render();}else if(history.length){lines=history.pop();save();render();}}
  button('undo','undo-2','그리기 되돌리기',undo);
  buttons.undo.title='그리기 되돌리기 (Ctrl+Z)';
  document.addEventListener('keydown',event=>{
    const target=event.target;
    if(!expanded||event.isComposing||event.repeat||event.shiftKey||event.altKey||!(event.ctrlKey||event.metaKey)||event.key.toLowerCase()!=='z')return;
    if(target instanceof HTMLElement&&(target.closest('input,textarea,select')||target.isContentEditable))return;
    event.preventDefault();undo();
  });
  button('clear','trash-2','모든 선 삭제',()=>{if(lines.length&&window.confirm('이 종목의 현재 봉 주기에 그린 선을 모두 삭제할까요?')){remember();lines=[];pending=null;save();render();}});
  const picker=document.createElement('input');picker.type='color';picker.value=color;picker.title='선 색상';picker.setAttribute('aria-label','선 색상');picker.addEventListener('input',()=>{color=picker.value;});toolbar.append(picker);
  function remember(){history.push(JSON.parse(JSON.stringify(lines)));if(history.length>50)history.shift();}
  function save(){if(key)drawings.set(key,lines);}
  function position(event){const rect=overlay.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
    const scale=chart.timeScale(),origin=scale.logicalToCoordinate(0),next=scale.logicalToCoordinate(1),price=series.coordinateToPrice(y),data=series.data();
    if(origin===null||next===null||next===origin||!Number.isFinite(price)||!data.length)return null;
    const logical=(x-origin)/(next-origin);
    // 빈 미래 영역도 가장 가까운 실제 봉에서의 거리로 표현한다. 날짜를 임의로 만들지 않는다.
    const index=Math.max(0,Math.min(data.length-1,Math.round(logical)));
    return {time:data[index].time,offset:logical-index,price};}
  function xCoordinate(point){
    const scale=chart.timeScale(),anchor=scale.timeToCoordinate(point.time);
    if(anchor===null)return null;
    const origin=scale.logicalToCoordinate(0),next=scale.logicalToCoordinate(1);
    return origin===null||next===null?null:anchor+(point.offset||0)*(next-origin);
  }
  function segment(item,preview=false){const x1=xCoordinate(item.a),y1=series.priceToCoordinate(item.a.price);
    const x2=item.b?xCoordinate(item.b):null,y2=item.b?series.priceToCoordinate(item.b.price):y1;
    if(y1===null||(item.kind==='trend'&&(x1===null||x2===null||y2===null)))return;
    const line=document.createElementNS(ns,'line');line.setAttribute('x1',item.kind==='horizontal'?0:x1);line.setAttribute('x2',item.kind==='horizontal'?chart.timeScale().width():x2);line.setAttribute('y1',y1);line.setAttribute('y2',y2);line.setAttribute('stroke',item.color);line.setAttribute('stroke-width',2);if(preview)line.setAttribute('stroke-dasharray','5 4');overlay.append(line);
    if(mode==='erase'&&!preview){const hit=line.cloneNode();hit.setAttribute('stroke','transparent');hit.setAttribute('stroke-width',16);hit.style.pointerEvents='stroke';hit.addEventListener('pointerdown',event=>{event.stopPropagation();remember();lines=lines.filter(l=>l!==item);save();render();});overlay.append(hit);}
  }
  function render(){overlay.setAttribute('width',chart.timeScale().width());overlay.setAttribute('height',Math.max(0,root.clientHeight-chart.timeScale().height()));overlay.replaceChildren();for(const line of lines)segment(line);if(pending)segment(pending,true);buttons.undo.disabled=!history.length;buttons.clear.disabled=!lines.length;}
  overlay.addEventListener('pointerdown',event=>{if(mode==='erase'||mode==='pointer'||!key)return;event.preventDefault();const p=position(event);if(!p)return;
    if(mode==='horizontal'){remember();lines.push({kind:'horizontal',a:p,color});save();}
    else if(!pending){pending={kind:'trend',a:p,b:p,color};}else{remember();lines.push({...pending,b:p});pending=null;save();}render();});
  overlay.addEventListener('pointermove',event=>{if(pending){const p=position(event);if(p){pending.b=p;render();}}});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'){if(pending||mode!=='pointer')select('pointer');else if(expanded)buttons.expand.click();}if(expanded&&event.key==='Tab'){const nodes=[...section.querySelectorAll('button,input,select,a[href]')].filter(n=>!n.disabled&&n.getClientRects().length);if(event.shiftKey&&document.activeElement===nodes[0]){event.preventDefault();nodes.at(-1).focus();}else if(!event.shiftKey&&document.activeElement===nodes.at(-1)){event.preventDefault();nodes[0].focus();}}});
  chart.timeScale().subscribeVisibleLogicalRangeChange(render);chart.subscribeCrosshairMove(render);
  root.addEventListener('pointermove',()=>requestAnimationFrame(render));root.addEventListener('wheel',()=>requestAnimationFrame(render));new ResizeObserver(()=>requestAnimationFrame(render)).observe(root);
  select('pointer');window.lucide?.createIcons();
  return {context(identity){if(key===identity)return;key=identity;pending=null;history=[];lines=drawings.get(key)||[];render();},render};
};
