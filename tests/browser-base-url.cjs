const {chromium}=require('playwright');const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const origin=process.env.PREVIEW_ORIGIN||'http://127.0.0.1:18003';
 const seed=await(await page.request.get(origin+'/api/config')).json();
 seed.config.ui_version=3;seed.config.models=[];seed.config.providers=[{id:'deepseek',name:'DeepSeek',mode:'custom',base_url:'https://api.deepseek.com'}];
 let requests=0,stale=false;
 await page.route('**/api/**',async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/api/config'){await route.fulfill({json:seed});return;}
   if(path==='/api/jobs'){await route.fulfill({json:[]});return;}
   if(path==='/api/runs'&&route.request().method()==='GET'){await route.fulfill({json:[]});return;}
   assert.equal(path,'/api/models');requests++;
   const base=route.request().postDataJSON().connection.base_url;
   const message=base.includes('/chat/completions')?'模型列表地址不存在（HTTP 404）。当前请求：https://api.deepseek.com/chat/completions/v1/models。你可能填入了具体接口路径，建议 Base URL：https://api.deepseek.com。':'模型列表地址不存在（HTTP 404）。请确认 Base URL 是 API 基础地址；若该来源未提供 /models，可手动添加模型。';
   if(stale)await route.fulfill({json:{count:1,models:[{id:'cached-model'}],stale:true,error:message}});
   else if(base==='https://api.deepseek.com')await route.fulfill({json:{count:1,models:[{id:'test-model'}],stale:false}});
   else await route.fulfill({status:502,json:{detail:message}});
 });
 await page.goto(origin);await page.waitForFunction(()=>document.querySelector('#provider-name').value==='DeepSeek');
 await page.locator('#base-url').fill('https://api.deepseek.com/chat/completions/v1');
 assert.equal(await page.locator('#base-url-suggestion').textContent(),'https://api.deepseek.com');
 assert.equal(requests,0);assert.equal(await page.locator('#base-url').inputValue(),'https://api.deepseek.com/chat/completions/v1');
 await page.locator('#api-key').fill('synthetic-old-address-key');await page.locator('#reload-picker').click();
 await page.waitForFunction(()=>document.querySelector('#picker-status').textContent.includes('建议 Base URL'));
 const before=requests;await page.locator('#use-base-url').click();
 assert.equal(await page.locator('#base-url').inputValue(),'https://api.deepseek.com');assert.equal(await page.locator('#api-key').inputValue(),'');assert.equal(requests,before);
 assert.equal(await page.locator('#base-url-hint').isVisible(),false);
 await page.locator('#api-key').fill('synthetic-corrected-key');await page.locator('#reload-picker').click();await page.getByRole('checkbox',{name:'选择 test-model',exact:true}).waitFor();
 await page.locator('#base-url').fill('https://proxy.test/custom/v1/models');assert.equal(await page.locator('#base-url-suggestion').textContent(),'https://proxy.test/custom/v1');
 await page.locator('#base-url').fill('https://api.deepseek.com/beta/chat/completions');assert.equal(await page.locator('#base-url-suggestion').textContent(),'https://api.deepseek.com/beta');
 await page.locator('#base-url').fill('https://proxy.test/models/gateway');assert.equal(await page.locator('#base-url-hint').isVisible(),false);
 await page.locator('#api-key').fill('synthetic-proxy-key');await page.locator('#reload-picker').click();await page.waitForFunction(()=>document.querySelector('#picker-status').textContent.includes('未提供 /models'));
 stale=true;await page.locator('#reload-picker').click();await page.getByRole('checkbox',{name:'选择 cached-model',exact:true}).waitFor();assert.match(await page.locator('#picker-status').textContent(),/未提供 \/models.*显示上次列表/);
 await page.locator('#base-url').fill('https://api.deepseek.com/chat/completions/v1');await page.screenshot({path:'/qa/url-hint-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:'/qa/url-hint-mobile.png',fullPage:true});
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS: immediate Base URL suggestions, explicit correction, no key transfer or automatic requests, proxy prefixes, actionable 404/stale diagnostics and mobile layout.');
})().catch(e=>{console.error(e);process.exit(1)});
