import os
import sys
import berserk
import chess
import chess.engine
import time
import chess.polyglot
import threading
import yaml
import requests
import queue
from datetime import timedelta
from matchmaking import Matchmaker

# ==========================================================
# ⚙️ MODÜLER AYARLAR PANELİ
# ==========================================================
SETTINGS = {
    "TOKEN": os.environ.get('LICHESS_TOKEN'),
    "ENGINE_PATH": "./src/Ethereal",
    "BOOK_PATH": "./book.bin",
    
    "MAX_PARALLEL_GAMES": 2,
    "MAX_TOTAL_RUNTIME": 21300,
    "STOP_ACCEPTING_MINS": 15,
    
    "LATENCY_BUFFER": 0.03,
    "TABLEBASE_PIECE_LIMIT": 7,
    "MIN_THINK_TIME": 0,
    
    "GREETING": "Oxybullet 2 On The Board!",
}

# Aynı rakiple kaç maç yapılabileceği sınırı
MAX_GAMES_PER_OPPONENT = 3 
opponent_tracker = {} 

# ==========================================================

class OxydanAegisV4:
    def __init__(self, exe_path, uci_options=None):
        self.exe_path = exe_path
        self.book_path = SETTINGS["BOOK_PATH"]
        self.engine_pool = queue.Queue()
        
        pool_size = SETTINGS["MAX_PARALLEL_GAMES"] + 1
        
        try:
            for i in range(pool_size):
                eng = chess.engine.SimpleEngine.popen_uci(self.exe_path, timeout=30)
                # DÜZELTME: MoveOverhead (Bitişik yazım)
                eng.configure({"MoveOverhead": 50}) 
                if uci_options:
                    for opt, val in uci_options.items():
                        try: eng.configure({opt: val})
                        except: pass
                self.engine_pool.put(eng)
            print(f"🚀 Oxybullet: {pool_size} Motor Hazır.", flush=True)
        except Exception as e:
            print(f"KRİTİK HATA: {e}", flush=True); sys.exit(1)

    def to_seconds(self, t):
        if t is None: return 0.0
        if isinstance(t, timedelta): return t.total_seconds()
        try:
            val = float(t)
            return val / 1000.0 if val > 1000 else val
        except: return 0.0

    def calculate_smart_time(self, t, inc, board):
        if t < 1.0: return 0.005 
        target_time = 0.06 
        buffer = SETTINGS.get("LATENCY_BUFFER", 0.05)
        final_time = target_time - buffer
        return max(0.01, final_time)

    def get_best_move(self, board, wtime, btime, winc, binc):
        # 1. KİTAP
        if os.path.exists(SETTINGS["BOOK_PATH"]):
            try:
                with chess.polyglot.open_reader(SETTINGS["BOOK_PATH"]) as reader:
                    entry = reader.get(board)
                    if entry: return entry.move
            except: pass

        # 2. TABLEBASE
        if len(board.piece_map()) <= SETTINGS["TABLEBASE_PIECE_LIMIT"]:
            try:
                fen = board.fen().replace(" ", "_")
                r = requests.get(f"https://tablebase.lichess.ovh/standard?fen={fen}", timeout=0.3)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("moves"): return chess.Move.from_uci(data["moves"][0]["uci"])
            except: pass

        # 3. MOTOR
        engine = self.engine_pool.get()
        try:
            my_time = self.to_seconds(wtime if board.turn == chess.WHITE else btime)
            my_inc = self.to_seconds(winc if board.turn == chess.WHITE else binc)
            think_time = self.calculate_smart_time(my_time, my_inc, board)
            result = engine.play(board, chess.engine.Limit(time=think_time))
            return result.move
        except Exception as e:
            print(f"🚨 Motor Hatası: {e}"); return next(iter(board.legal_moves))
        finally:
            self.engine_pool.put(engine)

def is_challenge_acceptable(challenge):
    """
    Oxybullet Protokol Denetleyici (Fedai Mekanizması)
    """
    challenger = challenge.get('challenger')
    if not challenger: return False, "Generic challenge"

    user_id = challenger['id']
    rating = challenger.get('rating', 0)
    is_bot = challenger.get('title') == 'BOT'
    rated = challenge.get('rated', False)
    
    # Zaman Kontrolü (Lichess saniye cinsinden gönderir)
    time_control = challenge.get('timeControl', {})
    limit = time_control.get('limit', 0)
    increment = time_control.get('increment', 0)
    
    # 1. Anti-Farming: Aynı rakiple çok fazla oynama
    if opponent_tracker.get(user_id, 0) >= MAX_GAMES_PER_OPPONENT:
        return False, "Too many games recently"

    # 2. BOT Protokolü
    if is_bot:
        # DURUM A: MASTERS (2000+) - Puanlı veya Puansız her şeyi kabul et (Max 30dk)
        if rating >= 2000:
            if limit <= 1800: 
                return True, "Accepted Masters Bot"
            return False, "Masters time limit exceeded (max 30m)"

        # DURUM B: CHALLENGERS (1500 - 2000) - SADECE PUANSIZ (Casual) ve HIZLI (Max 5dk)
        elif 1500 <= rating < 2000:
            if rated: 
                return False, "Challengers must play Casual (Rating Protection)"
            if limit <= 300: 
                return True, "Accepted Casual Challenger"
            return False, "Challenger time limit exceeded (max 5m)"
        
        # 1500 Altı botları direkt engelle
        return False, "Bot rating too low for protocol"

    # 3. İnsan (Human) Protokolü
    else:
        # İnsanlarla ASLA puanlı oynama (Hileci riskine karşı)
        if rated: 
            return False, "Humans must play Casual"
        # Max 10+0
        if limit <= 600: 
            return True, "Accepted Casual Human"
        return False, "Human time limit exceeded (max 10+0)"

    return False, "Unknown protocol violation"

