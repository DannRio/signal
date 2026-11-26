const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs/promises');

function chartFixtures() {
 const start=Date.UTC(2026,10,20);
 const candles=Array.from({length:240},(_,i)=>{
  const center=100000+i*9+Math.sin(i/11)*260;
  const open=center+(i%2?35:-35),close=center+(i%2?-35:35);
  return {timestamp:new Date(start+i*900000).toISOString(),open,close,high:Math.max(open,close)+55,low:Math.min(open,close)-55,volume:100+i,ema_fast:center-40,ema_mid:center-110,ema_slow:center-260};
 });
 const signal={symbol:'BTC/USDT:USDT',action:'watch_long',regime:'bullish',score:72,confidence:79,consensus:1,generated_at:new Date(start+240*900000).toISOString(),entry_plan:null,reasons:['Deterministic chart regression fixture.'],warnings:[],timeframe_states:[]};
 const result={trades:20,win_rate_percent:55,profit_factor:1.2,expectancy_r:.12,total_return_percent:3.5,net_pnl:350,max_drawdown_percent:2,starting_equity:10000,ending_equity:10350,trades_detail:[],equity_curve:Array.from({length:80},(_,i)=>[new Date(start+i*900000).toISOString(),10000+i*4.4+Math.sin(i/5)*45])};
 return {snapshot:{signal,candles,exchange_id:'kucoinfutures',market_type:'swap'},backtest:{result,diagnostics:{trade_quality:{},yearly:{}}}};
}

async function assertChart(page,id,expectedHeight,kind) {
 // Wait for a real layout/redraw, including ResizeObserver/requestAnimationFrame work.
 await page.waitForFunction(({id,expectedHeight})=>{
  const canvas=document.getElementById(id),rect=canvas.getBoundingClientRect(),ratio=devicePixelRatio;
  return rect.width>100&&Math.abs(rect.height-expectedHeight)<1&&Math.abs(canvas.width-rect.width*ratio)<=1&&Math.abs(canvas.height-expectedHeight*ratio)<=1;
 },{id,expectedHeight},{timeout:5000});
 const metrics=await page.locator(`#${id}`).evaluate((canvas,kind)=>{
  const rect=canvas.getBoundingClientRect(),pixels=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity,count=0;
  for(let y=0;y<canvas.height;y++) for(let x=0;x<canvas.width;x++){
   const index=(y*canvas.width+x)*4,r=pixels[index],g=pixels[index+1],b=pixels[index+2];
   const colored=kind==='candles'?((r<100&&g>160&&b>100&&b<190)||(r>220&&g<145&&b>90&&b<170)):(r>60&&r<100&&g>145&&g<185&&b>230);
   if(colored){minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);count++;}
  }
  const scaleX=rect.width/canvas.width,scaleY=rect.height/canvas.height;
  return {width:rect.width,height:rect.height,bitmapWidth:canvas.width,bitmapHeight:canvas.height,ratio:devicePixelRatio,count,minX:minX*scaleX,maxX:maxX*scaleX,minY:minY*scaleY,maxY:maxY*scaleY};
 },kind);
 assert.ok(metrics.count>100,`${id} must contain a rendered ${kind} series: ${JSON.stringify(metrics)}`);
 assert.ok(metrics.minX<35,`${id} must start near the left plot edge: ${JSON.stringify(metrics)}`);
 assert.ok(metrics.maxX>metrics.width-95,`${id} must span the visible plot width: ${JSON.stringify(metrics)}`);
 assert.ok(metrics.minY>5&&metrics.maxY<expectedHeight-10,`${id} series must fit vertically: ${JSON.stringify(metrics)}`);
 assert.ok(metrics.maxY-metrics.minY>expectedHeight*.45,`${id} series must use its vertical range: ${JSON.stringify(metrics)}`);
 return metrics;
}

