"use strict";
// Public captured source facts plus controlled transport placements. No account proof.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const reader = require('../static/reader.js');
const captured = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/discovery-captured.json')));
const raw = captured.results.flatMap(r => r.items).find(i => i.source_id === 'mittr');
const id = 'story:' + crypto.createHash('sha256').update(raw.canonical_url).digest('hex');
const components = Object.fromEntries(['relevance','freshness','trend','editor_consensus','deliberate_surprise','diversity','repetition_penalty','source_fatigue_penalty','final_score'].map(k=>[k,0]));
const card = {canonical_url:raw.canonical_url, coverage_mentions:[],language:raw.language,next_cursor:null,ordering_key:{},ordering_mode:'weighted_total',page_order_mode:'discovery',position:0,publication_seq:0,published_at:raw.published_at,score_components:components,story_id:id,summary:raw.description,title:raw.title,topic_ids:[],topic_ranks:{},source_kind:'outlet',source_name:raw.source_name,ranking_explanation:'Controlled transport fixture from captured source facts.',read_at:null,saved_at:null,state_revision:0,interests:[]};
const envelope = {schema_version:1,status:'ready',reason_code:'',edition:{edition_id:'contract-edition-1',generated_at:captured.generated_at,code_revision:'a'.repeat(40),policy_revision:2,policy_digest:'b'.repeat(64),snapshot_digest:captured.content_digest,profile_revision:0,receipt_digest:'c'.repeat(64),stale:false,disclosures:['Controlled auth transport, not live account verification.'],shortfalls:{updates:8,hot:6,interested:6,surprise:3},entries:[{position:1,primary_lane:'surprise',reason:'Captured publisher facts in a controlled transport placement.',secondary_reasons:[],card}]}};
function clone(x){return JSON.parse(JSON.stringify(x));}
assert.equal(reader.validateDiscovery(envelope).edition.entries[0].card.topic_ids.length,0);
assert.throws(()=>reader.validateFeedPage([card]));
const saved={...card,page_order_mode:'saved_at',next_cursor:{before_saved_at:captured.generated_at,before_story_id:id}};
assert.equal(reader.validateSavedPage([saved]).length,1);
for(const mutate of [v=>v.owner_user_id='unexpected',v=>v.edition.entries[0].position=2,v=>v.edition.entries.push(clone(v.edition.entries[0])),v=>v.edition.entries[0].card.next_cursor={},v=>v.edition.entries[0].card.publication_seq=1,v=>v.edition.entries[0].card.score_components.freshness=NaN,v=>v.edition.entries[0].secondary_reasons=[{lane:'surprise',reason:'duplicate'}]]){const v=clone(envelope);mutate(v);assert.throws(()=>reader.validateDiscovery(v));}
assert.equal(reader.validateDiscovery({schema_version:1,status:'unavailable',reason_code:'no_private_edition',edition:null}).status,'unavailable');
async function main(){
let calls=[];
const api=reader.createApi({url:'https://project-ref.supabase.co',key:'contract-public-key'},async()=>({access_token:'explicit-contract-auth-transport'}),async(url,options)=>{calls.push({url,options});return{ok:true,status:200,url,redirected:false,text:async()=>JSON.stringify(envelope)}});
await api.discoveryEdition();
assert.equal(calls[0].options.cache,'no-store');
assert.deepEqual(JSON.parse(calls[0].options.body),{p_edition_id:null});
assert.equal(calls[0].url.endsWith('/discovery_edition'),true);
const unsigned=reader.createApi({url:'https://project-ref.supabase.co',key:'contract-public-key'},async()=>null,async()=>{throw Error('must not fetch')});
await assert.rejects(unsigned.discoveryEdition());
console.log('discovery reader contract: PASS');
}
module.exports={envelope,card,captured};
if(require.main===module)main().catch(e=>{console.error(e);process.exitCode=1});
