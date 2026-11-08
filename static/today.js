// Günlük bülten üst bilgisi; farklı günlerden öneri karıştırmaz.
window.Today = {render() {
  const el=$("#today"), d=state.bulletinData;
  if(!d) {el.innerHTML="";return;}
  const date=new Date(d.day+"T12:00:00Z").toLocaleDateString("tr-TR",{weekday:"long",day:"numeric",month:"long"});
  const coverage=d.coverage;
  const detail=coverage?.detail_requested ? ` · Tam pazar detayı ${coverage.detail_loaded}/${coverage.detail_requested} maç` : "";
  el.innerHTML=`<div class="bulletin-summary"><span class="bulletin-date">${esc(date)}</span><span>${d.total} maç</span><span>${d.groups.length} lig</span><span class="bulletin-freshness">Oranlar: ${d.updated_at ? esc(fmtAgo(d.updated_at)) : "henüz alınmadı"}</span></div>${detail || d.fixture_only ? `<details class="coverage-note"><summary>Veri kapsamı</summary><p>${d.fixture_only || 0} fikstür kaydı${detail}</p></details>` : ""}`;
}};
