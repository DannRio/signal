const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs/promises');

async function fullPageScreenshot(page,filePath) {
 // Full-page captures should show sticky navigation at its document origin,
 // not composited over the currently scrolled section of the screenshot.
 await page.evaluate(()=>window.scrollTo(0,0));
 await page.waitForFunction(()=>window.scrollY===0);
 await page.screenshot({path:filePath,fullPage:true});
}

async function assertScrolledNavigation(page) {
 await page.evaluate(()=>window.scrollTo(0,document.documentElement.scrollHeight));
 await page.waitForFunction(()=>window.scrollY>100);
 const position=await page.locator('[data-tab="scanner"]').evaluate(button=>{
  const rect=button.getBoundingClientRect(),x=rect.left+rect.width/2,y=rect.top+rect.height/2;
  const hit=document.elementFromPoint(x,y);
  return {top:rect.top,bottom:rect.bottom,viewportHeight:innerHeight,hitTab:hit?.closest('[data-tab]')?.dataset.tab,scrollY};
 });
 assert.ok(position.top>=0&&position.bottom<=position.viewportHeight,JSON.stringify(position));
 assert.equal(position.hitTab,'scanner',`Sticky navigation must not sit underneath the header: ${JSON.stringify(position)}`);
 await page.click('[data-tab="live"]');
 assert.equal(await page.locator('#tab-live').evaluate(el=>el.classList.contains('active')),true);
 await page.click('[data-tab="scanner"]');
 assert.equal(await page.locator('#tab-scanner').evaluate(el=>el.classList.contains('active')),true);
}

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

