const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('static/app.js','utf8');
const helpers=source.slice(source.indexOf('function resetMatchFilters()'),source.indexOf('function renderBulletinDays()'));
test('opening view asks for main leagues, not the full bulletin',()=>{
 assert.match(source,/bulletinCategory:"all"/);
 assert.match(source,/state\.bulletinCategory="main"; state\.league=event\.target\.value === "all"/);
});
test('all date navigation resets stale high/live filters and updates the selected date',()=>{
 const state={bulletinDay:'2099-01-02'},matchFilters={high:true,live:true};let renders=0;
 const context=vm.createContext({state,matchFilters,Date,renderMatches:()=>renders++});
 vm.runInContext(helpers,context);
 vm.runInContext('shiftBulletinDay(-1)',context);
 assert.equal(state.bulletinDay,'2099-01-01');
 assert.deepEqual(matchFilters,{high:false,live:false});
 matchFilters.high=true;matchFilters.live=true;
 vm.runInContext('setBulletinDay("2099-01-03")',context);
 assert.equal(state.bulletinDay,'2099-01-03');
 assert.deepEqual(matchFilters,{high:false,live:false});
 assert.equal(renders,2);
});
