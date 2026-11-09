function confidenceReportHtml(report) {
  if (!report) return "";
  const rows = report.groups || [];
  return `<div class="card"><h3>Güvene göre gerçek öneri başarısı</h3><p class="muted">İlk yayımlandığında kaydedilen önerinin sonucu. Lig, güven ve model sürümü ayrı ölçülür. Tutma oranı, kazanç oranı değildir. Az maçta sonuçlar belirsizdir; aralık yaklaşık %95 belirsizliği gösterir.</p>${rows.length ? `<div class="mr-scroll"><table><tr><th>Lig</th><th>Güven</th><th>Tutan / maç</th><th>Gerçek isabet</th><th>Tahmini şans</th><th>%95 aralık</th><th>Sürüm / yöntem</th></tr>${rows.map((r) => `<tr><td>${esc(state.leagues.find((l) => l.id === r.league)?.name || r.league)}</td><td>${esc(r.confidence_label)}</td><td>${r.won} / ${r.n}</td><td>${mrPct(r.hit_rate)}</td><td>${mrPct(r.predicted_probability)}</td><td>${mrPct(r.interval95[0])}–${mrPct(r.interval95[1])}</td><td>${esc(r.version.slice(0, 8))} / ${r.policy === "balanced_v2" ? "Dengeli" : r.policy === "hit_rate_v1" ? "İsabet öncelikli" : esc(r.policy)}</td></tr>`).join("")}</table></div>` : `<p class="muted">Henüz sonuçlanan kayıtlı öneri yok. Yeni maçlar sonuçlandıkça bu tablo dolacak.</p>`}<p class="muted">Güven etiketi kaydedilmemiş ${Number(report.legacy_without_confidence) || 0} eski maç bu ölçüme alınmadı.</p></div>`;
}

function realOddsPerformanceHtml(r) {
  if (!r) return "";
  const summary = r.summary || {};
  const n = Number(summary.n) || 0;
  const fmt = (v) => v == null ? "—" : mrPct(v);
  const interval = summary.interval95;
  const band = interval ? fmt(interval[0]) + "–" + fmt(interval[1]) : "—";
  const segments = r.segments || {};
  const timing = r.price_tracking || {}, comparison = r.market_comparison || {};
  const timingText = timing.n ? `${timing.n} seçimde ilk fiyat / maçtan önceki son saat içinde kayıtlı fiyat farkı: ${fmt(timing.mean_price_difference)}.` : "Maç öncesi son saatten karşılaştırılabilir fiyat kaydı henüz yok.";
  const comparisonText = comparison.n ? `${comparison.n} aynı seçimde Brier: harmanlı tahmin ${comparison.blend_brier}, piyasa ${comparison.market_brier}. Düşük daha iyi.${comparison.reliable ? "" : " Örnek az; başarı sonucu çıkarılamaz."}` : "Piyasa karşılaştırması için eşleşmiş kayıt henüz yok.";
  const bands = (segments.odds_band || []).map(x => `<tr><td>${esc(x.key)}</td><td>${x.n}</td><td>${fmt(x.hit_rate)}</td><td>${fmt(x.realized_roi)}</td></tr>`).join("");
  const rows = (segments.league || []).map((x) => `
    <tr><td class="l">${esc(state.leagues.find(l => l.id === x.key)?.name || x.key)}</td>
    <td>${x.n}</td><td>${fmt(x.hit_rate)}</td><td>${fmt(x.predicted_rate)}</td>
    <td>${fmt(x.realized_roi)}</td><td>${x.reliable ? "30+ kayıt" : "Az veri"}</td></tr>`).join("");
  return `<div class="card"><h3>Gerçek iddaa oranlarıyla yayımlanmış seçim performansı</h3>
    <p class="muted">Yalnız maçtan önce kaydedilmiş, iddaa fiyatı doğrulanmış ilk tahminler.
    Sonuç bilindikten sonra seçim veya oran değiştirilmez. Tek seçime 1 birim varsayımsal stake.</p>
    <div class="mr-kpis">
      <div><div class="muted">Sonuçlanan seçim</div><div class="ch-big">${n}</div></div>
      <div><div class="muted">Gerçek isabet / tahmin</div><div class="ch-big">${fmt(summary.hit_rate)} / ${fmt(summary.predicted_rate)}</div></div>
      <div><div class="muted">Gerçek ROI / tahmini</div><div class="ch-big">${fmt(summary.realized_roi)} / ${fmt(summary.expected_roi)}</div></div>
    </div>
    <p class="muted">%95 Wilson isabet aralığı: ${band}. Brier: ${summary.brier ?? "—"}.
      ${summary.reliable ? "Örnek sayısı ön eşiği geçti; sonuçlar yine belirsizlik içerir." : "30'dan az seçim: performans yorumu için erken."}</p>
    <h4>Oran zamanlaması ve piyasa karşılaştırması</h4><p class="muted">${esc(timingText)} Marj düzeltilmemiş fiyat farkıdır; resmi kapanış oranı veya kârlılık kanıtı değildir.</p><p class="muted">${esc(comparisonText)}</p>
    ${bands ? `<h4>Gerçek oran bandına göre sonuçlar</h4><table><tr><th>Oran</th><th>Seçim</th><th>İsabet</th><th>ROI</th></tr>${bands}</table>` : ""}
    ${rows ? `<div class="mr-scroll"><table>
      <tr><th class="l">Lig</th><th>Seçim</th><th>İsabet</th><th>Model tahmini</th><th>Gerçek ROI</th><th>Örnek</th></tr>
      ${rows}</table></div>` : "<p class='muted'>Henüz gerçek oranlı, sonuçlanan ilk tahmin yok.</p>"}
  </div>`;
}

