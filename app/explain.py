"""Maç önerisi için herkesin anlayacağı özet: büyük seçim, düz Türkçe etiket, güven seviyesi ve 2-3 cümlelik gerekçe.
Öneri mantığına dokunmaz; `pick`, `factors` ve `iddaa_odds` alanlarını okuyup metne çevirir."""

LEVELS = {"Yüksek": 3, "Orta": 2, "Düşük": 1}
_FALLBACK = "Modelin hesabına göre bu seçenek bu maç için en tutarlı görünüyor."


def headline(market: str) -> str:
    if market in ("1X", "X2", "12"):
        return {"1X": "ÇŞ 1–0", "X2": "ÇŞ 0–2", "12": "ÇŞ 1–2"}[market]
    if market in ("1", "X", "2"):
        return "MS " + ("0" if market == "X" else market)
    if market[:1] in ("Ü", "A") and market[1:2].isdigit():
        return f"{market[1:]} {'Üst' if market[0] == 'Ü' else 'Alt'}"
    return market


def title(market: str, home: str = "", away: str = "") -> str:
    fixed = {
        "1": "Ev sahibi kazanır", "X": "Beraberlik", "2": "Deplasman kazanır",
        "1X": "Ev sahibi kaybetmez", "X2": "Deplasman kaybetmez", "12": "Beraberlik olmaz",
        "KG Var": "İki takım da gol atar", "KG Yok": "En az bir takım gol atamaz",
        "Ev 0.5Ü": "Ev sahibi gol atar", "Dep 0.5Ü": "Deplasman gol atar",
    }
    if market in fixed:
        return fixed[market]
    if market[:1] in ("Ü", "A") and market[1:2].isdigit():
        goals = int(float(market[1:]) + 0.5) if market[0] == "Ü" else int(float(market[1:]))
        return f"{goals} veya daha fazla gol" if market[0] == "Ü" else f"En fazla {goals} gol"
    return market


def _count(form: list[dict], key: str) -> int:
    return sum(1 for g in form if g.get("result") == key)


def _venue(form: list[dict], v: str) -> list[dict]:
    return [g for g in form if g.get("venue") == v]


def _h2h_wins(h2h: list[dict], team: str) -> tuple[int, int]:
    wins = 0
    for h in h2h:
        try:
            a, b = (int(x) for x in h["score"].split("-"))
        except (KeyError, ValueError):
            continue
        wins += (h["home"] == team and a > b) or (h["away"] == team and b > a)
    return wins, len(h2h)


def _form_fact(team: str, form: list[dict]) -> str | None:
    n = len(form)
    if n < 3:
        return None
    w = _count(form, "G")
    if w * 2 >= n:
        return f"{team} son {n} maçta {w} galibiyet aldı."
    return None


def _weak_fact(team: str, form: list[dict], venue: str) -> str | None:
    games = _venue(form, venue)
    lost = sum(1 for g in games if g["result"] == "M")
    where = "deplasmanda" if venue == "D" else "evinde"
    if len(games) >= 2 and lost * 2 > len(games):
        count = "hepsini" if lost == len(games) else f"{lost} tanesini"
        return f"{team} {where} son {len(games)} maçın {count} kaybetti."
    if len(form) >= 3 and _count(form, "G") == 0:
        return f"{team} son {len(form)} maçta hiç kazanamadı."
    return None


def _absence_fact(team: str, news: dict) -> str | None:
    n = (news or {}).get("absences") or 0
    return f"Haberlere göre {team} tarafında {n} oyuncu eksik (sakat ya da cezalı)." if n else None


