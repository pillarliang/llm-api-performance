const {chromium}=require('playwright');const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const origin=process.env.PREVIEW_ORIGIN||'http://127.0.0.1:18003';let first=true;
 await page.route('**/api/config',async route=>{if(first&&route.request().method()==='GET'){first=false;const response=await route.fetch();const seed=await response.json();delete seed.config.ui_version;delete seed.config.providers;await route.fulfill({response,json:seed});}else await route.continue();});
 await page.route('**/api/models',route=>route.fulfill({json:{count:1,models:[{id:'test-model'}],stale:false}}));
 await page.route('**/api/runs',async route=>{assert.equal(route.request().method(),'GET','This test must not start generation');await route.continue();});
 await page.goto(origin);await page.getByRole('checkbox',{name:'选择 test-model',exact:true}).check();
 const choice=value=>page.locator(`#input-tokens input[value="${value}"]`);
 assert.equal(await choice(512).isChecked(),true);assert.equal(await choice(1024).isChecked(),false);
 await page.locator('#token-picker summary').click();await choice(1024).check();await choice(2048).check();assert.equal(await page.locator('#pending-calls').textContent(),'9');
 const plan=()=>page.evaluate(async()=>{const response=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(configPayload())});if(!response.ok)throw Error(await response.text());return response.json();});
 let data=await plan();assert.deepEqual(data.groups_detail.map(g=>g.target_input_tokens),[512,1024,2048]);assert.equal(data.pending_calls,9);
 await choice('custom').check();assert.equal(await page.locator('#start-run').isDisabled(),true);
 await page.locator('#custom-tokens').fill('512, 4096, 4096');data=await plan();assert.deepEqual(data.groups_detail.map(g=>g.target_input_tokens),[512,1024,2048,4096]);assert.equal(data.pending_calls,12);
 for(const invalid of ['oops','1.5','0','-1','4096,']){await page.locator('#custom-tokens').fill(invalid);assert.equal(await page.locator('#start-run').isDisabled(),true);assert.equal(await page.locator('#input-token-error').isVisible(),true);}
 await page.locator('#custom-tokens').fill('4096，8192');await choice(1024).uncheck();
 await page.getByText('已自动保存到本机',{exact:true}).waitFor();
 assert.deepEqual(await page.evaluate(()=>JSON.parse(localStorage.getItem('llm-performance.workspace.v1')).config.models),[]);
 // Upgrade a previously saved selection: do not restore it on refresh.
 await page.evaluate(()=>{const saved=JSON.parse(localStorage.getItem('llm-performance.workspace.v1'));saved.config.models=config.models;localStorage.setItem('llm-performance.workspace.v1',JSON.stringify(saved));});
 await page.reload();await page.getByRole('checkbox',{name:'选择 test-model',exact:true}).waitFor();
 assert.equal(await page.locator('#model-list .model-row').count(),0);assert.equal(await page.getByRole('checkbox',{name:'选择 test-model',exact:true}).isChecked(),false);assert.equal(await page.locator('#start-run').isDisabled(),true);
 await page.getByRole('checkbox',{name:'选择 test-model',exact:true}).check();
 assert.equal(await page.locator('#token-picker').getAttribute('open'),null);await page.locator('#token-picker summary').click();
 assert.equal(await choice(512).isChecked(),true);assert.equal(await choice(1024).isChecked(),false);assert.equal(await choice(2048).isChecked(),true);assert.equal(await choice('custom').isChecked(),true);assert.equal(await page.locator('#custom-tokens').inputValue(),'4096, 8192');
 data=await plan();assert.deepEqual(data.groups_detail.map(g=>g.target_input_tokens),[512,2048,4096,8192]);
 for(const value of [512,2048,'custom'])await choice(value).uncheck();assert.equal(await page.locator('#start-run').isDisabled(),true);assert.match(await page.locator('#input-token-error').textContent(),/至少选择/);
 await choice(1024).check();assert.equal(await page.locator('#start-run').isDisabled(),false);assert.equal(await page.locator('#input-token-error').isVisible(),false);
 assert.equal(await page.locator('#token-selection').textContent(),'1024');await page.locator('#token-picker summary').press('Escape');assert.equal(await page.locator('#token-picker').getAttribute('open'),null);await page.locator('#token-picker summary').click();await page.locator('#repetitions').click();assert.equal(await page.locator('#token-picker').getAttribute('open'),null);await page.locator('#token-picker summary').click();
 await choice(512).check();await choice(2048).check();await page.locator('#input-tokens').scrollIntoViewIfNeeded();await page.screenshot({path:'/qa/token-multiselect-desktop.png'});
 await page.setViewportSize({width:390,height:844});await page.locator('#input-tokens').scrollIntoViewIfNeeded();assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:'/qa/token-multiselect-mobile.png'});
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS: preset multi-select, custom additions, unique groups/counts, validation, save/reload and mobile layout; no model calls.');
})().catch(e=>{console.error(e);process.exit(1)});
