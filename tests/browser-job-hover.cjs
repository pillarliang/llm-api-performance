// Regression: polling must preserve hovered/focused controls. All API writes are mocked.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const origin=process.env.PREVIEW_ORIGIN||'http://127.0.0.1:8001';
  const seed=await(await page.request.get(origin+'/api/config')).json();
  const job=(id,status)=>({job_id:id,status,name:'Hover '+id,version:'v1',path:`hover/v1/runs/${id}`,started_at:'2026-10-23T09:00:00Z',has_results:status==='finished',plan:{pending_calls:10,planned_cases:12,warmup_calls:2},progress:{completed:2,succeeded:2,active:1,warmups:1},targets:[{provider:'Mock',model:'test'}]});
  let jobs=[job('finished','finished'),job('active','running')];
  let runs=[{...jobs[0],run_id:'finished',status:'complete',totals:{successful_cases:10,planned_cases:10,requests:10}}];
  const stops=[],artifacts=[];
  await page.route('**/api/**',async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/api/config')return route.fulfill({json:seed});
   if(path==='/api/models')return route.fulfill({json:{count:0,models:[],stale:false}});
   if(path==='/api/jobs')return route.fulfill({json:jobs});
   if(path==='/api/runs')return route.fulfill({json:runs});
   if(path.endsWith('/stop')){const id=path.split('/')[3];stops.push(id);jobs.find(j=>j.job_id===id).status='cancelled';return route.fulfill({json:{stopped:true}});}
   if(path.startsWith('/api/artifact/')){artifacts.push(path);return route.fulfill({json:{settings:{name:'Hover report',version:'v1'},status:'complete',summary:[]}});}
   throw Error('Unexpected request '+path);
  });
  await page.goto(origin+'/#history');
  await page.waitForFunction(()=>document.querySelectorAll('.job-card').length===2&&document.querySelectorAll('#history-rows tr').length===1);
  await page.evaluate(()=>clearTimeout(pollTimer));
  const finished=page.locator('[data-job-id="finished"] button');
  const active=page.locator('[data-job-id="active"] button');
  const history=page.locator('#history-rows button');
  await finished.hover();await finished.focus();
  await finished.evaluate(el=>{window.savedButton=el;window.savedCard=el.closest('article');});
  await page.evaluate(async()=>{await poll();clearTimeout(pollTimer);});
  assert.equal(await finished.evaluate(el=>el===window.savedButton&&window.savedCard.isConnected),true,'poll replaced the result button');
  assert.equal(await finished.evaluate(el=>el===document.activeElement&&el.matches(':hover')&&getComputedStyle(el).cursor==='pointer'),true);
  // A progress update in another job must not interrupt a click in flight.
  await page.mouse.down();jobs[1].progress.completed=7;jobs[1].progress.succeeded=7;
  await page.evaluate(async()=>{await poll();clearTimeout(pollTimer);});
  assert.equal(await page.locator('[data-job-id="active"] [role=progressbar]').getAttribute('aria-valuenow'),'7');
  await page.mouse.up();await page.locator('#results').waitFor({state:'visible'});
  assert.equal(artifacts.at(-1),'/api/artifact/hover/v1/runs/finished/results.json');
  await page.locator('.tabs [data-tab=history]').click();
  await active.hover();await active.focus();await active.evaluate(el=>window.savedStop=el);
  jobs[1].progress.retries=3;
  await page.evaluate(async()=>{await poll();clearTimeout(pollTimer);});
  assert.equal(await active.evaluate(el=>el===window.savedStop&&el===document.activeElement&&el.matches(':hover')),true);
  assert.match(await page.locator('[data-job-id="active"] .job-detail').textContent(),/已重试 3 次/);
  await active.click();assert.deepEqual(stops,['active']);
  await page.locator('[data-job-id="active"] .badge').filter({hasText:'已停止'}).waitFor();
  // History refreshes must retain their buttons even when counters/status change.
  await history.hover();await history.focus();await history.evaluate(el=>window.savedHistory=el);
  runs[0].totals.requests=13;runs[0].status='partial';
  await page.evaluate(()=>refreshHistory());
  assert.equal(await history.evaluate(el=>el===window.savedHistory&&el===document.activeElement&&el.matches(':hover')),true);
  assert.match(await page.locator('#history-rows tr').textContent(),/待补跑.*13/);
  // Insert, reorder, remove, and transition a running job to results.
  jobs[1].status='finished';jobs[1].has_results=true;jobs[1].path='hover/v1/runs/latest';
  jobs=[job('new','running'),jobs[1],jobs[0]];
  await page.evaluate(async()=>{await poll();clearTimeout(pollTimer);});
  assert.deepEqual(await page.locator('.job-card').evaluateAll(els=>els.map(el=>el.dataset.jobId)),['new','active','finished']);
  await active.getByText('查看结果',{exact:true}).click();
  await page.locator('#results').waitFor({state:'visible'});
  assert.equal(artifacts.at(-1),'/api/artifact/hover/v1/runs/latest/results.json');
  jobs=[];runs=[];
  await page.evaluate(async()=>{await poll();clearTimeout(pollTimer);});
  await page.locator('.tabs [data-tab=history]').click();
  assert.equal(await page.locator('.job-card').count(),0);assert.equal(await page.locator('#history-rows tr').count(),0);
  assert.equal(await page.locator('#jobs-empty').isVisible(),true);assert.equal(await page.locator('#history-empty').isVisible(),true);
  assert.deepEqual(errors,[]);
  console.log('PASS: hover/focus and in-flight clicks survive polling; progress, stop, result links, history, reorder and removal work.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
