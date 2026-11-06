"""Yayımlanan tahminin değişmez, maç öncesi kaydı ve ileriye dönük ölçümü."""
import hashlib
import json
import math
from datetime import datetime, timezone
from functools import lru_cache
from ..config import BASE_DIR, MODEL
from ..db import get_meta, set_meta

SCHEMA = '''
CREATE TABLE IF NOT EXISTS prediction_snapshots (
    match_id TEXT PRIMARY KEY,
    league TEXT NOT NULL,
    kickoff TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    model_version TEXT NOT NULL,
    data_version TEXT,
    parameters TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prediction_snapshots_league ON prediction_snapshots(league, captured_at);
'''


def now():
    return datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def code_version():
    digest = hashlib.sha256()
    for name in ('config.py', 'news_intel.py', 'model/engine.py', 'model/ratings.py', 'model/predict.py',
                 'model/european.py', 'model/blend.py', 'model/extra_markets.py'):
        digest.update((BASE_DIR / 'app' / name).read_bytes())
    return digest.hexdigest()[:16]


def record(conn, predictions):
    at = now()
    parameters = json.dumps(MODEL, sort_keys=True)
    version = hashlib.sha256((code_version() + parameters).encode()).hexdigest()[:16]
    data_version = '|'.join(get_meta(conn, k, '') or '' for k in ('last_updated_at', 'iddaa_last_update', 'update_data_version'))
    rows = []
    for p in predictions:
        if p.get('small_league') or not p.get('markets') or datetime.fromisoformat(p['kickoff'].replace('Z', '+00:00')) <= at:
            continue
        markets = p['markets']
        payload = {'model': {k: v.get('model_p', v.get('p')) for k, v in markets.items()},
                   'blend': {k: v.get('p') for k, v in markets.items()},
                   'market': {k: v.get('market_p') for k, v in markets.items()},
                   'pick': p.get('pick'), 'confidence_label': p.get('confidence_label'),
                   'confidence': p.get('confidence'),
                   'odds': p.get('iddaa_odds', {}), 'news': p.get('factors', {}).get('news'),
                   'no_news': p.get('no_news_markets'),
                   'blend_settings': p.get('blend'), 'extra_markets': p.get('extra_markets', {})}
        rows.append((str(p['id']), p['league'], p['kickoff'], at.isoformat(), version, data_version, parameters, json.dumps(payload)))
    conn.executemany('INSERT INTO prediction_snapshots(match_id,league,kickoff,captured_at,model_version,data_version,parameters,payload) '
                     'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(match_id) DO NOTHING', rows)
    return len(rows)


def record_safely(conn, predictions):
    conn.execute('SAVEPOINT prediction_capture')
    try:
        record(conn, predictions)
        conn.execute('RELEASE SAVEPOINT prediction_capture')
        set_meta(conn, 'prediction_capture_error', '')
    except Exception as exc:
        conn.execute('ROLLBACK TO SAVEPOINT prediction_capture')
        conn.execute('RELEASE SAVEPOINT prediction_capture')
        set_meta(conn, 'prediction_capture_error', type(exc).__name__)


def news_impact_report(paired):
    """Aynı maçta haberli-habersiz 1X2 tahmin farkının güven aralığı.

    Delta = Brier(haberli) - Brier(habersiz). Negatif haberlerin lehine.
    Olasılık tahmininin iyileştiğine dair işaret için >=100 *etkilenen*
    eşleşmiş maç ve sıfırı dışlayan belirsizlik aralığı gerekir.
    """
    n = len(paired)
    if not n:
        return {"n": 0, "verdict": "insufficient_data", "min_samples": 100}
    diffs = []
    for news, control, actual in paired:
        score = lambda probs: sum((p - (i == actual)) ** 2 for i, p in enumerate(probs))
        diffs.append(score(news) - score(control))
    avg = sum(diffs) / n
    variance = sum((x - avg) ** 2 for x in diffs) / (n - 1) if n > 1 else 0
    radius = 1.96 * math.sqrt(variance / n)
    low, high = avg - radius, avg + radius
    verdict = ("insufficient_data" if n < 100 else
               "news_improves" if high < 0 else
               "news_hurts" if low > 0 else "inconclusive")
    return {"n": n, "delta_brier": round(avg, 5),
            "interval95": [round(low, 5), round(high, 5)],
            "verdict": verdict, "min_samples": 100}