async function checkChartFlow(browser,artifactDir,deviceScaleFactor) {
 const context=await browser.newContext({viewport:{width:1440,height:1100},deviceScaleFactor,timezoneId:'America/Halifax'});
 const page=await context.newPage(),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 const fixtures=chartFixtures();
 let snapshotRequests=0,backtestRequests=0,pendingLiveGate=null;
 await page.route('**/v1/market-snapshot',async route=>{snapshotRequests++;if(pendingLiveGate)await pendingLiveGate();await route.fulfill({json:fixtures.snapshot});});
 await page.route('**/v1/backtest/live',async route=>{backtestRequests++;await route.fulfill({json:fixtures.backtest});});
 await page.goto('http://127.0.0.1:8861/');
 // Exercise the actual Analyze button: drawing while its results are display:none was a root cause.
 for(let repeat=0;repeat<3;repeat++){
  await page.click('#analyzeButton');
  await page.waitForSelector('#liveResults:not(.hidden)');
  await assertChart(page,'priceChart',430,'candles');
  assert.equal(await page.locator('#liveError').isVisible(),false);
 }
 for(const width of [1100,1440,390]){
  await page.setViewportSize({width,height:1100});
  await assertChart(page,'priceChart',430,'candles');
 }
 await page.locator('#priceChart').screenshot({path:path.join(artifactDir,`price-chart-dpr-${deviceScaleFactor}-mobile.png`)});
 await page.setViewportSize({width:1440,height:1100});
 await assertChart(page,'priceChart',430,'candles');
 await page.locator('#priceChart').screenshot({path:path.join(artifactDir,`price-chart-dpr-${deviceScaleFactor}-desktop.png`)});
 await page.click('[data-tab="backtest"]');
 for(let repeat=0;repeat<3;repeat++){
  await page.click('#backtestButton');
  await page.waitForSelector('#btResults:not(.hidden)');
  await assertChart(page,'equityChart',330,'equity');
  assert.equal(await page.locator('#btError').isVisible(),false);
 }
 // A hidden chart must neither collapse nor accumulate DPR scaling as other tabs resize.
 await page.setViewportSize({width:1000,height:1100});
 await assertChart(page,'equityChart',330,'equity');
 await page.click('[data-tab="live"]');
 await assertChart(page,'priceChart',430,'candles');
 await page.setViewportSize({width:390,height:844});
 await assertChart(page,'priceChart',430,'candles');
 await page.click('[data-tab="backtest"]');
 await assertChart(page,'equityChart',330,'equity');
 await page.locator('#equityChart').screenshot({path:path.join(artifactDir,`equity-chart-dpr-${deviceScaleFactor}-mobile.png`)});
 // A response may finish after its tab was hidden; returning must draw at the current size.
 let announceRequest,releaseResponse;
 const requestStarted=new Promise(resolve=>{announceRequest=resolve;});
 pendingLiveGate=async()=>{announceRequest();await new Promise(resolve=>{releaseResponse=resolve;});};
 await page.click('[data-tab="live"]');
 await page.click('#analyzeButton');
 await requestStarted;
 await page.click('[data-tab="scanner"]');
 await page.setViewportSize({width:1200,height:1100});
 const responseFinished=page.waitForResponse(response=>response.url().endsWith('/v1/market-snapshot'));
 releaseResponse();
 await responseFinished;
 await page.waitForSelector('#liveLoading.hidden',{state:'attached'});
 await page.click('[data-tab="live"]');
 await assertChart(page,'priceChart',430,'candles');
 assert.equal(snapshotRequests,4,'Redrawing a resized/reopened chart must reuse the last response.');
 assert.equal(backtestRequests,3,'Redrawing equity must reuse the last response.');
 assert.deepEqual(errors,[]);
 await context.close();
 return {deviceScaleFactor,repeatedLive:'passed',repeatedBacktest:'passed',resize:'passed',tabReturn:'passed',hiddenResponse:'passed',pixelCoverage:'passed'};
}