function prospectiveHtml(forward) {
  if (forward?.variants) {
    const rows = Object.entries(forward.variants).map(([name, v]) => {
      const m = v.metrics;
      return `<tr><td>${esc(name)}</td><td>${m.n || 0}</td><td>${m.brier ?? "—"}</td><td>${m.logloss ?? "—"}</td></tr>`;
    }).join("");
    return `<div class="card"><h3>Yayımlanmış tahminlerin gerçek sonuçları</h3><p class="muted">İlk maç öncesi kayıtlar; üç varyant aynı maçlarda karşılaştırılır. Kayıtlı tahmin: ${Number(forward.stored) || 0}. ${forward.evaluation_status === "insufficient_data" ? "Kalibrasyon için yeterli kayıt yok; doğruluk artışı gösterilmiş değil." : "Örnek sayısı yeterli olmak tek başına kalibrasyon veya başarı kanıtı değildir."} Sonuçlar veri biriktikçe oluşur. Yukarıdaki geçmiş testten ayrıdır.</p><table><tr><th>Varyant</th><th>Maç</th><th>Brier</th><th>Log-loss</th></tr>${rows}</table></div>` + confidenceReportHtml(forward.recommendations);
  }
  return "";
}

function strongestPicksHtml(report) {
  const s = report?.summary || {}, n = Number(s.n) || 0;
  if (!n) return "";
  return `<div class="card"><h3>En yüksek olasılık seçeneği: kaydedilmiş sonuçlar</h3>
    <p class="muted">${n} sonuçlanan seçim · gerçek isabet ${mrPct(s.hit_rate)} · tahmin edilen olasılık ${mrPct(s.predicted_rate)}. Bu seçimler dengeli önerilerle aynı maçlarda örtüşebilir; iki grubu toplamayın.</p></div>`;
}

