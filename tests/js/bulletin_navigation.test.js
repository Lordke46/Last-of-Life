const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('static/app.js','utf8');
const helpers=source.slice(source.indexOf('function resetMatchFilters()'),source.indexOf('function renderBulletinDays()'));
test('opening view includes all leagues in the daily bulletin',()=>{
 assert.match(source,/bulletinCategory:"all"/);
 assert.match(source,/state\.bulletinCategory="main"; state\.league=event\.target\.value === "all"/);
});
test('all date navigation resets stale high/live filters and updates the selected date',()=>{
 const state={bulletinDay:'2099-01-02'},matchFilters={high:true,live:true};let renders=0, favoriteResets=0; const urls=[];
 let liveRefreshes=0;
 const context=vm.createContext({state,matchFilters,Date,URL,location:{href:'http://localhost/'},history:{replaceState:(_,__,url)=>urls.push(String(url))},window:{Extras:{resetMatchView:()=>favoriteResets++}},renderMatches:()=>renders++,refreshLive:()=>liveRefreshes++});
 vm.runInContext(helpers,context);
 vm.runInContext('shiftBulletinDay(-1)',context);
 assert.equal(state.bulletinDay,'2099-01-01');
 assert.deepEqual(matchFilters,{high:false,live:false});
 matchFilters.high=true;matchFilters.live=true;
 vm.runInContext('setBulletinDay("2099-01-03")',context);
 assert.equal(state.bulletinDay,'2099-01-03');
 assert.deepEqual(matchFilters,{high:false,live:false});
 assert.equal(renders,2);
 assert.equal(favoriteResets,2);
 assert.equal(liveRefreshes,2);
 assert.match(urls[1],/day=2099-01-03/);
});
