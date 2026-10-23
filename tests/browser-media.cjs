// Run only against an isolated preview with disposable local media data; model calls are intercepted.
const {chromium}=require('/browser/node_modules/playwright');
const origin=process.env.PREVIEW_ORIGIN||'http://127.0.0.1:18009';
const assert=require('node:assert/strict');const fs=require('node:fs');
(async()=>{const browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});const page=await browser.newPage({viewport:{width:1440,height:1100}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('**/api/models',r=>r.fulfill({json:{models:[{id:'qwen3.8-max'}],count:1,stale:false}}));
await page.goto(origin);await page.waitForSelector('.model-option');await page.locator('.model-option input').first().check();
await page.selectOption('#input-modality','upload');await page.waitForFunction(()=>document.querySelector('#media-status').textContent.includes('个人素材'));
await page.setInputFiles('#media-files',['/qa/image.jpg','/qa/video.mp4']);await page.click('#upload-media');await page.waitForFunction(()=>document.querySelectorAll('#media-assets fieldset').length===2,{timeout:30000});
const cookie=(await page.context().cookies()).find(c=>c.name==='visual_dataset_owner');fs.writeFileSync('/qa/cookie.json',JSON.stringify(cookie));
const image=page.locator('#media-assets fieldset').filter({has:page.locator('legend',{hasText:'image.jpg'})});
assert.equal(await image.locator('input').count(),2);await image.locator('input[value="image:512"]').check();
const video=page.locator('#media-assets fieldset').filter({has:page.locator('legend',{hasText:'video.mp4'})});assert.equal(await video.locator('input').count(),1);await video.locator('input[value="video:10:360"]').check();
await page.click('#prepare-media');await page.waitForFunction(()=>document.querySelector('#media-status').textContent.includes('已就绪'),{timeout:90000});
await page.waitForFunction(()=>!document.querySelector('#start-run').disabled);
let plan;await page.route('**/api/runs',async route=>{if(route.request().method()!=='POST')return route.continue();const data=route.request().postDataJSON();assert.equal(data.media.source,'upload');const response=await page.request.post(origin+'/api/plan',{data});assert.equal(response.status(),200,await response.text());plan=await response.json();assert.equal(plan.groups_detail.length,2);assert.deepEqual(new Set(plan.groups_detail.map(g=>g.modality)),new Set(['image','video_file']));assert(!JSON.stringify(plan).includes('x-oss-signature'));await route.fulfill({status:409,json:{detail:'浏览器验证完成，未发起模型调用'}});});
await page.screenshot({path:'/qa/desktop.png',fullPage:true});await page.click('#start-run');await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('浏览器验证完成'));
await page.setViewportSize({width:390,height:844});await page.screenshot({path:'/qa/mobile.png',fullPage:true});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);assert.deepEqual(errors,[]);
console.log('PASS: dynamic tiers, local upload/preparation, media plan, no model calls, desktop/mobile');await browser.close();})();
