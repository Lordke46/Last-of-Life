/* Research is separate from model recommendations; no service key in the browser. */
(function (root) {
  'use strict';
  const fields = new Set(['fixture_verification','form','lineups','lineup_surprises','model_prior','key_developments','not_found']);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const category = {injury:'Sakatlık',suspension:'Ceza',absence:'Eksiklik',squad:'Kadro',probable_lineup:'Muhtemel kadro',official_lineup:'Resmî kadro iddiası',lineup:'Kadro',form:'Form',schedule:'Fikstür',fixture_load:'Fikstür yoğunluğu',coach:'Teknik direktör',tactics:'Taktik',news:'Haber',other:'Diğer'};
  const status = {confirmed_official:'Skill: resmî kaynaklı',confirmed_independent:'Skill: bağımsız doğrulama iddiası',single_source:'Tek kaynak bildirimi',conflicting:'Çelişkili',unverified:'Doğrulanmamış',not_found:'Bulunamadı',outdated:'Eski bilgi'};
  function derived(f) { const d=f && f.skill_details; return !!(d && fields.has(d.field) && !f.original_status && f.statement === 'Skill alanı: '+d.field); }
  function safeUrl(url) { try { const u=new URL(url); return (u.protocol==='http:' || u.protocol==='https:') && !u.username && !u.password ? u.href : ''; } catch (_) { return ''; } }
  function sourceLink(s, observation) { const url=safeUrl(s.url), label=s.publisher || s.title || 'Kaynak', origin=observation?.origin || s.origin; return `${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : esc(label)} <span class="muted">· yayın: ${esc(observation?.published_date || observation?.published_at || s.published_date || s.published_at || 'bilinmiyor')} · gözlem: ${esc(observation?.accessed_at || s.accessed_at || 'bilinmiyor')} · tür: ${esc(s.kind || 'unknown')}${s.tier != null ? ` · kademe: ${esc(s.tier)}` : ''}${origin ? ` · köken: ${esc(origin)}` : ''}</span>`; }
  function displayDate(value) { const d=new Date(value); return Number.isNaN(d.getTime()) ? esc(value || 'Bilinmiyor') : esc(new Intl.DateTimeFormat('tr-TR',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Istanbul'}).format(d))+' TSİ'; }
  function view(entry) {
    const p=entry.report || {}, findings=Array.isArray(p.findings)?p.findings:[], sources=Array.isArray(p.sources)?p.sources:[];
    const originals=findings.filter(f=>!derived(f)), extras=findings.filter(derived), byId=new Map(sources.map(s=>[s.id,s]));
    const counts={}; for(const f of originals) {const k=f.original_status || f.verification_status || 'unknown'; counts[k]=(counts[k]||0)+1;}
    const countsText=Object.entries(counts).map(([k,v])=>`${esc(status[k] || k)}: ${v}`).join(' · ');
    const warning=[...(entry.context?.warnings || []),...(p.limitations || [])];
    const researched = Date.parse(entry.researched_at);
    if (!Number.isFinite(researched) || researched > Date.now() || Date.now()-researched > 6*60*60*1000 || entry.freshness_status !== 'current') warning.unshift('Araştırma eski veya güncelliği bilinmiyor.');
    const risks=originals.filter(f=>['injury','suspension','absence','squad','probable_lineup','official_lineup','lineup'].includes(f.skill_details?.original_category || f.category));
    if(entry.context?.snapshot_missing) warning.unshift('Bağımsız model snapshotı bulunmuyor.');
    if(entry.context?.quote_freshness==='stale') warning.unshift('İş bağlamındaki oranlar eski.');
    const evidence=originals.map(f=>{ const details=f.skill_details || {}, cat=details.original_category || f.category, links=(f.source_ids||[]).map(id=>byId.get(id)).filter(Boolean);
      return `<li><div><b>${esc(category[cat] || cat)}</b> · ${esc(status[f.original_status] || f.verification_status || 'Bilinmiyor')} <span class="muted">(kayıt düzeyi: ${esc(f.verification_status || 'unknown')})</span></div><p>${esc(f.statement)}</p>${f.qualitative_impact ? `<p class="muted">Etki: ${esc(f.qualitative_impact)}</p>`:''}${details.conflict_note ? `<p>Çelişki: ${esc(details.conflict_note)}</p>`:''}${links.length?`<ul class="grok-sources">${links.map(s=>`<li>${sourceLink(s,(Array.isArray(details.source_observations) ? details.source_observations : []).find(o=>o && o.source_id===s.id))}</li>`).join('')}</ul>`:'<p class="muted">Bağlı kaynak yok.</p>'}</li>`; }).join('');
    const extra=extras.map(f=>`<details><summary>${esc(({fixture_verification:'Fikstür',form:'Form',lineups:'Kadro',lineup_surprises:'Kadro sürprizleri',model_prior:'Önceki model bilgisi / itiraz',key_developments:'Önemli gelişmeler',not_found:'Bulunamayanlar'})[f.skill_details.field] || f.skill_details.field)}</summary><pre>${esc(JSON.stringify(f.skill_details.value,null,2))}</pre></details>`).join('');
    const lineup=({predicted:'Muhtemel',official:'Resmî kaynaklı olarak kaydedilmiş',unknown:'Bilinmiyor'})[p.lineup_assessment?.lineup_status] || 'Bilinmiyor';
    return `<div class="grok-meta">${displayDate(entry.researched_at)} · ${esc(entry.mode)} · sürüm ${esc(entry.revision)} · ${originals.length} özgün bulgu · ${sources.length} kaynak</div><p class="muted">Bu araştırma matematiksel modele uygulanmaz. Şema kontrolü, haberlerin doğrulandığı anlamına gelmez; kaynak ve bağımsızlık beyanları ayrıca değerlendirilmelidir.</p><p>Kaydedilen kadro durumu: <b>${esc(lineup)}</b></p>${countsText?`<p class="muted">${countsText}</p>`:''}${risks.length?`<p><b>Risk olarak incelenecek iddialar (${risks.length}):</b> ${risks.map(f=>esc(f.statement)).join(' · ')}</p>`:'<p class="muted">Önemli kadro veya eksiklik iddiası bulunamadı; bu, risk olmadığı anlamına gelmez.</p>'}${warning.length?`<details><summary>Uyarılar ve sınırlar (${warning.length})</summary><ul>${warning.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details>`:''}<details open><summary>Özgün bulgular (${originals.length})</summary><ul class="grok-findings">${evidence || '<li>Bulgu yok.</li>'}</ul></details><details><summary>Skill bağlamından türetilen kayıtlar (${extras.length})</summary>${extra || '<p>Ek bağlam yok.</p>'}</details><details><summary>Tüm kaynaklar (${sources.length})</summary><ul class="grok-sources">${sources.map(s=>`<li>${sourceLink(s)}</li>`).join('')}</ul></details>`;
  }
  async function mount(match, container) {
    const box=document.createElement('section'); box.className='detail grok-intelligence'; box.innerHTML='<details><summary>Grok Intelligence</summary><div class="detail-body"><p class="muted">Araştırma yükleniyor…</p></div></details>'; container.appendChild(box);
    const body=box.querySelector('.detail-body'); const ids=[...new Set([String(match.id || ''), match.iddaa_event_id ? 'iddaa:'+match.iddaa_event_id : ''].filter(Boolean))];
    try {
      let reports=[];
      for (const id of ids) {
        const response=await fetch('/api/grok-intelligence/'+encodeURIComponent(id),{cache:'no-store',credentials:'same-origin'});
        if(!response.ok) { body.innerHTML='<p class="muted">Araştırma şu anda kullanılamıyor.</p>'; return; }
        const data=await response.json(); const found=Array.isArray(data.reports)?data.reports:[];
        for (const entry of found) if(!reports.some(existing=>existing.report_id===entry.report_id && existing.revision===entry.revision)) reports.push(entry);
      }
      reports.sort((a,b)=>String(b.received_at || '').localeCompare(String(a.received_at || '')));
      if(!reports.length) { box.querySelector('summary').textContent='Grok Intelligence · araştırma yok'; body.innerHTML='<p class="muted">Bu maç için araştırma bulunmuyor.</p>'; return; }
      const latest=reports[0], stamp=Date.parse(latest.researched_at);
      box.querySelector('summary').textContent='Grok Intelligence · '+(latest.freshness_status==='current' && Number.isFinite(stamp) && stamp<=Date.now() && Date.now()-stamp<=6*60*60*1000 ? 'araştırma var' : 'eski veya bilinmeyen araştırma');
      body.innerHTML=`<label>Araştırma sürümü <select class="grok-revision">${reports.map((r,i)=>`<option value="${i}">${displayDate(r.researched_at)} · ${esc(r.mode)} · v${esc(r.revision)}</option>`).join('')}</select></label><div class="grok-report"></div>`;
      const select=body.querySelector('select'), target=body.querySelector('.grok-report');
      const render=()=>{target.innerHTML=view(reports[Number(select.value)] || reports[0]);}; select.addEventListener('change',render);render();
    } catch (_) {body.innerHTML='<p class="muted">Araştırma şu anda kullanılamıyor.</p>';}
  }
  const api={mount,view,derived,safeUrl}; if(typeof module!=='undefined') module.exports=api; if(root) root.GrokIntelligence=api;
})(typeof window!=='undefined'?window:null);
