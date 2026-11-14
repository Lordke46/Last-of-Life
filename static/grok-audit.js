/* Grok kupon denetimi: yönetici, Grok'un (abonelik, API yok) verdiği JSON kararı buraya yapıştırır.
   Grok yorumu olasılıkları değiştirmez; veto'lu bacak için ayrık alternatif gösterilir. */
(function (root) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const KINDS = [['daily', 'Günlük denetim'], ['lineup', 'Kadro kontrolü (son 150 dk)'], ['weekly', 'Haftalık değerlendirme']];
  let adminChecked = null;

  async function isAdmin() {
    if (adminChecked === null) {
      try { adminChecked = (await (await fetch('/api/auth/me')).json()).user?.role === 'admin'; } catch (_) { adminChecked = false; }
    }
    return adminChecked;
  }

  function briefUrl(day, kind) { return `/api/coupons/brief?day=${encodeURIComponent(day)}&kind=${encodeURIComponent(kind)}`; }

  async function mountControl(container, day, onSaved) {
    if (!container || !day || !(await isAdmin())) return null;
    const box = document.createElement('details');
    box.className = 'detail grok-audit-control';
    box.innerHTML = `<summary>🧠 Grok denetimi gir (yönetici)</summary><div class="detail-body">
      <p class="muted">1) Brief'i Grok'a ver (zamanlanmış görev bağlantıyı kendisi okur ya da metni yapıştır). 2) Grok'un döndürdüğü JSON'u aşağıya yapıştır ve kaydet.</p>
      <div class="grok-audit-row"><label>Tür <select data-kind>${KINDS.map(([k, label]) => `<option value="${k}">${esc(label)}</option>`).join('')}</select></label>
        <a class="btn ghost" data-brief-link href="${briefUrl(day, 'daily')}" target="_blank" rel="noopener">Brief'i aç</a>
        <button type="button" class="btn ghost" data-copy-brief>Brief'i kopyala</button></div>
      <textarea data-raw rows="8" placeholder='{"schema":"coupon-audit-v1", ...}' spellcheck="false"></textarea>
      <div class="grok-audit-row"><button type="button" class="btn" data-save>Kaydet</button><span class="muted" data-status></span></div></div>`;
    container.appendChild(box);
    const kindSel = box.querySelector('[data-kind]'), link = box.querySelector('[data-brief-link]'),
      raw = box.querySelector('[data-raw]'), status = box.querySelector('[data-status]');
    kindSel.addEventListener('change', () => { link.href = briefUrl(day, kindSel.value); });
    box.querySelector('[data-copy-brief]').addEventListener('click', async () => {
      status.textContent = 'Brief alınıyor…';
      try {
        const text = await (await fetch(briefUrl(day, kindSel.value), { cache: 'no-store' })).text();
        await navigator.clipboard.writeText(text);
        status.textContent = `Brief panoya kopyalandı (${text.length} karakter).`;
      } catch (e) { status.textContent = 'Kopyalanamadı: ' + (e?.message || e); }
    });
    box.querySelector('[data-save]').addEventListener('click', async () => {
      if (!raw.value.trim()) { status.textContent = 'Önce Grok yanıtını yapıştır.'; return; }
      status.textContent = 'Kaydediliyor…';
      try {
        const r = await fetch('/api/coupons/grok-audit', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ day, kind: kindSel.value, raw: raw.value }) });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${r.status}`);
        const swapped = (body.swaps || []).filter(s => s.coupon_id).length;
        status.textContent = `Kaydedildi: ${body.verdicts} karar · ${body.vetoes} veto · ${body.cautions} dikkat` +
          (swapped ? ` · ${swapped} kupon alternatife geçti` : '') +
          ({ sent: ' · Telegram gönderildi', already_sent: ' · Telegram daha önce gönderilmiş', telegram_not_configured: ' · Telegram sunucuda tanımlı değil (zamanlayıcı gönderir)', failed: ' · Telegram gönderilemedi' }[body.telegram] || '');
        raw.value = '';
        if (typeof onSaved === 'function') onSaved(body);
      } catch (e) { status.textContent = 'Kaydedilemedi: ' + (e?.message || e); }
    });
    return box;
  }

  const api = { mountControl, briefUrl, isAdmin };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.GrokAudit = api;
})(typeof window !== 'undefined' ? window : globalThis);
