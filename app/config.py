"""Uygulama ayarları. Hepsi lokal, API anahtarı gerekmez."""
import os
import re
import unicodedata
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path):
    """Repo dışına sızmayan yerel .env dosyasını okur; gerçek ortam değişkeni her zaman önceliklidir."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv(BASE_DIR / ".env")

# Doluysa ortak Supabase Postgres (transaction pooler, port 6543); boşsa yerel SQLite dosyası.
DATABASE_URL = os.environ.get("DATABASE_URL") or None
DB_PATH = Path(os.environ.get("LOCAL_DB_PATH") or BASE_DIR / "data" / "iddia.db")
STATIC_DIR = BASE_DIR / "static"

# Lig anahtarları ESPN fikstür sayfalarındaki kısaltmalardır. Ek alanlar:
#   source: "espn" (varsayılan) | "tff" (ESPN'de verisi olmayan lig; resmi TFF sitesinden)
#   kind:   "league" (varsayılan) | "cup" (ligler arası takımların oynadığı Avrupa kupası)
LEAGUES = {
    "tur.1": {"name": "Süper Lig", "country": "Türkiye", "lang": "tr", "slug": "super-lig"},
    "eng.1": {"name": "Premier League", "country": "İngiltere", "lang": "en", "slug": "premier-league"},
    "esp.1": {"name": "La Liga", "country": "İspanya", "lang": "en", "slug": "la-liga"},
    "ita.1": {"name": "Serie A", "country": "İtalya", "lang": "en", "slug": "serie-a"},
    "ger.1": {"name": "Bundesliga", "country": "Almanya", "lang": "en", "slug": "bundesliga"},
    "fra.1": {"name": "Ligue 1", "country": "Fransa", "lang": "en", "slug": "ligue-1"},
    "tur.2": {"name": "TFF 1. Lig", "country": "Türkiye", "lang": "tr", "slug": "tff-1-lig", "source": "tff"},
    "ned.1": {"name": "Eredivisie", "country": "Hollanda", "lang": "en", "slug": "eredivisie"},
    "por.1": {"name": "Liga Portugal", "country": "Portekiz", "lang": "en", "slug": "liga-portugal",
              "aliases": ("Portugal", "Primeira Liga", "Portuguese Primeira Liga", "Liga Portugal Betclic")},
    "uefa.champions": {"name": "UEFA Şampiyonlar Ligi", "country": "Avrupa", "lang": "en",
                        "slug": "uefa-champions-league", "kind": "cup"},
    "uefa.europa": {"name": "UEFA Avrupa Ligi", "country": "Avrupa", "lang": "en",
                    "slug": "uefa-europa-league", "kind": "cup"},
}


def is_cup(league: str) -> bool:
    return LEAGUES.get(league, {}).get("kind") == "cup"


def _league_token(value: str) -> str:
    """Normalize an ID, slug, display name, or configured alias for comparison."""
    ascii_value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
    return "".join(char for char in ascii_value if char.isalnum())


def league_slug(league: str) -> str:
    """Return the stable URL-style slug for a configured league."""
    config = LEAGUES.get(league)
    if not config:
        return ""
    if config.get("slug"):
        return config["slug"]
    ascii_name = unicodedata.normalize("NFKD", config["name"]).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")


def league_aliases(league: str) -> tuple[str, ...]:
    """Return all configured external identifiers that resolve to one canonical ID."""
    config = LEAGUES.get(league)
    if not config:
        return ()
    values = (league, league_slug(league), config["name"], *config.get("aliases", ()))
    return tuple(dict.fromkeys(value for value in values if value))


def resolve_league(value: str | None) -> str | None:
    """Map a league ID, URL slug, display name, or configured alias to its canonical ID."""
    if value is None:
        return None
    token = _league_token(value)
    if not token:
        return None
    matches = [league for league in LEAGUES
               if any(_league_token(alias) == token for alias in league_aliases(league))]
    return matches[0] if len(matches) == 1 else None

# Geçmiş veri bu tarihten itibaren çekilir (2021-22 sezonundan beri; Elo ısınması, H2H ve
# sezonlar arası güç hesapları için). Daha eski tarihe çekilirse eksik kısım bir sonraki
# güncellemede otomatik doldurulur.
HISTORY_START = date(2021, 7, 1)
# Bu sürümden önce veritabanının kapsadığı ilk gün (eski kurulumlarda geriye doldurma sınırı)
LEGACY_HISTORY_START = date(2025, 7, 15)
# Bugünden kaç gün sonrasına kadar fikstür çekilsin
FIXTURE_DAYS_AHEAD = 21

# Not: tam tarayıcı UA dizesi ESPN tarafında bot kontrolüne (HTTP 202, boş gövde) takılıyor
USER_AGENT = "Mozilla/5.0"
REQUEST_DELAY_SEC = 0.4  # kaynak sitelere nazik olmak için

# Model parametreleri
MODEL = {
    "elo_k": 20.0,
    "elo_home_adv": 60.0,
    "elo_new_team": 1450.0,
    "elo_season_regress": 0.25,  # sezon arasında ortalamaya çekme oranı
    "half_life_days": 200.0,  # Poisson güçlerinde zaman ağırlığı yarı ömrü
    "prior_matches": 6.0,  # Poisson güçlerinde lig ortalamasına büzülme
    "elo_weight": 0.7,  # Poisson ile Elo tahminini harmanlama ağırlığı
    "elo_goals_per_point": 0.0045,  # 100 Elo puanı ≈ 0.45 gol üstünlük
    "form_coef": 0.06,  # form farkının log(λ) etkisi
    "rest_coef": 0.03,  # yorgunluk (<4 gün dinlenme) cezası
    "dc_rho": -0.06,  # Dixon-Coles düşük skor düzeltmesi
    "first_half_goal_share": 0.45,  # toplam xG'nin ilk yarıya ayrılan payı
    "margin": 0.05,  # önerilen orana eklenen kâr marjı (%5)
    # iddaa.com oranlarından marjı temizlenmiş olasılıkların harmanlama ağırlığı: 0 = yalnız model,
    # 1 = yalnız piyasa. Oran yoksa model olasılığı aynen kalır. İlk 100 maç öncesi snapshot'ta
    # Brier piyasa 0,496 < 50/50 harman 0,518 < model 0,547 ölçüldü (docs/benchmarks); bu yüzden
    # varsayılan piyasa ağırlıklıdır, model yalnız eşit fiyatlı seçimler arasında yön verir.
    # adaptive_market yeterli kanıt biriktiğinde pazar grubuna göre bu değeri geçersiz kılar.
    "market_weight": 0.75,
    "disagreement_review_pp": 20.0,  # İnceleme eşiği; optimize edildiği iddia edilmez.
}

NEWS_LOOKBACK_DAYS = 7
