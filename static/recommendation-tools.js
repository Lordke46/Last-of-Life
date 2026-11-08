/* Match-only comparison; no third-party requests and no probability adjustment. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.RecommendationTools = api;
})(globalThis, function () {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const percentage = value => typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value * 100)}%` : '—';
  const number = value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '—';
  function render(match, button = () => '', headline = value => value) {
    const tools = match.recommendation_tools;
    if (!tools) return '';
    const rows = (tools.by_probability || []).map(item => {
      const move = item.movement;
      const signals = [item.model_market_disagree ? 'Model ve piyasa ayrışıyor' : '',
        !item.actionable ? 'İnceleme amaçlı; öneri değil' : '',
        !item.meets_probability_threshold ? 'Öneri eşiği altında' : '',
        move ? `İlk kayıt ${number(move.first_observed)} → ${number(move.current)} (${move.change_pct > 0 ? '+' : ''}${move.change_pct}%)` : ''].filter(Boolean);
      return `<tr><td class="l" data-field="choice"><b>${esc(headline(item.market))}</b>${signals.length ? `<small class="comparison-signals">${signals.map(esc).join(' · ')}</small>` : ''}</td>
        <td data-field="probability" data-label="Şans">${percentage(item.p)}<small class="comparison-signals">Model ${percentage(item.model_p)} / Piyasa ${percentage(item.market_p)}</small></td>
        <td data-field="odds" data-label="İddaa / adil"><b>${number(item.odds)}</b><small class="comparison-signals">Adil ${number(item.fair_odds)}</small></td>
        <td data-field="value" data-label="Fiyat farkı">${item.estimated_value >= 0 ? '+' : ''}${(item.estimated_value * 100).toFixed(1)}%</td>
        <td data-field="action">${item.actionable ? button(match, item.market) : ''}</td></tr>`;
    }).join('');
    const value = tools.by_value?.[0];
    return `<section class="card recommendation-comparison" aria-label="Seçenek karşılaştırması"><h3>Bu maçta hangi seçenek?</h3><p class="muted small">${tools.preferred_odds.map(number).join('–')} gerçek oran aralığında tutma ihtimaline göre ilk üç seçenek.</p>
      ${(tools.warnings || []).map(warning => `<p class="muted small">${esc(warning)}</p>`).join('')}
      ${rows ? `<table><thead><tr><th class="l">Seçenek</th><th>Tahmini şans</th><th>İddaa / adil</th><th>Fiyat farkı</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="muted">Bu oran aralığında karşılaştırılabilecek gerçek fiyatlı seçenek yok.</p>'}
      ${value && value.actionable && value.estimated_value > 0 ? `<p class="muted small">En yüksek tahmini fiyat avantajı: ${esc(headline(value.market))} (${(value.estimated_value * 100).toFixed(1)}%). Fiyat avantajı, tutma ihtimalinden ayrı hesaplanır.</p>` : ''}
      <p class="muted small">${esc(tools.note)}</p></section>`;
  }
  return {render};
});
