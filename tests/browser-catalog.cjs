// Live check of the model picker embedded in experiment configuration.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 const origin=process.env.PREVIEW_ORIGIN||'http://127.0.0.1:8001';
 await page.goto(origin+'/#catalog');
 await page.waitForFunction(()=>document.querySelectorAll('#model-picker .model-option').length>0,{}, {timeout:60000});
 const models=page.locator('#model-picker .model-option');const count=await models.count();
 assert.equal(await page.locator('[data-tab="catalog"]').count(),0);
 assert.ok(await page.locator('#experiment').isVisible());
 const name=await models.first().locator('.model-name').textContent();
 await page.locator('#model-search').fill(name);
 await page.getByRole('checkbox',{name:`选择 ${name}`,exact:true}).check();
 assert.ok(await page.locator('#model-list .model-row').filter({hasText:name}).count()>0);
 await page.locator('#model-search').fill('');assert.equal(await models.count(),count);
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.deepEqual(errors,[]);await browser.close();console.log(`Model picker: ${count} models, search, selection, legacy hash redirect and mobile layout passed.`);
})().catch(error=>{console.error(error);process.exit(1)});