async function checkPaperAndResearchFlow(browser,artifactDir) {
 const context=await browser.newContext({viewport:{width:1440,height:1100},timezoneId:'America/Halifax'});
 const page=await context.newPage(),errors=[],dialogs=[];
 page.on('pageerror',error=>errors.push(error.message));
 page.on('dialog',async dialog=>{dialogs.push(dialog.message());await dialog.dismiss();});
 const stamp='2026-11-26T00:00:00+00:00',account='paper <img src=x onerror=alert(1)>',untrusted='quote_gap <img src=x onerror=alert(1)>';
 const summary={status:'observed',account_id:account,assumed_equity:5000,open_positions:1,unresolved_positions:1,closed_positions:1,closed_pnl_before_funding:9.7,overdue_observations:1,funding_complete:false};
 const positions=[
  {symbol:'BTC/USDT:USDT',side:'long',status:'open',entry_price:100,exit_price:null,contracts:10,pnl_before_funding:null},
  {symbol:'ETH/USDT:USDT',side:'short',status:'closed',entry_price:205,exit_price:200,contracts:20,pnl_before_funding:9.7,exit_reason:'observed_target'},
  {symbol:'SOL/USDT:USDT',side:'long',status:'unresolved',entry_price:50,exit_price:null,contracts:5,pnl_before_funding:null,unresolved_reason:untrusted},
 ];
 const events=[
  {observed_at:stamp,symbol:'BTC/USDT:USDT',decision:'opened',reason:'qualified_snapshot'},
  {observed_at:stamp,symbol:'SOL/USDT:USDT',decision:'skipped',reason:untrusted},
  {observed_at:stamp,symbol:'XRP/USDT:USDT',decision:'expired',reason:'signal_expired'},
 ];
 const requestedAccounts=[];
 await page.route('**/v1/paper/**',async route=>{
  const url=new URL(route.request().url()),selected=url.searchParams.get('account_id');
  requestedAccounts.push(selected);
  assert.ok([account,'unstarted','default'].includes(selected),'Account IDs must survive URL encoding.');
  const empty=selected!==account;
  const response=url.pathname.endsWith('/status')?(empty?{status:'not_started',account_id:selected}:summary):{items:empty?[]:url.pathname.endsWith('/positions')?positions:events};
  await route.fulfill({json:response});
 });
 for(const endpoint of ['journal/signals','journal/backtests','datasets']) await page.route(`**/v1/${endpoint}?*`,route=>route.fulfill({json:{items:[]}}));
 const replayId='b'.repeat(64);
 await page.route('**/v1/scanner/replays?*',route=>route.fulfill({json:{items:[{snapshot_id:replayId,symbol:`BTC/USDT:USDT ${untrusted}`,action:'long',captured_at:stamp}]}}));
 await page.route(`**/v1/scanner/replays/${replayId}/verify`,route=>route.fulfill({json:{passed:true,differences:[]}}));
 await page.route('**/v1/scanner/metrics?*',route=>route.fulfill({json:{sampled_runs:7,groups:[
  {workload:{symbols:['BTC/USDT:USDT','ETH/USDT:USDT']},cache_state:'disabled',runs:2,duration_seconds:{p50:5,p95:6.8},coverage_percent:100},
  {workload:{symbols:['BTC/USDT:USDT','ETH/USDT:USDT']},cache_state:'warm',runs:5,duration_seconds:{p50:.45,p95:.7},coverage_percent:100},
 ]}}));
 const evidence={schema_version:1,contract_spec:{symbol:'BTC/USDT:USDT',contract_size:'0.001',lot_size:'1',tick_size:'0.1'},contract_spec_source:'Deterministic browser fixture',contract_spec_assumption:'fixed_over_backtest_window',funding:{coverage_start:'2026-11-20T00:00:00+00:00',coverage_end:'2026-11-21T00:00:00+00:00',expected_settlement_timestamps:['2026-11-20T08:00:00+00:00'],events:[{timestamp:'2026-11-20T08:00:00+00:00',rate:'0.0001',mark_price:'100000',source:'Historical settlement fixture'}],source:'Historical funding fixture',schedule_source:'Historical schedule fixture'}};
 const baseline=chartFixtures().backtest;
 const funded={...baseline,result:{...baseline.result,funding_pnl:-12.5,simulation_metadata:{funding_modeled:true,contract_rounding_modeled:true,margin_and_liquidation_modeled:false}}};
 let backtestRequests=0,validationRequests=0;
 await page.route('**/v1/backtest/live',async route=>{
  backtestRequests++;
  const body=route.request().postDataJSON();
  if(body.futures_inputs) assert.deepEqual(body.futures_inputs,evidence);
  await route.fulfill({json:body.futures_inputs?funded:baseline});
 });
 await page.route('**/v1/research/validate',async route=>{
  validationRequests++;
  assert.deepEqual(route.request().postDataJSON().futures_inputs,evidence);
  await route.fulfill({json:{validation:{base:{...funded.diagnostics,summary:funded.result},gate:{status:'FAIL',checks:[{name:'sufficient_out_of_sample_trades',passed:false,available:false,requirement:'Need enough independent trades.',value:0}],limitations:['Funding evidence does not establish a trading edge.']},monte_carlo:{probability_ending_below_start:null}}}});
 });
 await page.goto('http://127.0.0.1:8861/');
 await page.click('[data-tab="paper"]');
 await page.waitForFunction(()=>document.getElementById('paperStatus').textContent.includes('has not started'));
 await page.locator('#paperAccount').fill(account);
 await page.click('#refreshPaperButton');
 await page.waitForFunction(()=>document.querySelectorAll('#paperRows tr').length===3);
 assert.match(await page.locator('#paperStatus').textContent(),/assumed starting equity 5,000/);
 assert.ok((await page.locator('#paperStatus').textContent()).includes(account));
 assert.match(await page.locator('#paperMetrics').textContent(),/P&L before funding/);
 assert.match(await page.locator('#paperMetrics').textContent(),/9\.7/);
 const unresolved=page.locator('#paperRows tr').filter({hasText:'SOL/USDT:USDT'});
 assert.equal(await unresolved.locator('td').nth(2).textContent(),'unresolved');
 assert.equal(await unresolved.locator('td').nth(4).textContent(),'—');
 assert.equal(await unresolved.locator('td').nth(6).textContent(),'—');
 assert.ok((await unresolved.textContent()).includes(untrusted));
 assert.equal(await page.locator('#paperRows img,#paperEvents img,#paperStatus img').count(),0);
 assert.match(await page.locator('#paperEvents').textContent(),/expired/);
 await fullPageScreenshot(page,path.join(artifactDir,'paper-desktop.png'));
 await page.setViewportSize({width:390,height:844});
 const mobile=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
 assert.ok(mobile.scroll<=mobile.width+1,JSON.stringify(mobile));
 await fullPageScreenshot(page,path.join(artifactDir,'paper-mobile.png'));
 await page.locator('#paperAccount').fill('unstarted');
 await page.click('#refreshPaperButton');
 await page.waitForFunction(()=>document.getElementById('paperStatus').textContent.includes('has not started'));
 assert.match(await page.locator('#paperRows').textContent(),/No paper positions/);
 assert.match(await page.locator('#paperEvents').textContent(),/No admission events/);
 assert.equal(requestedAccounts.filter(value=>value===account).length,3);
 assert.equal(requestedAccounts.filter(value=>value==='unstarted').length,3);
 await page.setViewportSize({width:1440,height:1100});
 await page.click('[data-tab="journal"]');
 await page.waitForSelector('#replayJournal [data-replay]');
 await page.waitForFunction(()=>document.querySelectorAll('#workloadMetrics .journal-item').length===2);
 assert.match(await page.locator('#workloadMetrics').textContent(),/2 markets · disabled/);
 assert.match(await page.locator('#workloadMetrics').textContent(),/2 markets · warm/);
 assert.match(await page.locator('#workloadMetrics').textContent(),/95th percentile 0\.7s/);
 assert.equal(await page.locator('#replayJournal img').count(),0);
 await page.locator('#replayJournal [data-replay]').click();
 await page.waitForFunction(()=>document.querySelector('#replayJournal .replay-result').textContent.includes('Replay matched'));
 await fullPageScreenshot(page,path.join(artifactDir,'journal-replay-metrics.png'));
 await page.click('[data-tab="backtest"]');
 await page.locator('#futuresEvidenceFile').setInputFiles({name:'futures-evidence.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(evidence))});
 await page.click('#backtestButton');
 await page.waitForFunction(()=>document.getElementById('futuresSimulation').textContent.includes('Funding P&L:'));
 assert.match(await page.locator('#futuresSimulation').textContent(),/-12\.5 USDT/);
 assert.match(await page.locator('#futuresSimulation').textContent(),/Margin and liquidation are not modeled/);
 await page.click('#validateButton');
 await page.waitForFunction(()=>document.getElementById('validationGate').textContent.includes('Funding evidence does not establish'));
 assert.match(await page.locator('#validationGate').textContent(),/Missing/);
 assert.match(await page.locator('#futuresSimulation').textContent(),/Funding P&L:/);
 await assertChart(page,'equityChart',330,'equity');
 await fullPageScreenshot(page,path.join(artifactDir,'futures-evidence-validation.png'));
 await page.locator('#futuresEvidenceFile').setInputFiles([]);
 await page.click('#backtestButton');
 await page.waitForFunction(()=>document.getElementById('futuresSimulation').textContent.includes('Historical funding is not included'));
 assert.equal(backtestRequests,2);
 assert.equal(validationRequests,1);
 await page.locator('#futuresEvidenceFile').setInputFiles({name:'malformed-evidence.json',mimeType:'application/json',buffer:Buffer.from('{ invalid')});
 await page.click('#backtestButton');
 await page.waitForSelector('#btError:not(.hidden)');
 assert.equal(backtestRequests,2,'Malformed local JSON must not start a historical download.');
 assert.deepEqual(errors,[]);
 assert.deepEqual(dialogs,[]);
 await context.close();
 return {paperOutcomes:'passed',paperUnknownPnl:'passed',escapedEvidence:'passed',emptyAccount:'passed',groupedMetrics:'passed',journalReplay:'passed',fundingUpload:'passed',validationUpload:'passed',malformedEvidence:'passed'};
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
 const replayId='a'.repeat(64);
 const good={rank:1,candidate,signal,status:'analyzed',actionable:true,blocked_reasons:[],valid_until:new Date(now+120000).toISOString(),contract_plan,replay_id:replayId,data_quality:{'15m':{rows:500,last_close:signal.generated_at,sha256:'a'.repeat(64)}}};
 const blocked={...good,rank:2,candidate:{...candidate,symbol:'ETH/USDT:USDT',base:'ETH'},actionable:false,status:'blocked',error_code:'outside_entry_zone',blocked_reasons:['Current executable quote is outside the planned entry zone.']};
 const failed={rank:3,candidate:{...candidate,symbol:'SOL/USDT:USDT'},signal:null,status:'failed',error_code:'candle_gap',error:'Missing one 15m candle.',actionable:false};
 const report={status:'degraded',generated_at:new Date(now).toISOString(),started_at:new Date(now-5000).toISOString(),duration_seconds:5,considered_markets:4,excluded_by_limit:0,analyzed_markets:2,actionable_markets:1,blocked_markets:1,failed_markets:1,rejected_markets:1,run_id:'test-ui-fixture',items:[good,blocked,failed],errors:[],rejections:[{symbol:'TEST/USDT:USDT',code:'low_volume',reason:'Below minimum turnover <img src=x onerror=alert(1)>'}],metrics:{counters:{http_requests:7,ohlcv_rows:1500,cache_hits:5,signal_cache_hits:2},timings_seconds:{universe_discovery:.4,market_analysis:4,quote_refresh:.2,snapshot_write:.1}}};
 const scanRequests=[];
 await page.route('**/v1/scan',async route=>{const body=route.request().postDataJSON();scanRequests.push(body);assert.equal(body.maximum_quote_age_seconds,30);assert.equal(body.scan_timeout_seconds,180);await route.fulfill({json:body.track_paper?{...report,paper:{summary:{account_id:body.paper_account,open_positions:1,unresolved_positions:0},errors:[]}}:report});});
 let replayVerifications=0;
 await page.route(`**/v1/scanner/replays/${replayId}/verify`,async route=>{assert.equal(route.request().method(),'POST');replayVerifications++;await route.fulfill({json:{passed:replayVerifications===1,differences:replayVerifications===1?[]:['signal.score: <img src=x onerror=alert(1)>']}});});
 const exportedSnapshot={snapshot_id:replayId,schema_version:1,as_of:new Date(now).toISOString(),engine_config:{fixture:true},frame_data:{'15m':[{timestamp:new Date(now-900000).toISOString(),open:99990,high:100010,low:99980,close:100000,volume:100}]},candidate,expected_signal:signal};
 await page.route(`**/v1/scanner/replays/${replayId}`,route=>route.fulfill({json:exportedSnapshot}));
 for(let attempt=0;attempt<40;attempt++){try{await page.goto('http://127.0.0.1:8861/');break;}catch(error){if(attempt===39)throw error;await new Promise(resolve=>setTimeout(resolve,250));}}
 await page.click('[data-tab="scanner"]');await page.click('#scanButton');
 await page.waitForSelector('#scanResults:not(.hidden)');
 assert.match(await page.locator('#scanSummary').textContent(),/1 qualified now/);
 assert.match(await page.locator('#scanRows').textContent(),/QUALIFIED LONG/);
 assert.match(await page.locator('#scanRows').textContent(),/BLOCKED/);
 assert.match(await page.locator('#scanRows').textContent(),/FAILED \[candle_gap\]/);
 assert.equal(await page.locator('#scanRejections img').count(),0);
 assert.equal(scanRequests[0].track_paper,false,'Paper tracking must require an explicit opt-in.');
 assert.match(await page.locator('#scanMetrics').textContent(),/HTTP requests7/);
 assert.match(await page.locator('#scanMetrics').textContent(),/Downloaded candles1,500/);
 await page.getByText('Scan timing and replay evidence',{exact:true}).click();
 assert.match(await page.locator('#scanTiming').textContent(),/market analysis4s/);
 await page.locator('#scanRows summary').first().click();
 assert.match(await page.locator('#scanRows .scan-detail').first().innerText(),/47 contracts/);
 await page.locator('#scanRows [data-replay]').first().click();
 await page.waitForFunction(()=>document.querySelector('#scanRows .replay-result')?.textContent.includes('Replay matched'));
 assert.equal(await page.locator('#scanRows [data-replay]').first().isDisabled(),false);
 const downloaded=page.waitForEvent('download');
 await page.locator('#scanRows [data-export-replay]').first().click();
 const download=await downloaded;
 assert.equal(download.suggestedFilename(),`signal-replay-${replayId.slice(0,12)}.json`);
 const exportPath=path.join(artifactDir,'exported-replay.json');
 await download.saveAs(exportPath);
 assert.deepEqual(JSON.parse(await fs.readFile(exportPath,'utf8')),exportedSnapshot);
 await page.locator('#scanRows [data-replay]').first().click();
 await page.waitForFunction(()=>document.querySelector('#scanRows .replay-result')?.textContent.includes('Replay differs'));
 assert.equal(await page.locator('#scanRows .replay-result img').count(),0);
 assert.ok((await page.locator('#scanRows .replay-result').first().textContent()).includes('<img src=x'));
 const desktopTable=await page.locator('#scanResults .table-wrap').first().evaluate(el=>({width:el.clientWidth,scroll:el.scrollWidth}));
 assert.ok(desktopTable.scroll<=desktopTable.width+1,JSON.stringify(desktopTable));
 await assertScrolledNavigation(page);
 await fullPageScreenshot(page,path.join(artifactDir,'scanner-desktop.png'));
 await page.locator('#scanPaper').selectOption('on');
 await page.locator('#scanPaperAccount').fill('browser <paper>');
 await page.click('#scanButton');
 await page.waitForFunction(()=>document.getElementById('scanMessages').textContent.includes('browser <paper>'));
 assert.equal(scanRequests[1].track_paper,true);
 assert.equal(scanRequests[1].paper_account,'browser <paper>');
 assert.equal(await page.locator('#scanMessages paper').count(),0);
 await page.evaluate(()=>{state.lastScanReport.items[0].valid_until=new Date(Date.now()-1).toISOString();updateScanExpiry();});
 assert.match(await page.locator('#scanRows').textContent(),/EXPIRED/);
 assert.match(await page.locator('#scanSummary').textContent(),/0 qualified now/);
 assert.equal(await page.locator('#scanButton').isDisabled(),false);
 await page.setViewportSize({width:390,height:844});
 await fullPageScreenshot(page,path.join(artifactDir,'scanner-mobile.png'));
 const size=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
 assert.ok(size.scroll<=size.width+1,JSON.stringify(size));
 assert.deepEqual(errors,[]);
 const charts=[];
 for(const ratio of [1,1.25,2]) charts.push(await checkChartFlow(browser,artifactDir,ratio));
 const evidence=await checkPaperAndResearchFlow(browser,artifactDir);
 console.log(JSON.stringify({desktop:'passed',mobile:'passed',scrolledNavigation:'passed',expiry:'passed',escaping:'passed',scanMetrics:'passed',replayVerifyAndExport:'passed',paperOptIn:'passed',pageErrors:errors,charts,evidence}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
