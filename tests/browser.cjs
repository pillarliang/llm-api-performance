// Run against an isolated preview; generation is intercepted and never reaches a provider.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--renderer-process-limit=1','--no-zygote']});
 const page=await browser.newPage({viewport:{width:1440,height:1100}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const origin=process.env.PREVIEW_ORIGIN||'http://127.0.0.1:18003';
 const shots=process.env.ARTIFACT_DIR||'/tmp/provider-qa';fs.mkdirSync(shots,{recursive:true});
 let firstConfig=true,defaultId,secondId;
 await page.route('**/api/config',async route=>{
   if(route.request().method()==='GET'&&firstConfig){firstConfig=false;const response=await route.fetch();const data=await response.json();delete data.config.ui_version;delete data.config.providers;await route.fulfill({response,json:data});}
   else await route.continue();
 });
 let slow=false,release,fail=false;
 await page.route('**/api/models',async route=>{
   const connection=route.request().postDataJSON().connection;
   if(slow){slow=false;await new Promise(resolve=>release=resolve);}
   if(fail){await route.fulfill({status:502,json:{detail:'模拟列表读取失败'}});return;}
   await route.fulfill({json:{count:2,stale:false,models:[{id:'same-model',owned_by:'Test'},{id:connection.mode==='default'?'default-only':'custom-only',owned_by:'Test'}]}});
 });
 let startCalls=0,runBody,startPlan,releaseStart;
 await page.route('**/api/runs',async route=>{
   if(route.request().method()==='GET'){await route.fulfill({json:[]});return;}
   startCalls++;runBody=route.request().postDataJSON();
   const response=await page.request.post(origin+'/api/plan',{data:runBody});
   if(response.status()!==200){await route.fulfill({status:response.status(),json:await response.json()});return;}
   startPlan=await response.json();
   await new Promise(resolve=>releaseStart=resolve);
   await route.fulfill({json:{status:'starting',plan:startPlan}});
 });
 await page.goto(origin,{waitUntil:'domcontentloaded',timeout:90000});
 await page.getByRole('checkbox',{name:'选择 same-model',exact:true}).waitFor({timeout:60000}).catch(async e=>{console.error('notice:',await page.locator('#notice').textContent(),'errors:',errors);throw e;});
 assert.equal(await page.locator('#check-plan').count(),0);
 assert.equal(await page.locator('#start-run').isDisabled(),true);
 defaultId=await page.locator('#picker-provider').inputValue();
 await page.getByRole('checkbox',{name:'选择 same-model',exact:true}).check();
 assert.equal(await page.locator('#start-run').isDisabled(),false);
 await page.locator('#add-provider').click();
 secondId=await page.locator('#picker-provider').inputValue();
 await page.locator('#provider-name').fill('Provider B');
 await page.locator('#base-url').fill('https://provider-b.test/v1');
 await page.locator('#api-key').fill('browser-source-b-secret');
 await page.locator('#reload-picker').click();
 await page.getByRole('checkbox',{name:'选择 custom-only',exact:true}).waitFor();
 assert.equal(await page.getByRole('checkbox',{name:'选择 same-model',exact:true}).isChecked(),false);
 await page.getByRole('checkbox',{name:'选择 same-model',exact:true}).check();
 assert.equal(await page.locator('#model-list .model-row').count(),2);
 assert.deepEqual(await page.locator('.model-source').allTextContents(),['阿里云 SG','Provider B']);
 const firstRow=page.locator('#model-list .model-row').nth(0),secondRow=page.locator('#model-list .model-row').nth(1);
 await firstRow.getByRole('button',{name:'自定义请求参数',exact:true}).click();
 await firstRow.locator('textarea').fill('{"temperature":0.2}');
 await secondRow.getByRole('button',{name:'自定义请求参数',exact:true}).click();
 assert.deepEqual(JSON.parse(await secondRow.locator('textarea').inputValue()),{});
 await secondRow.locator('textarea').fill('{"temperature":0.8}');
 assert.deepEqual(JSON.parse(await firstRow.locator('textarea').inputValue()),{temperature:0.2});
 await firstRow.getByRole('button',{name:'自定义请求参数',exact:true}).click();
 await secondRow.getByRole('button',{name:'自定义请求参数',exact:true}).click();
 await page.locator('#picker-provider').selectOption(defaultId);
 assert.equal(await page.getByRole('checkbox',{name:'选择 same-model',exact:true}).isChecked(),true);
 assert.equal(await page.locator('#base-url').inputValue(),'https://sg.test/v1');
 await page.locator('#picker-provider').selectOption(secondId);
 assert.equal(await page.locator('#api-key').inputValue(),'browser-source-b-secret');
 await page.locator('#custom-model-name').fill('same-model');await page.locator('#add-custom-model').click();
 assert.equal(await page.locator('#model-list .model-row').count(),2);
 await page.getByText('已自动保存到本机',{exact:true}).waitFor();
 const saved=await page.evaluate(()=>JSON.parse(localStorage.getItem('llm-performance.workspace.v1')));
 assert.equal(saved.config.ui_version,3);assert.equal(saved.config.providers.length,2);
 assert.deepEqual(saved.config.models,[]);
 assert.ok(!JSON.stringify(saved).includes('browser-source-b-secret'));
 // Starting with missing credentials must fail before the preview can launch anything.
 await page.reload();await page.getByRole('checkbox',{name:'选择 same-model',exact:true}).waitFor();assert.equal(await page.locator('#model-list .model-row').count(),0);
 await page.locator('#picker-provider').selectOption(defaultId);await page.getByRole('checkbox',{name:'选择 same-model',exact:true}).check();await page.locator('#picker-provider').selectOption(secondId);await page.getByRole('checkbox',{name:'选择 same-model',exact:true}).check();
 await page.locator('#picker-provider').selectOption(secondId);
 assert.equal(await page.locator('#api-key').inputValue(),'browser-source-b-secret');
 await page.locator('#clear-local-key').click();
 assert.equal(await page.locator('#api-key').inputValue(),'');
 await page.locator('#start-run').click();await page.locator('#notice').waitFor();
 assert.match(await page.locator('#notice').textContent(),/Provider B.*API Key/);
 assert.equal(startCalls,1);
 await page.locator('#api-key').fill('browser-source-b-secret');
 await page.locator('#start-run').click();
 await page.waitForFunction(()=>document.querySelector('#start-run').disabled);
 while(!releaseStart)await page.waitForTimeout(20);
 assert.equal(startCalls,2);assert.equal(startPlan.pending_calls,6);
 assert.equal(await page.locator('#provider-name').isDisabled(),true);
 await page.locator('#start-run').evaluate(el=>el.click());assert.equal(startCalls,2);
 assert.equal(runBody.connections.find(p=>p.id===secondId).api_key,'browser-source-b-secret');
 releaseStart();await page.getByText('后台任务已创建，可继续新建评测',{exact:true}).waitFor();await page.locator('.tabs [data-tab=experiment]').click();
 assert.equal(await page.locator('#provider-name').isDisabled(),false);
 // Late catalog responses must never replace the newly selected source's list.
 await page.locator('#reload-picker').click();await page.getByRole('checkbox',{name:'选择 custom-only',exact:true}).waitFor();
 slow=true;await page.locator('#reload-picker').click();while(!release)await page.waitForTimeout(20);
 await page.locator('#picker-provider').selectOption(defaultId);release();
 await page.getByRole('checkbox',{name:'选择 default-only',exact:true}).waitFor();
 assert.equal(await page.getByRole('checkbox',{name:'选择 custom-only',exact:true}).count(),0);
 await page.locator('#picker-provider').selectOption(secondId);
 fail=true;await page.locator('#reload-picker').click();await page.waitForFunction(()=>document.querySelector('#picker-status').textContent.includes('模拟列表读取失败'));
 await page.locator('#custom-model-name').fill('manual-model');await page.locator('#add-custom-model').click();
 assert.equal(await page.locator('#model-list .model-row').count(),3);fail=false;
 await page.locator('#reload-picker').click();await page.getByRole('checkbox',{name:'选择 custom-only',exact:true}).waitFor();
 await page.locator('#model-search').click();assert.equal(await page.locator('#model-search').evaluate(el=>getComputedStyle(el).outlineStyle),'none');
 await page.screenshot({path:shots+'/providers-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 await page.screenshot({path:shots+'/providers-mobile.png',fullPage:true});
 // Deleting B removes only its models and credentials; A remains bound to A.
 await page.locator('#remove-provider').click();
 assert.equal(await page.locator('#model-list .model-row').count(),1);
 assert.equal(await page.locator('#provider-list .provider-item').count(),1);
 assert.equal(await page.locator('.model-source').textContent(),'阿里云 SG');
 assert.equal(await page.locator('#remove-provider').isDisabled(),true);
 assert.deepEqual(errors,[]);await browser.close();
 console.log('PASS: multiple providers, same model, isolated edits/keys, persistence, one-click validation, duplicate start prevention, stale catalogs, deletion, desktop/mobile.');
})().catch(e=>{console.error(e);process.exit(1)});