(async()=>{
 const artifactDir=process.env.SIGNAL_UI_ARTIFACT_DIR || path.join(process.cwd(),'artifacts','ui');
 await fs.mkdir(artifactDir,{recursive:true});
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1100},timezoneId:'America/Halifax'});
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 const now=Date.now();
 const candidate={symbol:'BTC/USDT:USDT',base:'BTC',quote:'USDT',quote_volume:125000000,spread_bps:1.2,bid:99999,ask:100001,last_price:100000,quote_timestamp:new Date(now).toISOString(),metadata_observed_at:new Date(now).toISOString(),volume_source:'contracts/active.turnoverOf24h',funding_rate:0.0001,funding_interval_hours:8,next_funding_at:new Date(now+3600000).toISOString(),mark_price:100000,index_price:100003};
 const signal={symbol:candidate.symbol,action:'long',regime:'bullish',confidence:83.1,consensus:1,score:62,generated_at:new Date(now-120000).toISOString()};
 const contract_plan={contracts:47,base_quantity:.047,notional_value:4700,actual_risk_amount:47.1,entry_low:99980,entry_high:100020,stop_loss:99000,take_profit_1:101000,take_profit_2:102000,take_profit_3:103000,net_reward_to_risk_1:.76,net_reward_to_risk_2:1.63,net_reward_to_risk_3:2.51};
 const good={rank:1,candidate,signal,status:'analyzed',actionable:true,blocked_reasons:[],valid_until:new Date(now+120000).toISOString(),contract_plan,data_quality:{'15m':{rows:500,last_close:signal.generated_at,sha256:'a'.repeat(64)}}};
 const blocked={...good,rank:2,candidate:{...candidate,symbol:'ETH/USDT:USDT',base:'ETH'},actionable:false,status:'blocked',error_code:'outside_entry_zone',blocked_reasons:['Current executable quote is outside the planned entry zone.']};
 const failed={rank:3,candidate:{...candidate,symbol:'SOL/USDT:USDT'},signal:null,status:'failed',error_code:'candle_gap',error:'Missing one 15m candle.',actionable:false};
 const report={status:'degraded',generated_at:new Date(now).toISOString(),started_at:new Date(now-5000).toISOString(),duration_seconds:5,considered_markets:4,excluded_by_limit:0,analyzed_markets:2,actionable_markets:1,blocked_markets:1,failed_markets:1,rejected_markets:1,run_id:'test-ui-fixture',items:[good,blocked,failed],errors:[],rejections:[{symbol:'TEST/USDT:USDT',code:'low_volume',reason:'Below minimum turnover <img src=x onerror=alert(1)>'}]};
 await page.route('**/v1/scan',async route=>{const body=route.request().postDataJSON();assert.equal(body.maximum_quote_age_seconds,30);assert.equal(body.scan_timeout_seconds,180);await route.fulfill({json:report});});
 for(let attempt=0;attempt<40;attempt++){try{await page.goto('http://127.0.0.1:8861/');break;}catch(error){if(attempt===39)throw error;await new Promise(resolve=>setTimeout(resolve,250));}}
 await page.click('[data-tab="scanner"]');await page.click('#scanButton');
 await page.waitForSelector('#scanResults:not(.hidden)');
 assert.match(await page.locator('#scanSummary').textContent(),/1 qualified now/);
 assert.match(await page.locator('#scanRows').textContent(),/QUALIFIED LONG/);
 assert.match(await page.locator('#scanRows').textContent(),/BLOCKED/);
 assert.match(await page.locator('#scanRows').textContent(),/FAILED \[candle_gap\]/);
 assert.equal(await page.locator('#scanRejections img').count(),0);
 await page.locator('#scanRows summary').first().click();
 assert.match(await page.locator('#scanRows .scan-detail').first().innerText(),/47 contracts/);
 const desktopTable=await page.locator('#scanResults .table-wrap').first().evaluate(el=>({width:el.clientWidth,scroll:el.scrollWidth}));
 assert.ok(desktopTable.scroll<=desktopTable.width+1,JSON.stringify(desktopTable));
 await page.screenshot({path:path.join(artifactDir,'scanner-desktop.png'),fullPage:true});
 await page.evaluate(()=>{state.lastScanReport.items[0].valid_until=new Date(Date.now()-1).toISOString();updateScanExpiry();});
 assert.match(await page.locator('#scanRows').textContent(),/EXPIRED/);
 assert.match(await page.locator('#scanSummary').textContent(),/0 qualified now/);
 assert.equal(await page.locator('#scanButton').isDisabled(),false);
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:path.join(artifactDir,'scanner-mobile.png'),fullPage:true});
 const size=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
 assert.ok(size.scroll<=size.width+1,JSON.stringify(size));
 assert.deepEqual(errors,[]);
 const charts=[];
 for(const ratio of [1,1.25,2]) charts.push(await checkChartFlow(browser,artifactDir,ratio));
 console.log(JSON.stringify({desktop:'passed',mobile:'passed',expiry:'passed',escaping:'passed',pageErrors:errors,charts}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