def handle_game(client, game_id, bot, my_id):
    try:
        client.bots.post_message(game_id, SETTINGS["GREETING"])
        stream = client.bots.stream_game_state(game_id)
        my_color = None

        for state in stream:
            if 'error' in state: break
            if state['type'] == 'gameFull':
                my_color = chess.WHITE if state['white'].get('id') == my_id else chess.BLACK
                # Rakibi takip listesine ekle
                opp_id = state['black']['id'] if my_color == chess.WHITE else state['white']['id']
                opponent_tracker[opp_id] = opponent_tracker.get(opp_id, 0) + 1
                curr_state = state['state']
            elif state['type'] == 'gameState':
                curr_state = state
            else: continue

            moves = curr_state.get('moves', "").split()
            board = chess.Board()
            for m in moves: board.push_uci(m)

            if curr_state.get('status') in ['mate', 'resign', 'draw', 'outoftime', 'aborted', 'stalemate']:
                break

            if board.turn == my_color and not board.is_game_over():
                move = bot.get_best_move(board, curr_state.get('wtime'), curr_state.get('btime'), 
                                        curr_state.get('winc'), curr_state.get('binc'))
                if move:
                    for attempt in range(3):
                        try:
                            client.bots.make_move(game_id, move.uci())
                            break 
                        except: time.sleep(0.5)
    except Exception as e:
        print(f"🚨 Oyun Hatası ({game_id}): {e}", flush=True)

def handle_game_wrapper(client, game_id, bot, my_id, active_games):
    try: handle_game(client, game_id, bot, my_id)
    finally: active_games.discard(game_id)

def main():
    start_time = time.time()
    session = berserk.TokenSession(SETTINGS["TOKEN"])
    client = berserk.Client(session=session)
    
    try:
        with open("config.yml", "r") as f: config = yaml.safe_load(f)
        my_id = client.account.get()['id']
    except Exception as e:
        print(f"Bağlantı Hatası: {e}"); return

    bot = OxydanAegisV4(SETTINGS["ENGINE_PATH"], uci_options=config.get('engine', {}).get('uci_options', {}))
    active_games = set() 

    if config.get("matchmaking"):
        mm = Matchmaker(client, config, active_games) 
        threading.Thread(target=mm.start, daemon=True).start()

    print(f"🔥 Oxybullet 2 Hazır. ID: {my_id}", flush=True)

    # ANA DÖNGÜ: Bağlantı kopsa da tazeleyerek devam eder
    while True:
        try:
            for event in client.bots.stream_incoming_events():
                cur_elapsed = time.time() - start_time
                should_stop = os.path.exists("STOP.txt") or cur_elapsed > SETTINGS["MAX_TOTAL_RUNTIME"]
                close_to_end = cur_elapsed > (SETTINGS["MAX_TOTAL_RUNTIME"] - (SETTINGS["STOP_ACCEPTING_MINS"] * 60))

                if event['type'] == 'challenge':
                    ch_id = event['challenge']['id']
                    accept, reason = is_challenge_acceptable(event['challenge'])
                    
                    if not should_stop and not close_to_end and len(active_games) < SETTINGS["MAX_PARALLEL_GAMES"] and accept:
                        client.challenges.accept(ch_id)
                    else:
                        client.challenges.decline(ch_id, reason='later' if accept else 'policy')
                        if should_stop and len(active_games) == 0: sys.exit(0)

                elif event['type'] == 'gameStart':
                    game_id = event['game']['id']
                    if game_id not in active_games and len(active_games) < SETTINGS["MAX_PARALLEL_GAMES"]:
                        active_games.add(game_id)
                        threading.Thread(target=handle_game_wrapper, args=(client, game_id, bot, my_id, active_games), daemon=True).start()

        except Exception as e:
            print(f"⚠️ Akış koptu, 5sn içinde tazeleniyor: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
