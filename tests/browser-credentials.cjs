// Synthetic keys only. All provider and application requests are intercepted.
const {chromium}=require('playwright');const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const origin=process.env.PREVIEW_ORIGIN||'http://127.0.0.1:18003';
 const seedPage=await browser.newPage();const seed=await(await seedPage.request.get(origin+'/api/config')).json();await seedPage.close();
 seed.config.ui_version=3;seed.config.models=[];
 seed.config.providers=[{id:'a',name:'Source A',mode:'custom',base_url:'https://a.test/v1'},{id:'b',name:'Source B',mode:'custom',base_url:'https://b.test/v1'}];
 let savedBody;const sent=[];const errors=[];
 async function context(blocked=false){
   const ctx=await browser.newContext();
   if(blocked)await ctx.addInitScript(()=>{for(const method of ['getItem','setItem','removeItem'])Storage.prototype[method]=()=>{throw new DOMException('Storage blocked','SecurityError');};});
   ctx.on('page',page=>page.on('pageerror',e=>errors.push(e.message)));
   await ctx.route('**/api/**',async route=>{
     const path=new URL(route.request().url()).pathname;
     if(path==='/api/config'){
       if(route.request().method()==='GET')await route.fulfill({json:seed});
       else{savedBody=route.request().postDataJSON();await route.fulfill({json:{saved:true}});}return;
     }
     if(path==='/api/models'){sent.push(route.request().postDataJSON().connection);await route.fulfill({json:{count:1,models:[{id:'same-model'}]}});return;}
     if(path==='/api/jobs'){await route.fulfill({json:[]});return;}
     if(path==='/api/runs'&&route.request().method()==='GET'){await route.fulfill({json:[]});return;}
     throw new Error('Unexpected request: '+path);
   });return ctx;
 }
 async function open(ctx){const page=await ctx.newPage();await page.goto(origin);await page.locator('#provider-name').waitFor();await page.waitForFunction(()=>document.querySelector('#provider-name').value==='Source A');return page;}
 const ctx=await context();const page=await open(ctx);
 await page.locator('#api-key').fill('synthetic-a-key');
 await page.locator('#picker-provider').selectOption('b');await page.locator('#api-key').fill('synthetic-b-key');
 await page.locator('#picker-provider').selectOption('a');await page.reload();await page.waitForFunction(()=>document.querySelector('#api-key').value==='synthetic-a-key');
 await page.locator('#picker-provider').selectOption('b');assert.equal(await page.locator('#api-key').inputValue(),'synthetic-b-key');
 await page.getByText('已自动保存到本机',{exact:true}).waitFor();
 const saved=await page.evaluate(()=>JSON.parse(localStorage.getItem('llm-performance.workspace.v1')));assert.ok(!JSON.stringify(saved.config).includes('synthetic-'));assert.equal(savedBody,undefined);
 const second=await context();const isolated=await open(second);assert.equal(await isolated.locator('#api-key').inputValue(),'');await isolated.locator('#picker-provider').selectOption('b');assert.equal(await isolated.locator('#api-key').inputValue(),'');await second.close();
 // A changed shared endpoint must never receive the key saved for the old endpoint.
 seed.config.providers[0].base_url='https://untrusted.test/v1';await page.evaluate(()=>localStorage.removeItem('llm-performance.workspace.v1'));
 await page.reload();await page.waitForFunction(()=>document.querySelector('#base-url').value==='https://untrusted.test/v1');assert.equal(await page.locator('#api-key').inputValue(),'');
 assert.ok(!sent.some(c=>c.base_url.includes('untrusted.test')));
 seed.config.providers[0].base_url='https://a.test/v1/';await page.evaluate(()=>localStorage.removeItem('llm-performance.workspace.v1'));await page.reload();await page.waitForFunction(()=>document.querySelector('#api-key').value==='synthetic-a-key');
 // Paths and schemes are part of the binding, not just the hostname.
 for(const base of ['https://a.test/other/v1','http://a.test/v1']){await page.locator('#base-url').fill(base);assert.equal(await page.locator('#api-key').inputValue(),'');}
 await page.locator('#base-url').fill('https://a.test/v1');assert.equal(await page.locator('#api-key').inputValue(),'synthetic-a-key');
 const tab=await open(ctx);assert.equal(await tab.locator('#api-key').inputValue(),'synthetic-a-key');
 await page.locator('#clear-local-key').click();await tab.waitForFunction(()=>document.querySelector('#api-key').value==='');
 await page.reload();await page.waitForFunction(()=>document.querySelector('#provider-name').value==='Source A');assert.equal(await page.locator('#api-key').inputValue(),'');
 await page.locator('#picker-provider').selectOption('b');assert.equal(await page.locator('#api-key').inputValue(),'synthetic-b-key');
 await page.locator('#remove-provider').click();
 assert.equal(await page.evaluate(()=>Object.keys(localStorage).filter(k=>k.startsWith('llm-performance.provider-key.v1:')).length),0);
 await ctx.close();
 // Storage failures fall back to page memory without falsely reporting persistence.
 const blocked=await context(true);const fallback=await open(blocked);await fallback.locator('#api-key').fill('session-only-key');
 assert.match(await fallback.locator('#connection-help').textContent(),/无法保存/);
 await fallback.locator('#picker-provider').selectOption('b');await fallback.locator('#picker-provider').selectOption('a');assert.equal(await fallback.locator('#api-key').inputValue(),'session-only-key');
 await fallback.reload();await fallback.waitForFunction(()=>document.querySelector('#provider-name').value==='Source A');assert.equal(await fallback.locator('#api-key').inputValue(),'');
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS: refresh persistence, browser isolation, endpoint/path/scheme binding, metadata-only config, cross-tab clearing, source deletion and blocked-storage fallback.');
})().catch(e=>{console.error(e);process.exit(1)});
