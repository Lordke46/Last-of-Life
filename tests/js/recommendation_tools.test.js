const test=require('node:test');
const assert=require('node:assert/strict');
const {render}=require('../../static/recommendation-tools.js');

test('comparison escapes text, shows disagreement and permits only actionable real quotes',()=>{
 const item={market:'<1>',p:.7,model_p:.8,market_p:.6,odds:1.5,fair_odds:1.43,estimated_value:.05,
  model_market_disagree:true,meets_probability_threshold:true,actionable:false};
 const match={recommendation_tools:{preferred_odds:[1.4,2.3],by_probability:[item],by_value:[item],warnings:['<missing>'],note:'<note>'}};
 let calls=0;
 const html=render(match,()=>{calls++;return '<button>Add</button>';});
 assert.equal(calls,0);
 assert.match(html,/&lt;1&gt;/);
 assert.match(html,/&lt;missing&gt;/);
 assert.match(html,/Model ve piyasa ayrışıyor/);
 assert.match(html,/70%/);
 item.actionable=true;
 assert.match(render(match,()=>'<button>Add</button>'),/<button>Add<\/button>/);
});
test('missing detail tools and an empty price band remain truthful',()=>{
 assert.equal(render({}),'');
 assert.match(render({recommendation_tools:{preferred_odds:[1.4,2.3],by_probability:[],warnings:[],note:''}}),/karşılaştırılabilecek gerçek fiyatlı seçenek yok/);
});
