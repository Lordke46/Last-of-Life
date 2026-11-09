const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('confidence report shows real results, sample size, version and escaped league names', () => {
  const context = vm.createContext({state:{leagues:[{id:'test',name:'<Liga>'}]},
    esc:(s)=>String(s).replace(/</g,'&lt;').replace(/>/g,'&gt;')});
  vm.runInContext(fs.readFileSync('static/model-report.js','utf8'),context);
  context.report={groups:[{league:'test',confidence_label:'Yüksek',n:2,won:1,hit_rate:.5,
    predicted_probability:.85,interval95:[.0945,.9055],version:'abcdef123456',policy:'hit_rate_v1'}],legacy_without_confidence:3};
  const html=vm.runInContext('confidenceReportHtml(report)',context);
  assert.match(html,/&lt;Liga&gt;/);
  assert.match(html,/1 \/ 2/);
  assert.match(html,/50.0%/);
  assert.match(html,/85.0%/);
  assert.match(html,/abcdef12/);
  assert.match(html,/İsabet öncelikli/);
  assert.match(html,/3 eski maç/);
  assert.match(vm.runInContext('confidenceReportHtml({groups:[]})',context),/Henüz sonuçlanan/);
});

test('price timing and matched market benchmark are labelled without promising profit',()=>{
 const context=vm.createContext({state:{leagues:[]},esc:s=>String(s),mrPct:p=>(p*100).toFixed(1)+'%'});
 vm.runInContext(fs.readFileSync('static/model-report.js','utf8'),context);
 context.sample={summary:{n:2},segments:{odds_band:[{key:'1.40–1.79',n:2,hit_rate:.5,realized_roi:-.2}]},
  price_tracking:{n:1,mean_price_difference:.1},market_comparison:{n:1,blend_brier:.1,market_brier:.2,reliable:false}};
 const html=vm.runInContext('realOddsPerformanceHtml(sample)',context);
 assert.match(html,/10.0%/);
 assert.match(html,/Marj düzeltilmemiş/);
 assert.match(html,/başarı sonucu çıkarılamaz/);
 assert.match(html,/1.40–1.79/);
});

test('engine comparison labels selected or candidate delta and strongest picks separately',()=>{
 const context=vm.createContext({state:{leagues:[{id:'test',name:'Test Liga'}]},esc:s=>String(s),mrPct:p=>(p*100).toFixed(1)+'%'});
 vm.runInContext(fs.readFileSync('static/model-report.js','utf8'),context);
 context.engines={test:{fitted_weight:0,status:'incumbent_retained',training_matches:500,validation:{
  '0.5':{n:100,delta_brier:-.003,blocks:10,upper95:-.001},'1.0':{n:100,delta_brier:.002,blocks:10,upper95:.004}}}};
 const engine=vm.runInContext('ensembleReportHtml(engines)',context);
 assert.match(engine,/Motor karşılaştırması/);
 assert.match(engine,/yalnız geçmiş doğrulama desteklerse/);
 assert.match(engine,/Test Liga/);
 assert.match(engine,/Mevcut/);
 assert.match(engine,/Aday −0\.0030/);
 context.picks={summary:{n:7,hit_rate:.43,predicted_rate:.58}};
 const picks=vm.runInContext('strongestPicksHtml(picks)',context);
 assert.match(picks,/En yüksek olasılık seçeneği: kaydedilmiş sonuçlar/);
 assert.match(picks,/43\.0%/);
 assert.match(picks,/dengeli önerilerle aynı maçlarda örtüşebilir/);
 assert.equal(vm.runInContext('strongestPicksHtml({summary:{n:0}})',context),'');
});
