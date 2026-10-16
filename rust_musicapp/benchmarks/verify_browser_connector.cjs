// Development-only Node check; the app and connector never launch Node.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../browser_connector/popup.js'), 'utf8');
async function scenario(url, cookies, expectedPosts) {
  let click, posts=0, reads=0;
  const button={disabled:false,addEventListener:(_,action)=>{click=action;}}, status={textContent:''};
  const context={URL,URLSearchParams,AbortSignal,document:{getElementById:id=>id==='connect'?button:status},
    chrome:{tabs:{query:async()=>[{id:11,url}]},cookies:{getAllCookieStores:async()=>[{id:'profile-A',tabIds:[11]}],getAll:async filter=>{reads++;assert.deepEqual(JSON.parse(JSON.stringify(filter)),{domain:'youtube.com',storeId:'profile-A'});return cookies;}}},
    fetch:async(address,options)=>{posts++;assert.equal(address,'http://127.0.0.1:12345/session');assert.equal(options.headers['X-MusicApp-Token'],'a'.repeat(64));assert.equal(options.method,'POST');assert.equal(JSON.parse(options.body)[0].domain,'.youtube.com');return {ok:true,json:async()=>({verified:true})};}};
  vm.runInNewContext(source,context);await click();
  assert.equal(posts,expectedPosts);assert.equal(button.disabled,false);
  return {posts,reads,status:status.textContent};
}
(async()=>{
  const cookies=[{name:'SAPISID',value:'synthetic-test-only',domain:'.youtube.com',path:'/'}];
  const valid='http://127.0.0.1:12345/connect#musicapp='+'a'.repeat(64);
  for(const url of ['https://example.com/connect#musicapp='+'a'.repeat(64), valid.replace('127.0.0.1','localhost'),valid.replace('/connect','/elsewhere'), valid.replace('a'.repeat(64),'short')]) {
    const result=await scenario(url,cookies,0);assert.equal(result.reads,0);
  }
  const missing=await scenario(valid,[],0);assert.match(missing.status,/Sign in to YouTube/);
  const connected=await scenario(valid,cookies,1);assert.match(connected.status,/Connected/);
  console.log('6 connector checks passed: scoped profile/store, explicit handoff, invalid target denial, missing-login handling. No real cookies or network used.');
})().catch(error=>{console.error(error);process.exitCode=1;});