def _side_facts(market: str, home: str, away: str, f: dict) -> list[str]:
    form_h, form_a = f["form"]["home"], f["form"]["away"]
    news_h, news_a = f["news"]["home"], f["news"]["away"]
    h2h = f.get("h2h") or []
    if market in ("1", "1X"):
        me, opp, form_me, form_opp, v_opp, news_me, news_opp = home, away, form_h, form_a, "D", news_h, news_a
    else:
        me, opp, form_me, form_opp, v_opp, news_me, news_opp = away, home, form_a, form_h, "E", news_a, news_h
    wins, n = _h2h_wins(h2h, me)
    facts = [_form_fact(me, form_me), _weak_fact(opp, form_opp, v_opp), _absence_fact(opp, news_opp)]
    if n >= 2 and wins * 2 >= n:
        facts.append(f"Aralarındaki son {n} maçın {wins} tanesini {me} kazandı.")
    facts = [x for x in facts if x]
    if market in ("1X", "X2"):
        kept = sum(1 for g in form_me if g["result"] != "M")
        if len(form_me) >= 3 and kept * 4 >= len(form_me) * 3:
            facts.insert(0, f"{me} son {len(form_me)} maçın {kept} tanesinde yenilmedi.")
    own = _absence_fact(me, news_me)
    if own:
        facts.append(own.replace("Haberlere göre", "Dikkat: haberlere göre"))
    return facts


def _draw_facts(home: str, away: str, f: dict) -> list[str]:
    facts = []
    gap = abs(f["form"]["home_ppg"] - f["form"]["away_ppg"])
    if f["form"]["home"] and f["form"]["away"] and gap < 0.5:
        facts.append("İki takımın son maçlardaki performansı birbirine çok yakın.")
    h2h = f.get("h2h") or []
    draws = sum(1 for h in h2h if h.get("score", "-").split("-")[0] == h.get("score", "-").split("-")[-1])
    if len(h2h) >= 2 and draws * 3 >= len(h2h):
        facts.append(f"Aralarındaki son {len(h2h)} maçın {draws} tanesi berabere bitti.")
    return facts


def _goal_facts(market: str, home: str, away: str, f: dict) -> list[str]:
    games = f["form"]["home"] + f["form"]["away"]
    if len(games) < 4:
        return []
    avg = sum(g["gf"] + g["ga"] for g in games) / len(games)
    both = sum(1 for g in games if g["gf"] > 0 and g["ga"] > 0)
    facts = []
    if market[0] in ("Ü", "A"):
        facts.append(f"{home} ve {away} son maçlarında maç başına ortalama {str(round(avg, 1)).replace('.', ',')} gol atıldı.")
    else:
        facts.append(f"İki takımın son {len(games)} maçının {both} tanesinde her iki takım da gol attı."
                     if market == "KG Var" else
                     f"İki takımın son {len(games)} maçının {len(games) - both} tanesinde en az bir takım gol atamadı.")
    return facts


def reason(item: dict) -> str:
    pick = item.get("pick")
    f = item.get("factors")
    if not pick or not f:
        return _FALLBACK
    market, home, away = pick["market"], item["home"], item["away"]
    if market in ("1", "2", "1X", "X2"):
        facts = _side_facts(market, home, away, f)
    elif market == "X":
        facts = _draw_facts(home, away, f)
    elif market in ("KG Var", "KG Yok") or market[:1] in ("Ü", "A") and market[1:2].isdigit():
        facts = _goal_facts(market, home, away, f)
    else:  # 12, Ev/Dep 0.5Ü ve bilinmeyen pazarlar: yanlış gerekçe yerine genel cümle
        facts = []
    return " ".join(facts[:3]) if facts else _FALLBACK


