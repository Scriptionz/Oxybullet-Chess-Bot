import time
import random
import itertools
import os
import requests
import json
from datetime import datetime, timedelta

# ==========================================================
# ⚙️ AYARLAR
# ==========================================================
SETTINGS = {
    "RATED_MODE":            True,
    "MAX_PARALLEL_GAMES":    3,
    "SAFETY_LOCK_TIME":      45,
    "STOP_FILE":             "STOP.txt",
    "POOL_REFRESH_SECONDS":  900,
    "BLACKLIST_MINUTES":     60,
    "CHESS960_CHANCE":       0.10,

    # Turnuva
    "AUTO_TOURNAMENT":       True,
    "JOIN_UPCOMING_MINS":    15,
    "ONLY_BOT_TOURNEYS":     True,
    "TOURNAMENT_COOLDOWN":   600,

    # Zaman kontrolleri (saniye)
    "TC_ALL":    ["30", "60", "60+1", "120+1", "180", "180+2",
                  "300", "300+3", "600", "600+5", "900+10", "1800"],
    "TC_MAX_10": ["30", "60", "60+1", "120+1", "180", "180+2",
                  "300", "300+3", "600"],

    # Tier puan aralıkları
    "TIER_ELITE": (2700, 4000),
    "TIER_HIGH":  (2300, 2700),
    "TIER_MID":   (2000, 2300),
    "TIER_LOW":   (1500, 2000),

    # Kümülatif eşikler: Low %10 | Mid %23 | High %35 | Elite %32
    "TIER_THRESHOLDS": {
        "LOW":  0.10,
        "MID":  0.33,
        "HIGH": 0.68,
    },

    # Koruma mekanizmaları
    "LOSING_STREAK_LIMIT":   3,
    "RATING_DROP_THRESHOLD": 50,
    "PROTECTION_GAME_COUNT": 10,

    # Kalıcı kara liste — bu botlara asla meydan okuma gönderilmez,
    # gelen meydan okumaları da reddedilir. (hepsi küçük harf olmalı)
    "PERMANENT_BLACKLIST": {
        "waychess-bot",  # Botu gereksiz yere meşgul ediyor
    },
}

# ==========================================================
# PROTOKOL
# ==========================================================
# İNSANLAR : 1500+, puansız, 0.5+0→30+0, Standart+Chess960
# BOTLAR    : <1500 reddet
#             1500-2000 → puansız, max 10+0
#             2000-2300 → puanlı,  max 10+0
#             2300+     → puanlı,  her format
# MATCHMAKER: Elite %32 | High %35 | Mid %23 | Low %10
# KORUMA    : 3 üst üste kayıp veya 50 puan düşüş → Mid kilitli (10 maç)
# ==========================================================

def _parse_tc(tc_str):
    if '+' in tc_str:
        p = tc_str.split('+')
        return int(p[0]), int(p[1])
    return int(tc_str), 0


# Tier adı yardımcısı (TIER_NAMES dead code'u kaldırıldı, tek yerde tanımlı)
_TIER_NAME = {
    (2700, 4000): "Elite",
    (2300, 2700): "High",
    (2000, 2300): "Mid",
    (1500, 2000): "Low",
}


class RatingTracker:
    """Kötü seri ve puan düşüşü takibi."""

    def __init__(self):
        self.baseline = {
            'bullet': 2931, 'blitz': 2889,
            'rapid': 2925,  'classical': 2773, 'chess960': 2021,
        }
        self.current          = dict(self.baseline)
        self.losing_streak    = 0
        self.protection_games = 0
        self.in_protection    = False

    def record_result(self, result, mode, new_rating=None):
        # Puan düşüşü kontrolü
        if new_rating and mode in self.current:
            old = self.current[mode]
            self.current[mode] = new_rating
            drop = old - new_rating
            if drop >= SETTINGS["RATING_DROP_THRESHOLD"]:
                self._activate_protection(
                    f"{mode.capitalize()} puanı {drop} puan düştü ({old}→{new_rating})"
                )

        # Seri kontrolü
        if result == 'loss':
            self.losing_streak += 1
            if self.losing_streak >= SETTINGS["LOSING_STREAK_LIMIT"]:
                self._activate_protection(
                    f"{self.losing_streak} üst üste kayıp"
                )
        else:
            self.losing_streak = 0

        # Koruma geri sayımı
        if self.in_protection:
            self.protection_games -= 1
            if self.protection_games <= 0:
                self.in_protection = False
                self.losing_streak = 0
                print("✅ [Koruma] Koruma modu sona erdi, normal dağılıma dönülüyor.")

    def _activate_protection(self, reason):
        if not self.in_protection:
            print(f"🛡️ [Koruma] {reason}")
            print(f"🛡️ [Koruma] Sonraki {SETTINGS['PROTECTION_GAME_COUNT']} maç Mid tier'da oynanacak.")
        self.in_protection    = True
        self.protection_games = SETTINGS["PROTECTION_GAME_COUNT"]

    def is_in_protection(self):
        return self.in_protection


