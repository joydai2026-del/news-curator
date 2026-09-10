"use strict";
// Headless full reader regression with captured publisher facts and test auth only.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
let chromium;try{({chromium}=require('playwright'));}catch(_){process.exit(77);}
const {envelope,card}=require('./discovery_reader_runner.js');
const root=process.argv[2];
const server=http.createServer((req,res)=>{const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);const file=path.join(root,pathname==='/'?'index.html':pathname);if(!file.startsWith(root+path.sep)){res.writeHead(404);res.end();return;}try{res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':'text/html');res.end(fs.readFileSync(file));}catch(_){res.writeHead(404);res.end();}});
let browser;
async function main(){
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try{browser=await chromium.launch({headless:true,args:['--mute-audio']});}catch(e){if(!String(e).includes('Executable doesn'))throw e;browser=await chromium.launch({headless:true,channel:'chrome',args:['--mute-audio']});}
const context=await browser.newContext({viewport:{width:1200,height:850}});const page=await context.newPage();
let edition=JSON.parse(JSON.stringify(envelope)),held=null,hold=false;
let mutation=0;
await page.route('https://project-ref.supabase.co/rest/v1/rpc/**',async route=>{
const name=new URL(route.request().url()).pathname.split('/').pop();let payload;
if(name==='latest_publication')payload={finalized_at:card.published_at,initial_history_cursor:null,page_size:24,poll_seconds:15,publication_seq:1,topics:[{name:'AI',topic_id:'ai'}]};
else if(name==='feed_page'||name==='updates_since')payload=[];
else if(name==='discovery_edition'){if(hold){hold=false;held=route;return;}payload=edition;}
else if(name==='set_story_state'){mutation++;const body=route.request().postDataJSON();payload={status:'updated',read_at:body.p_read?card.published_at:null,saved_at:body.p_saved?card.published_at:null,revision:mutation};}
else throw Error('Unexpected RPC '+name);
await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(payload)});
});
await page.goto(`http://127.0.0.1:${server.address().port}/`);
await page.waitForFunction(()=>document.querySelector('[data-discovery-lane="updates"]').getAttribute('aria-pressed')==='true');
assert.match(await page.locator('#discovery-status').textContent(),/No verified publisher changes/);
assert.equal(await page.locator('.card:visible').count(),0);
await page.locator('button[data-discovery-lane="surprise"]').click();
assert.equal(await page.locator('.card:visible').count(),1);
assert.equal(await page.locator('.card[data-story-id]').count(),1);
await page.locator('.card:visible .accordion-toggle').click();
await page.waitForFunction(()=>document.querySelector('.card[data-discovery-position]').dataset.stateRevision==='1');
assert.equal(await page.locator('.card.is-read:visible').count(),1);
await page.locator('.card:visible .save-action').click();
await page.waitForFunction(()=>document.querySelector('.card[data-discovery-position]').classList.contains('is-saved'));
assert.equal(await page.locator('.card.is-read:visible').count(),1);
const before=await page.locator('.card:visible').boundingBox();
await page.locator('.card:visible .read-action').click();
await page.waitForFunction(()=>!document.querySelector('.card[data-discovery-position]').classList.contains('is-read'));
const after=await page.locator('.card:visible').boundingBox();assert.ok(Math.abs(before.y-after.y)<2);
assert.equal(await page.locator('.card:visible a.add-interest').getAttribute('href'),'/auth/callback/');
assert.equal(await page.locator('.card:visible .interest-action').count(),0);
assert.equal(await page.locator('.card:visible a').filter({hasText:'Read original'}).getAttribute('target'),'_blank');
await page.screenshot({path:path.join(root,'discovery-desktop.png')});
await page.setViewportSize({width:390,height:844});
assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
await page.screenshot({path:path.join(root,'discovery-mobile.png')});
await page.setViewportSize({width:1200,height:850});
await page.locator('#q').fill('controlled-no-match');assert.equal(await page.locator('.card:visible').count(),0);await page.locator('#q').fill('');
await page.locator('.chip[data-filter="ai"]:visible').click();assert.equal(await page.locator('.card:visible').count(),0);
await page.locator('.chip[data-filter="__all__"]:visible').click();assert.equal(await page.locator('.card:visible').count(),1);
// Poll a later private edition and preserve existing content until explicit click.
edition=JSON.parse(JSON.stringify(envelope));edition.edition.edition_id='contract-edition-2';edition.edition.entries[0].reason='Second controlled placement, same captured publisher facts.';
await page.evaluate(()=>window.__discoveryPoll());await page.locator('#discovery-notice:not([hidden])').waitFor();
assert.equal(await page.locator('.signal').getByText('Second controlled placement, same captured publisher facts.').count(),0);
await page.locator('#discovery-accept').click();await page.waitForFunction(()=>document.querySelector('.signal').textContent.includes('Second controlled placement'));assert.equal(await page.locator('.signal').getByText('Second controlled placement, same captured publisher facts.').count(),1);
// Account changes clear private content before the next account's response lands.
hold=true;await page.evaluate(()=>window.__discoveryPoll());await page.waitForTimeout(50);
const previousAccount=held;edition={schema_version:1,status:'unavailable',reason_code:'no_private_edition',edition:null};
await page.evaluate(()=>{window.__token='controlled-other-account';window.dispatchEvent(new Event('news-curator:auth-changed'));});
await page.waitForFunction(()=>document.querySelector('#discovery-status').textContent.includes('not ready yet'));
assert.equal(await page.locator('[data-discovery-position]').count(),0);
await previousAccount.fulfill({status:200,contentType:'application/json',body:JSON.stringify(envelope)});
await page.waitForTimeout(50);assert.equal(await page.locator('[data-discovery-position]').count(),0);
edition=JSON.parse(JSON.stringify(envelope));
await page.evaluate(()=>window.dispatchEvent(new Event('news-curator:auth-changed')));
await page.waitForFunction(()=>document.querySelector('button[data-discovery-lane="updates"]').getAttribute('aria-pressed')==='true');
await page.locator('button[data-discovery-lane="surprise"]').click();
// Delayed previous-account response must not restore private cards after logout.
hold=true;await page.evaluate(()=>window.__discoveryPoll());await page.waitForTimeout(50);
await page.evaluate(()=>{window.__signed=false;window.dispatchEvent(new Event('news-curator:auth-changed'));});
await page.waitForFunction(()=>document.querySelector('#discovery-controls').hidden);
assert.equal(await page.locator('[data-discovery-lane]').count(),4);
assert.equal(await page.locator('[data-discovery-position]').count(),0);
assert.equal(await page.locator('.secondary-reason').count(),0);
if(held)await held.fulfill({status:200,contentType:'application/json',body:JSON.stringify(edition)});
await page.waitForTimeout(50);assert.equal(await page.locator('[data-discovery-position]').count(),0);
assert.equal(await page.locator('#sections').getByText('Second controlled placement, same captured publisher facts.').count(),0);
await page.screenshot({path:path.join(root,'discovery-logout.png')});
await context.close();console.log('discovery reader browser: PASS (controlled auth transport, no live account proof)');
}
main().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));});
setTimeout(()=>process.exit(1),45000).unref();