def summarize(item: dict) -> dict:
    def summary(market, text, reason_text='', label='', level=0, odd=None):
        return {'market':market,'headline':headline(market) if market else label,
                'title':text,'reason':reason_text,'odds':odd,'level':level,
                'level_label':{'Piyasa adayı':'Piyasa verisi','Model ön tahmini':'Oran bekleniyor','Aday seçenek':'Eşik altında','Oran bandı dışı':'Bant dışı'}.get(label,label),'recommendation_label':label}
    from .match_state import describe
    lifecycle=describe(item)
    phase,price=lifecycle['phase'],lifecycle['price_status']
    if phase=='inactive':
        return summary(None,'Maç ertelendi, durduruldu veya iptal edildi; yeni bahis önerisi üretilmez.',label='Maç aktif değil')
    if phase=='started_result_pending':
        return summary(None,'Başlama saati geçti; canlı durum veya kesin sonuç henüz doğrulanmadı. Gösterilen oranlar maç öncesi kayıttır.',label='Sonuç bekleniyor')
    if phase=='completed':
        h,a=item.get('home_goals'),item.get('away_goals')
        return summary(None,f"{item['home']} {h}–{a} {item['away']}" if h is not None and a is not None else 'Maç tamamlandı; kesin skor bekleniyor.',label='Bitti')
    if price=='removed_from_bulletin':
        return summary(None,'Maç henüz başlamadı ancak son bültende bulunmuyor. Son kayıtlı oranlar güncel fiyat olarak kullanılamaz.',label='Bültende yok')
    if price=='unverified_or_stale' and item.get('quote_provenance') is not None:
        return summary(None,'Maçın oranları henüz güncel kaynaktan doğrulanamadı. Son kayıtlı fiyatlar gösteriliyor.',label='Oran yenileniyor')
    if (item.get('decision') or {}).get('status') == 'no_bet' and not item.get('result_only') and not item.get('is_archived'):
        return summary(None,'Bu maç için uygun öneri yok',
                       'Güncellik, veri tutarlılığı veya model–piyasa ayrışması kontrolünü geçen seçenek bulunamadı.',
                       label='Bahis önerilmiyor')
    if item.get('result_only'):
        h,a=item.get('home_goals'),item.get('away_goals')
        if item.get('completed') and h is not None and a is not None:
            return summary(None,f"{item['home']} {h}–{a} {item['away']}",label='Bitti')
        return summary(None,'Başlama saati geçti; sonuç henüz doğrulanmadı.',label='Sonuç bekleniyor')
    if item.get('is_archived'):
        return summary(None,'Son kaydedilen bülten oranları; güncel bahis fiyatı değildir.',label='Arşiv kaydı')
    pick=item.get('pick')
    alternative=item.get('alternative')
    selected=pick or alternative
    if not selected:
        return summary(None,'Seçim hesabı için yeterli oran veya model verisi bulunmuyor.',label='Veri bekleniyor')
    market=selected['market'];probability=selected.get('p')
    chance=f"Tahmini olasılık %{probability*100:.0f}. " if probability is not None else ''
    odd=selected.get('odds')
    if item.get('small_league') or selected.get('probability_source')=='iddaa_market':
        status=selected.get('candidate_status')
        fallback=selected.get('target_met') is False or status in ('below_threshold','outside_band')
        note=('1,40–2,30 bandı dışında; ' if status=='outside_band'
              else 'Tercih edilen olasılık eşiğinin altında; ' if fallback else '')
        return summary(market,title(market),f"{note}Marjı temizlenmiş iddaa piyasa olasılığı %{probability*100:.0f}; takım modeli güveni değildir.",
                       label='Aday seçenek' if fallback else 'Piyasa adayı',odd=odd)
    if alternative:
        status=alternative.get('candidate_status')
        if status=='odds_missing':
            return summary(market,title(market),chance+'İddaa oranı henüz yok. Bu model ön tahminidir; güncel oran gelene kadar kupona eklenmez.',label='Model ön tahmini')
        if status=='below_threshold':
            return summary(market,title(market),chance+'Güçlü öneri için aranan %56 olasılık eşiğinin altında; yalnız aday olarak gösteriliyor.',label='Aday seçenek',odd=odd)
        return summary(market,title(market),chance+'1,40–2,30 oran bandı dışında; yalnız alternatif olarak gösteriliyor.',label='Oran bandı dışı',odd=odd)
    label=item.get('confidence_label') or 'Düşük'
    explanation=reason(item)
    if pick.get('policy')=='best_available_v3':
        explanation+=' Mevcut güncel seçenekler içindeki seçimdir; tercih edilen olasılık veya oran hedefine ulaşmıyor.'
    if pick.get('policy')=='balanced_v2':
        explanation+=' Seçim, 1,40–2,30 gerçek oran bandındaki seçeneklerden tahmini tutma ihtimali en yüksek olana göre yapılır.'
    return {'market':market,'headline':headline(market),'title':title(market,item['home'],item['away']),
            'odds':(item.get('iddaa_odds') or {}).get(market),'level':LEVELS.get(label,1),
            'level_label':label,'reason':explanation,'recommendation_label':'Model önerisi'}