function ensembleReportHtml(selection) {
  const entries = Object.entries(selection || {});
  if (!entries.length) return "";
  const rows = entries.map(([league, data]) => {
    const weight = Number(data.fitted_weight) || 0;
    const validation = data.validation || {};
    const selectedKey = weight === 1 ? "1.0" : weight === .5 ? "0.5" : null;
    const evidence = selectedKey ? validation[selectedKey] : Object.values(validation)
      .filter(v => Number.isFinite(v.delta_brier)).sort((a,b) => a.delta_brier-b.delta_brier)[0];
    const name = state.leagues.find(l => l.id === league)?.name || league;
    const model = weight === 0 ? "Mevcut" : weight === .5 ? "%50 harman" : "Öğrenilmiş gol modeli";
    const delta = evidence?.delta_brier;
    const deltaLabel = delta == null ? "—" : `${selectedKey ? "Seçilen" : "Aday"} ${delta >= 0 ? "+" : "−"}${Math.abs(delta).toFixed(4)}`;
    return `<tr><td class="l">${esc(name)}</td><td>${model}</td><td>${evidence?.n ?? 0}</td><td>${deltaLabel}</td></tr>`;
  }).join("");
  return `<div class="card"><h3>Motor karşılaştırması</h3>
    <p class="muted">Yeni model yalnız geçmiş doğrulama desteklerse devreye girer; geçmiş test canlı başarı garantisi değildir.</p>
    <div class="mr-scroll"><table><tr><th class="l">Lig</th><th>Seçili motor</th><th>Doğrulama maçı</th><th>Brier farkı</th></tr>${rows}</table></div>
    <p class="muted small">Mevcut motor tutulduysa fark, en iyi adayın doğrulama farkıdır.</p></div>`;
}

async function backtestControl(box) {
  try {
    const me = await api("/api/auth/me");
    if (me.user?.role !== "admin") return;
    const data = await api("/api/backtest");
    const card = document.createElement("div");
    card.className = "card";
    const text = document.createElement("p"), button = document.createElement("button");
    text.textContent = "Geçmiş test durumu: " + (data.job?.status || "idle");
    button.className = "btn";
    button.textContent = "Geçmiş testi çalıştır";
    button.disabled = data.running;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await api("/api/backtest", {method: "POST"});
        text.textContent = "İş kuyruğa alındı. İşleyici yaklaşık 15 dakikada bir kontrol eder; sayfayı daha sonra yenileyebilirsiniz.";
      } catch (e) { text.textContent = "İş başlatılamadı: " + e.message; button.disabled = false; }
    });
    card.append(text, button);
    box.appendChild(card);
  } catch (_) { /* oturum veya iş durumu okunamazsa karne yine görünür */ }
}

// "Karne" sekmesi: /api/model-report (cron önbelleği). app.js yardımcıları: $, esc, api, detail.
const mrPct = (v, d = 1) => (v == null ? "–" : (v * 100).toFixed(d) + "%");
const mrDiff = (v) => (v == null ? "–" : `<span class="${v >= 0 ? "ch-win" : "ch-loss"}">${v >= 0 ? "+" : "−"}${Math.abs(v * 100).toFixed(1)} puan</span>`);