class Matchmaker:
    def __init__(self, client, config, active_games, token):
        self.client            = client
        self.config            = config.get("matchmaking", {})
        self.enabled           = self.config.get("allow_feed", True)
        self.active_games      = active_games
        self.my_id             = None
        self.bot_pool          = []
        self.blacklist         = {}
        self.opponent_tracker  = {}
        self.last_pool_update  = 0
        self.wait_timeout      = 120
        self.registered_tournaments = set()
        self.last_tournament_join   = 0
        self.token             = token
        self.rating_tracker    = RatingTracker()
        self._initialize_id()

    def _initialize_id(self):
        try:
            self.my_id = self.client.account.get()['id']
            print(f"[Matchmaker] Bağlantı Başarılı. ID: {self.my_id}")
        except:
            self.my_id = "oxydan"

    def _is_stop_triggered(self):
        if os.path.exists(SETTINGS["STOP_FILE"]):
            if len(self.active_games) == 0:
                print("🏁 [Matchmaker] Sistem kapatılıyor.")
                os._exit(0)
            return True
        return False

    def _get_bot_rating(self, bot_id, clock_limit_sn):
        try:
            if clock_limit_sn < 180:    mode = 'bullet'
            elif clock_limit_sn < 480:  mode = 'blitz'
            elif clock_limit_sn < 1500: mode = 'rapid'
            else:                        mode = 'classical'
            data = self.client.users.get_public_data(bot_id)
            return data.get('perfs', {}).get(mode, {}).get('rating', 0)
        except:
            return 0

    def _is_in_tournament_game(self):
        try:
            ongoing = self.client.games.get_ongoing()
            return any(g.get('tournamentId') or g.get('swissId') for g in ongoing)
        except:
            return False

    # ==========================================================
    # 🏆 TURNUVA YÖNETİMİ
    # ==========================================================

    def _auth_headers(self):
        h = {"User-Agent": "OxydanBot/3.0"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _fetch_arena_tournaments(self):
        try:
            r = requests.get(
                "https://lichess.org/api/tournament",
                headers=self._auth_headers(), timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                return data.get('created', []) + data.get('started', [])
        except Exception as e:
            print(f"⚠️ [Arena] {e}")
        return []

    def _fetch_swiss_tournaments(self):
        bot_teams  = ["lichess-bots", "computer-chess-club", "engine-bots"]
        swiss_list = []
        for team in bot_teams:
            try:
                r = requests.get(
                    f"https://lichess.org/api/team/{team}/swiss",
                    headers=self._auth_headers(),
                    params={"status": "created"}, timeout=10
                )
                if r.status_code == 200:
                    for line in r.text.strip().split('\n'):
                        if line:
                            try: swiss_list.append(json.loads(line))
                            except: pass
            except Exception as e:
                print(f"⚠️ [Swiss] {team}: {e}")
        return swiss_list

    def _join_arena(self, tid):
        try:
            r = requests.post(
                f"https://lichess.org/api/tournament/{tid}/join",
                headers=self._auth_headers(), timeout=10
            )
            return r.status_code == 200
        except Exception as e:
            print(f"⚠️ [Arena] Katılım: {e}")
            return False

    def _join_swiss(self, sid):
        try:
            r = requests.post(
                f"https://lichess.org/api/swiss/{sid}/join",
                headers=self._auth_headers(), timeout=10
            )
            return r.status_code == 200
        except Exception as e:
            print(f"⚠️ [Swiss] Katılım: {e}")
            return False

    def _manage_tournaments(self):
        if not SETTINGS.get("AUTO_TOURNAMENT", True):
            return
        if (time.time() - self.last_tournament_join) < SETTINGS["TOURNAMENT_COOLDOWN"]:
            return

        print("[Matchmaker] Turnuvalar taranıyor (Arena + Swiss)...")

        for t in self._fetch_arena_tournaments():
            tid  = t.get('id')
            if tid in self.registered_tournaments: continue
            name = t.get('fullName', '').lower()
            if SETTINGS.get("ONLY_BOT_TOURNEYS") and "bot" not in name: continue
            starts = t.get('startsAt', 0) / 1000
            if starts > 0 and (starts - time.time()) > SETTINGS["JOIN_UPCOMING_MINS"] * 60: continue
            if self._join_arena(tid):
                self.registered_tournaments.add(tid)
                self.last_tournament_join = time.time()
                print(f"🏆 [Arena] KATILINDI: {t.get('fullName')}")
                return

        for s in self._fetch_swiss_tournaments():
            sid  = s.get('id')
            if sid in self.registered_tournaments: continue
            name = s.get('name', '').lower()
            if SETTINGS.get("ONLY_BOT_TOURNEYS") and "bot" not in name: continue
            starts = s.get('startsAt', 0) / 1000
            if starts > 0 and (starts - time.time()) > SETTINGS["JOIN_UPCOMING_MINS"] * 60: continue
            if self._join_swiss(sid):
                self.registered_tournaments.add(sid)
                self.last_tournament_join = time.time()
                print(f"🏆 [Swiss] KATILINDI: {s.get('name')}")
                return

    def _cleanup_history(self):
        if len(self.registered_tournaments) > 500:
            self.registered_tournaments.clear()
            print("🧹 [System] Turnuva hafızası temizlendi.")

    # ==========================================================
    # 📋 PROTOKOL — Meydan Okuma Kabulü
    # ==========================================================

    def is_challenge_acceptable(self, challenge):
        if self._is_in_tournament_game():
            return False, "Currently in a tournament game."

        variant = challenge.get('variant', {}).get('key', 'standard')
        if variant not in ['standard', 'chess960']:
            return False, f"Variant '{variant}' not supported."

        challenger = challenge.get('challenger')
        if not challenger:
            return False, "No challenger info."

        user_id = challenger.get('id', '')
        rating  = challenger.get('rating') or 0
        title   = (challenger.get('title') or '').upper()
        is_bot  = title == 'BOT'
        rated   = challenge.get('rated', False)

        # DÜZELTME 1: Kalıcı blacklist kontrolü (gelen meydan okumalar için)
        if user_id.lower() in SETTINGS["PERMANENT_BLACKLIST"]:
            return False, f"{user_id} is permanently blacklisted."

        tc = challenge.get('timeControl', {})
        if tc.get('type') != 'clock':
            return False, "Only clock games allowed."

        limit_sn = tc.get('limit', 0)

        if self.opponent_tracker.get(user_id, 0) >= 3:
            return False, "Too many games with this opponent recently."

        # İNSAN
        if not is_bot:
            if rating < 1500:
                return False, "Human rating below 1500."
            if rated:
                return False, "Humans must play casual."
            if limit_sn < 30 or limit_sn > 1800:
                return False, "Time control out of range (0.5+0 to 30+0)."
            return True, f"Accepted human ({rating})"

        # BOT
        if rating < 1500:
            return False, "Bot rating below 1500."
        if 1500 <= rating < 2000:
            if rated:
                return False, "Bots 1500-2000 must play casual."
            if limit_sn > 600:
                return False, "Max 10+0 for bots 1500-2000."
            return True, f"Accepted casual bot ({rating})"
        if 2000 <= rating < 2300:
            if limit_sn > 600:
                return False, "Max 10+0 for bots 2000-2300."
            return True, f"Accepted rated bot ({rating})"
        # 2300+
        if limit_sn < 30 or limit_sn > 1800:
            return False, "Time control out of range (0.5+0 to 30+0)."
        return True, f"Accepted elite bot ({rating})"

    # ==========================================================
    # 🎯 MATCHMAKER — Akıllı Tier Seçimi
    # ==========================================================

    def _pick_tier(self):
        """
        Koruma aktifse → Mid kilitli.
        Değilse ağırlıklı rastgele:
          Low %10 | Mid %23 | High %35 | Elite %32
        """
        if self.rating_tracker.is_in_protection():
            print(f"🛡️ [Koruma] Mid kilitli — kalan: {self.rating_tracker.protection_games} maç")
            return SETTINGS["TIER_MID"]

        r = random.random()
        t = SETTINGS["TIER_THRESHOLDS"]
        if r < t["LOW"]:  return SETTINGS["TIER_LOW"]
        if r < t["MID"]:  return SETTINGS["TIER_MID"]
        if r < t["HIGH"]: return SETTINGS["TIER_HIGH"]
        return SETTINGS["TIER_ELITE"]

    def _refresh_bot_pool(self):
        now = time.time()
        if not self.bot_pool or (now - self.last_pool_update > SETTINGS["POOL_REFRESH_SECONDS"]):
            try:
                stream = self.client.bots.get_online_bots()
                online = list(itertools.islice(stream, 200))
                self.bot_pool = [
                    b.get('id') for b in online
                    if b.get('id') and b.get('id').lower() != (self.my_id or '').lower()
                    # DÜZELTME 2: Bot havuzu oluşturulurken kalıcı blacklist filtrelenir
                    and b.get('id', '').lower() not in SETTINGS["PERMANENT_BLACKLIST"]
                ]
                random.shuffle(self.bot_pool)
                self.last_pool_update = now
                print(f"[Matchmaker] Bot havuzu: {len(self.bot_pool)} bot")
            except:
                time.sleep(10)

    def _find_suitable_target(self):
        self._refresh_bot_pool()
        tier      = self._pick_tier()
        tier_name = _TIER_NAME.get(tier, "?")  # DÜZELTME 3: Dead code kaldırıldı, tek lookup
        now       = datetime.now()

        if tier == SETTINGS["TIER_LOW"]:
            tc_pool  = SETTINGS["TC_MAX_10"]
            is_rated = False
        elif tier == SETTINGS["TIER_MID"]:
            tc_pool  = SETTINGS["TC_MAX_10"]
            is_rated = SETTINGS["RATED_MODE"]
        else:
            tc_pool  = SETTINGS["TC_ALL"]
            is_rated = SETTINGS["RATED_MODE"]

        tc_str           = random.choice(tc_pool)
        limit_sn, inc_sn = _parse_tc(tc_str)

        random.shuffle(self.bot_pool)
        for bot_id in self.bot_pool[:40]:
            if bot_id in self.blacklist and self.blacklist[bot_id] > now:
                continue
            rating = self._get_bot_rating(bot_id, limit_sn)
            time.sleep(0.3)
            if tier[0] <= rating <= tier[1]:
                return bot_id, rating, limit_sn, inc_sn, is_rated, tier_name

        return None, 0, 0, 0, False, tier_name

    def record_game_result(self, result, mode, new_rating=None):
        """
        lichess-bot.py'den oyun sonunda çağrılır.
        result: 'win' | 'loss' | 'draw'
        mode:   'bullet' | 'blitz' | 'rapid' | 'classical' | 'chess960'
        new_rating: opsiyonel yeni puan
        """
        self.rating_tracker.record_result(result, mode, new_rating)

    # ==========================================================
    # 🚀 ANA DÖNGÜ
    # ==========================================================

    def start(self):
        if not self.enabled:
            return

        print("🚀 Matchmaker v3 Aktif — Akıllı Koruma Protokolü")
        print("   Tier: Elite %32 | High %35 | Mid %23 | Low %10")
        last_cleanup = time.time()

        while True:
            try:
                if time.time() - last_cleanup > 21600:
                    self._cleanup_history()
                    last_cleanup = time.time()

                self._manage_tournaments()

                if self._is_in_tournament_game() or self._is_stop_triggered():
                    time.sleep(60)
                    continue

                if len(self.active_games) < SETTINGS["MAX_PARALLEL_GAMES"]:
                    target, rating, limit_sn, inc_sn, is_rated, tier_name = self._find_suitable_target()

                    if target:
                        variant   = 'chess960' if random.random() < SETTINGS["CHESS960_CHANCE"] else 'standard'
                        rated_str = "Rated" if is_rated else "Casual"
                        mins      = limit_sn // 60
                        secs      = limit_sn % 60
                        tc_label  = f"{mins}:{secs:02d}+{inc_sn}" if secs else f"{mins}+{inc_sn}"

                        print(f"[{tier_name}] → {target} ({rating}) | {rated_str} | {tc_label} | {variant}")

                        self.blacklist[target] = datetime.now() + timedelta(
                            minutes=SETTINGS["BLACKLIST_MINUTES"]
                        )
                        self.client.challenges.create(
                            username=target,
                            rated=is_rated,
                            variant=variant,
                            clock_limit=limit_sn,
                            clock_increment=inc_sn
                        )
                        time.sleep(SETTINGS["SAFETY_LOCK_TIME"])
                    else:
                        time.sleep(10)
                else:
                    time.sleep(10)

            except Exception as e:
                err = str(e)
                if "429" in err:
                    print(f"⚠️ Rate limit (429), {self.wait_timeout}sn bekleniyor.")
                    time.sleep(self.wait_timeout)
                    self.wait_timeout = min(self.wait_timeout * 2, 900)
                else:
                    print(f"⚠️ [Matchmaker] Hata: {err}")
                    time.sleep(30)