def report(conn):
    from .report import metrics, calibration
    from .blend import GROUPS
    from .settlement import outcome
    rows = conn.execute('SELECT s.*,m.home_goals,m.away_goals,m.kickoff AS actual_kickoff '
                        'FROM prediction_snapshots s JOIN matches m ON m.id=s.match_id WHERE m.completed=1').fetchall()
    samples = {k: [] for k in ('model', 'blend', 'market')}
    news_pairs = {'with_news': [], 'without_news': []}
    affected_pairs = []
    market_scores = {}
    versions = set()
    recommendation_groups = {}
    legacy = 0
    for r in rows:
        captured = datetime.fromisoformat(r['captured_at'].replace('Z', '+00:00'))
        if captured >= datetime.fromisoformat(r['actual_kickoff'].replace('Z', '+00:00')):
            continue
        hg, ag = r['home_goals'], r['away_goals']
        if hg is None or ag is None:
            continue
        payload = json.loads(r['payload'])
        versions.add(r['model_version'])
        pick = payload.get('pick') or {}
        label = payload.get('confidence_label')
        hit = outcome(pick.get('market'), hg, ag)
        probability = pick.get('p')
        if (hit is not None and label in ('Yüksek', 'Orta', 'Düşük')
                and isinstance(probability, (int, float)) and not isinstance(probability, bool)
                and math.isfinite(probability) and 0 <= probability <= 1):
            # Sürüm ve politika karışmasın: eski tahmin yeni kuralla yeniden seçilmez.
            key = (r['league'], label, r['model_version'], pick.get('policy', 'legacy'))
            group = recommendation_groups.setdefault(key, {'n': 0, 'won': 0, 'probability_sum': 0.0})
            group['n'] += 1
            group['won'] += int(hit)
            group['probability_sum'] += probability
        elif 'confidence_label' not in payload:
            legacy += 1
        actual = 0 if hg > ag else 1 if hg == ag else 2
        no_news = payload.get('no_news') or {}
        with_news = payload.get('model') or {}
        if all(isinstance(v.get(k), (int, float)) for v in (with_news, no_news)
               for k in ('1', 'X', '2')):
            for key, values in (('with_news', with_news), ('without_news', no_news)):
                news_pairs[key].append({'day': r['kickoff'][:10],
                                        'probs': [values[k] for k in ('1', 'X', '2')],
                                        'actual': actual})
            # Haberin etkisi olmayan maçlar (iki tahmin bire bir aynı)
            # etki hesabını sulandırmasın; sadece gerçek karşıt tahmin farkı.
            actual_news = [with_news[k] for k in ('1', 'X', '2')]
            baseline = [no_news[k] for k in ('1', 'X', '2')]
            if any(abs(a - b) > 1e-6 for a, b in zip(actual_news, baseline)):
                affected_pairs.append((actual_news, baseline, actual))
        # Aynı maç örnekleminde üç varyantın karşılaştırması.
        if all(payload[variant].get(k) is not None for variant in samples for k in ('1', 'X', '2')):
            for variant in samples:
                probs = [payload[variant][k] for k in ('1', 'X', '2')]
                samples[variant].append({'day': r['kickoff'][:10], 'probs': probs, 'actual': actual})
        for market, probability in payload['blend'].items():
            won = outcome(market, hg, ag)
            if probability is None or won is None:
                continue
            score = market_scores.setdefault(market, {'n': 0, 'brier_sum': 0.0})
            score['n'] += 1
            score['brier_sum'] += (probability - int(won)) ** 2
    return {'scope': 'first_published_prematch', 'versions': sorted(versions),
            'variants': {v: {'metrics': metrics(s), 'calibration': calibration(s)} for v, s in samples.items()},
            'markets': {k: {'n': v['n'], 'brier': round(v['brier_sum'] / v['n'], 4)} for k, v in market_scores.items()},
            'recommendations': {'groups': recommendation_rows(recommendation_groups), 'legacy_without_confidence': legacy},
            'news_ab': {'scope': 'paired_prematch', 'with_news': metrics(news_pairs['with_news']),
                        'without_news': metrics(news_pairs['without_news']),
                        'comparable_matches': len(news_pairs['with_news']),
                        'affected_only': news_impact_report(affected_pairs),
                        'unaffected_matches': len(news_pairs['with_news']) - len(affected_pairs)},
            'stored': conn.execute('SELECT COUNT(*) n FROM prediction_snapshots').fetchone()['n']}


def recommendation_rows(groups):
    """Gözlenen isabet ve %95 Wilson aralığı; az örneklem açıkça görünür."""
    rows = []
    for (league, label, version, policy), group in sorted(groups.items()):
        n, won = group['n'], group['won']
        rate, z = won / n, 1.96
        scale = 1 + z*z/n
        center = (rate + z*z/(2*n))/scale
        radius = z * math.sqrt(rate*(1-rate)/n + z*z/(4*n*n))/scale
        rows.append({'league': league, 'confidence_label': label, 'version': version, 'policy': policy,
                     'n': n, 'won': won, 'hit_rate': round(rate, 4),
                     'predicted_probability': round(group['probability_sum']/n, 4),
                     'interval95': [round(max(0, center-radius), 4), round(min(1, center+radius), 4)]})
    return rows
