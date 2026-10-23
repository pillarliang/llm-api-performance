// Browser workspace autosave and simultaneous jobs. No real generation requests.
const {chromium}=require('playwright');const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const origin=process.env.PREVIEW_ORIGIN||'http://127.0.0.1:18003';
 const seed=await(await page.request.get(origin+'/api/config')).json();delete seed.config.ui_version;delete seed.config.providers;
 const jobs=[],stops=[];let configWrites=0;
 await context.route('**/api/**',async route=>{
  const path=new URL(route.request().url()).pathname,method=route.request().method();
  if(path==='/api/config'){if(method!=='GET')configWrites++;return route.fulfill({json:seed});}
  if(path==='/api/models')return route.fulfill({json:{count:1,models:[{id:'deepseek-chat'}],stale:false}});
  if(path==='/api/jobs')return route.fulfill({json:jobs});
  if(path.match(/^\/api\/jobs\/.+\/stop$/)){const id=path.split('/')[3];stops.push(id);jobs.find(j=>j.job_id===id).status='cancelled';return route.fulfill({json:{stopped:true}});}
  if(path==='/api/runs'&&method==='GET')return route.fulfill({json:[]});
  if(path==='/api/runs'&&method==='POST'){
   const payload=route.request().postDataJSON();assert.equal(payload.mode,'full');assert.equal(payload.config.providers.length,2);assert.equal(payload.config.max_retries,2);
   assert.equal(payload.connections.find(p=>p.api_key)?.api_key,'synthetic-deepseek-key');
   const job={job_id:String(jobs.length+1),status:'running',name:payload.config.name,version:payload.config.version,path:`test/v1/runs/${jobs.length+1}`,started_at:new Date().toISOString(),plan:{pending_calls:12,planned_cases:12,warmup_calls:0},progress:{completed:6,succeeded:5,failed:1,active:2,retrying:1,retries:3,attempts:11}};
   jobs.push(job);return route.fulfill({json:job});
  }
  throw Error('Unexpected request '+path);
 });
 await page.goto(origin);await page.locator('#add-provider').click();await page.locator('#provider-name').fill('DeepSeek');await page.locator('#base-url').fill('https://api.deepseek.com');await page.locator('#api-key').fill('synthetic-deepseek-key');
 await page.locator('#custom-model-name').fill('deepseek-chat');await page.locator('#add-custom-model').click();
 await page.locator('#token-picker summary').click();await page.locator('#input-tokens input[value="1024"]').check();
 await page.reload();await page.waitForFunction(()=>document.querySelector('#provider-name').value==='DeepSeek');
 assert.equal(await page.locator('#provider-list .provider-item').count(),2);assert.equal(await page.locator('#api-key').inputValue(),'synthetic-deepseek-key');assert.equal(await page.locator('#model-list .model-row').count(),0);assert.equal(await page.locator('#input-tokens input[value="1024"]').isChecked(),true);
 assert.equal(await page.locator('#start-run').isDisabled(),true);await page.getByRole('checkbox',{name:'选择 deepseek-chat',exact:true}).check();
 assert.equal(await page.locator('#save-config').count(),0);assert.equal(configWrites,0);
 await page.locator('#start-run').click();await page.locator('.job-card').waitFor();assert.match(await page.locator('.job-counts').textContent(),/已完成 6 \/ 12 项50%/);
 assert.equal(await page.locator('[role=progressbar]').getAttribute('aria-valuenow'),'6');assert.match(await page.locator('.job-detail').textContent(),/成功 5 · 失败 1.*已重试 3 次/);assert.equal(await page.locator('#events').count(),0);
 await page.locator('.tabs [data-tab=experiment]').click();assert.equal(await page.locator('#provider-name').isDisabled(),false);assert.equal(await page.locator('#start-run').isDisabled(),false);
 await page.locator('#start-run').click();await page.waitForFunction(()=>document.querySelectorAll('.job-card').length===2);assert.equal(jobs.length,2);
 await page.reload();await page.waitForFunction(()=>document.querySelectorAll('.job-card').length===2);assert.match(await page.locator('#active-count').textContent(),/2 个运行中/);
 await page.locator('[data-job-id="1"]').getByRole('button',{name:'停止任务'}).click();await page.locator('[data-job-id="1"]').getByText('已停止',{exact:true}).waitFor();assert.deepEqual(stops,['1']);assert.equal(jobs[1].status,'running');assert.match(await page.locator('#active-count').textContent(),/1 个运行中/);
 await page.screenshot({path:'/qa/song-jobs-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:'/qa/song-jobs-mobile.png',fullPage:true});
 await page.locator('.tabs [data-tab=experiment]').click();assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:'/qa/song-config-mobile.png',fullPage:true});
 await page.setViewportSize({width:1440,height:1000});await page.screenshot({path:'/qa/song-config-desktop.png',fullPage:true});
 // Upgrade an old browser that retained only a source key, without source metadata.
 const sourceId=await page.locator('#picker-provider').inputValue();
 await page.evaluate(()=>localStorage.removeItem('llm-performance.workspace.v1'));await page.reload();
 await page.waitForFunction(()=>document.querySelectorAll('#provider-list .provider-item').length===2);
 await page.locator('#picker-provider').selectOption(sourceId);assert.equal(await page.locator('#provider-name').inputValue(),'DeepSeek');assert.equal(await page.locator('#api-key').inputValue(),'synthetic-deepseek-key');
 const isolated=await browser.newPage();await isolated.goto(origin);await isolated.waitForFunction(()=>document.querySelector('#provider-list').children.length>0);assert.equal(await isolated.locator('#provider-list .provider-item').count(),1);assert.equal(await isolated.locator('#api-key').inputValue(),'');
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS: source/condition/key autosave and model selection reset without manual save, browser isolation, two independent jobs, refresh recovery, progress denominator, per-job stop, responsive layout.');
})().catch(e=>{console.error(e);process.exit(1)});