function mrTrendSvg(days) {
  const W = 320, H = 120, P = 24;
  if (days.length < 2) return `<p class="muted">Son 30 günde yeterli maç yok.</p>`;
  const x = (i) => P + (i / (days.length - 1)) * (W - P - 8), y = (v) => H - 18 - v * (H - 30);
  const line = days.map((d, i) => `${x(i)},${y(d.accuracy)}`).join(" ");
  const grid = [0, 0.5, 1].map((t) => `<line x1="${P}" y1="${y(t)}" x2="${W - 8}" y2="${y(t)}" stroke="var(--line)"/><text x="${P - 4}" y="${y(t) + 3}" text-anchor="end" font-size="9" fill="var(--muted)">${t * 100}</text>`).join("");
  const dots = days.map((d, i) => `<circle cx="${x(i)}" cy="${y(d.accuracy)}" r="2.5" fill="var(--accent2)"><title>${d.day}: ${mrPct(d.accuracy)} (${d.n} maç)</title></circle>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="mr-chart" role="img" aria-label="Günlük isabet">${grid}<polyline points="${line}" fill="none" stroke="var(--accent2)" stroke-width="1.5"/>${dots}<text x="${P}" y="${H - 4}" font-size="9" fill="var(--muted)">${days[0].day.slice(5)}</text><text x="${W - 8}" y="${H - 4}" text-anchor="end" font-size="9" fill="var(--muted)">${days[days.length - 1].day.slice(5)}</text></svg>`;
}

function mrCouponRow(label, c) {
  if (!c) return "";
  return `<tr><td class="l">${label}</td><td>${c.won}/${c.won + c.lost}</td><td>${mrPct(c.hit_rate, 0)}</td></tr>`;
}

async function renderModelReport() {
  const box = $("#modelReport");
  box.innerHTML = Ui.skeleton(2);
  let r;
  try { r = await api("/api/model-report"); } catch (e) { box.innerHTML = Ui.error(e.message); return; }
  if (!r.computed_at || !r.overall.n) { box.innerHTML = realOddsPerformanceHtml(r.prospective?.real_odds_picks) + strongestPicksHtml(r.prospective?.strongest_picks) + ensembleReportHtml(r.ensemble_selection) + prospectiveHtml(r.prospective) + Ui.empty("Geçmiş karne henüz hazır değil", "Veriler güncellendikten sonra burada görünecek."); await backtestControl(box); return; }
  const o = r.overall, c = r.coupons;
  const bankoRate = c && c.banko && (c.banko.won + c.banko.lost) ? mrPct(c.banko.hit_rate, 0) : "–";
  box.innerHTML = realOddsPerformanceHtml(r.prospective?.real_odds_picks) + strongestPicksHtml(r.prospective?.strongest_picks) + ensembleReportHtml(r.ensemble_selection) + `
    <h2 class="sec-title">Karne</h2>
    <p class="muted">Geçmiş 1X2 testi: en olası maç sonucu, yalnızca maçtan önceki sonuç verileriyle yeniden kurulur. Bu, yayımlanan önerilerin isabet oranı değildir.</p>
    <div class="mr-kpis">
      <div class="card"><div class="muted">Geçmiş 1X2 testi</div><div class="ch-big">${mrPct(o.accuracy, 0)}</div><div class="muted">${o.n.toLocaleString("tr-TR")} maç · öneri isabeti değildir</div></div>
      <div class="card"><div class="muted">Rastgele tahmine göre</div><div class="ch-big">${mrDiff(o.vs_random)}</div><div class="muted">Rastgele tahmin yaklaşık %33 tutar</div></div>
      <div class="card"><div class="muted">Banko kupon tutma oranı</div><div class="ch-big">${bankoRate}</div><div class="muted">Sonuçlanan kuponlar</div></div>
    </div>
    <div class="card"><h3>Geçmiş 1X2 testi · son 30 gün</h3>${mrTrendSvg(r.trend)}<p class="muted">Günlük en olası maç sonucu isabeti · ${r.trend.reduce((s, d) => s + d.n, 0)} maç; yayımlanan öneri isabeti değildir.</p></div>
    ${detail("Detaylar", `<h4>Liglere göre</h4><div class="mr-scroll"><table><tr><th class="l">Lig</th><th>Maç</th><th>Doğru tahmin</th></tr>
      ${r.leagues.map((l) => `<tr><td class="l">${esc(l.name)}</td><td>${l.n}</td><td>${mrPct(l.accuracy, 0)}</td></tr>`).join("")}</table></div>
      ${c ? `<h4>Kuponlar</h4><table><tr><th class="l">Kupon</th><th>Tutan</th><th>Tutma</th></tr>${mrCouponRow("Banko", c.banko)}${mrCouponRow("Sürpriz", c.surprise)}${mrCouponRow("Toplam", c.total)}</table>` : ""}`)}
    <p class="muted small">Güncelleme: ${new Date(r.computed_at).toLocaleString("tr-TR")}. Tahminler garanti değildir.</p>`;
  box.insertAdjacentHTML("beforeend", prospectiveHtml(r.prospective));
  await backtestControl(box);

}
