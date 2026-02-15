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
# ⚙️ MODÜLER AYARLAR PANELİ (Burayı Değiştirmeniz Yeterli)
# ==========================================================
SETTINGS = {
    "TOKEN": os.environ.get('LICHESS_TOKEN'),
    "ENGINE_PATH": "./src/Ethereal",
    "BOOK_PATH": "./book.bin",
    
    # --- OYUN LİMİTLERİ ---
    "MAX_PARALLEL_GAMES": 2,      # Aynı anda oynanacak maç sayısı
    "MAX_TOTAL_RUNTIME": 21300,   # Toplam çalışma süresi (5 saat 55 dk)
    "STOP_ACCEPTING_MINS": 15,    # Kapanışa kaç dk kala yeni maç almasın?
    
    # --- MOTOR VE ZAMAN YÖNETİMİ ---
    "LATENCY_BUFFER": 0.03,       # Saniye cinsinden ağ gecikme payı (150ms)
    "TABLEBASE_PIECE_LIMIT": 7,   # Kaç taş kalınca tablebase'e sorsun? (6 güvenlidir)
    "MIN_THINK_TIME": 0,       # En az düşünme süresi
    
    # --- MESAJLAR ---
    "GREETING": "Oxybullet 1 Active. System stabilized.",
}
# ==========================================================

class OxydanAegisV4:
    def __init__(self, exe_path, uci_options=None):
        self.exe_path = exe_path
        self.book_path = SETTINGS["BOOK_PATH"]
        self.uci_options = uci_options
        self.engine_pool = queue.Queue()
        
        # Havuz Boyutu: Paralel maç sayısı + 1 (Yedek ünite)
        pool_size = SETTINGS["MAX_PARALLEL_GAMES"] + 1
        
        try:
            for i in range(pool_size):
                eng = chess.engine.SimpleEngine.popen_uci(self.exe_path, timeout=30)
                if uci_options:
                    for opt, val in uci_options.items():
                        try: eng.configure({opt: val})
                        except: pass
                self.engine_pool.put(eng)
            print(f"🚀 Oxydan v7: {pool_size} Motor Ünitesi Havuza Alındı.", flush=True)
        except Exception as e:
            print(f"KRİTİK HATA: Motorlar başlatılamadı: {e}", flush=True)
            sys.exit(1)

    def to_seconds(self, t):
        if t is None: return 0.0
        if isinstance(t, timedelta): return t.total_seconds()
        try:
            val = float(t)
            return val / 1000.0 if val > 1000 else val
        except: return 0.0

    def calculate_smart_time(self, t, inc, board):
        """
        OxyBullet Özel: Her tempoda mermi hızında oynar.
        """
        # 1. ACİL DURUM (Süre 1 saniyenin altındaysa pre-move hızı)
        if t < 1.0:
            return 0.005 

        # 2. HEDEF HIZ (0.06 saniye ideal bir 'bak-ve-oyna' süresidir)
        target_time = 0.06 

        # 3. GECİKME PAYINI ÇIKAR (LATENCY_BUFFER 0.04 - 0.05 civarı olmalı)
        buffer = SETTINGS.get("LATENCY_BUFFER", 0.04)
        final_time = target_time - buffer
        
        # Asla 0.01'in altına düşme (Motorun hata vermemesi için kilit nokta)
        return max(SETTINGS.get("MIN_THINK_TIME", 0.01), final_time)

    def get_best_move(self, board, wtime, btime, winc, binc):
        """
        Sırasıyla: Kitap -> Syzygy API -> Ethereal Engine
        """
        # --- 1. ADIM: AÇILIŞ KİTABI ---
        if os.path.exists(SETTINGS["BOOK_PATH"]):
            try:
                with chess.polyglot.open_reader(SETTINGS["BOOK_PATH"]) as reader:
                    best_entry = None
                    for entry in reader.find_all(board):
                        if best_entry is None or entry.weight > best_entry.weight:
                            best_entry = entry
                    if best_entry:
                        print(f"📖 Kitap: {best_entry.move}", flush=True)
                        return best_entry.move
            except Exception as e:
                print(f"⚠️ Kitap Hatası: {e}", flush=True)

        # --- 2. ADIM: SYZYGY TABLEBASE (Oyun Sonu) ---
        piece_count = len(board.piece_map())
        if piece_count <= SETTINGS.get("TABLEBASE_PIECE_LIMIT", 7):
            try:
                current_time_ms = wtime if board.turn == chess.WHITE else btime
                current_time_sec = self.to_seconds(current_time_ms)
                
                # Süreye göre derinlik ve timeout ayarı
                api_timeout = 0.4 if current_time_sec > 10 else 0.2
                fen = board.fen().replace(" ", "_")
                
                r = requests.get(f"https://tablebase.lichess.ovh/standard?fen={fen}", timeout=api_timeout)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("moves"):
                        tb_move = data["moves"][0]["uci"]
                        print(f"🎯 Tablebase: {tb_move}", flush=True)
                        return chess.Move.from_uci(tb_move)
            except Exception as e:
                print(f"⚠️ Syzygy Pas Geçildi: {e}", flush=True)

        # --- 3. ADIM: MOTOR HESAPLAMA (Ethereal) ---
        engine = self.engine_pool.get() # Havuzdan bir motor al
        try:
            my_time = wtime if board.turn == chess.WHITE else btime
            my_inc = winc if board.turn == chess.WHITE else binc
            
            think_time = self.calculate_smart_time(self.to_seconds(my_time), self.to_seconds(my_inc), board)
            
            result = engine.play(board, chess.engine.Limit(time=think_time))
            print(f"⚙️ Motor: {result.move} ({think_time:.3f}s)", flush=True)
            return result.move
            
        except Exception as e:
            print(f"🚨 Motor Hatası: {e}", flush=True)
            return next(iter(board.legal_moves)) # Çökmemek için ilk yasal hamle
        finally:
            self.engine_pool.put(engine) # Motoru mutlaka havuza geri koy

def handle_game(client, game_id, bot, my_id):
    try:
        client.bots.post_message(game_id, SETTINGS["GREETING"])
        stream = client.bots.stream_game_state(game_id)
        my_color = None

        for state in stream:
            if 'error' in state: break

            if state['type'] == 'gameFull':
                my_color = chess.WHITE if state['white'].get('id') == my_id else chess.BLACK
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
                wtime, btime = curr_state.get('wtime'), curr_state.get('btime')
                winc, binc = curr_state.get('winc'), curr_state.get('binc')
                move = bot.get_best_move(board, wtime, btime, winc, binc)
                
                if move:
                    for attempt in range(3):
                        try:
                            client.bots.make_move(game_id, move.uci())
                            break 
                        except:
                            time.sleep((attempt + 1) * 1)
    except Exception as e:
        print(f"Oyun Hatası ({game_id}): {e}", flush=True)

def handle_game_wrapper(client, game_id, bot, my_id, active_games):
    try:
        handle_game(client, game_id, bot, my_id)
    finally:
        active_games.discard(game_id)
        print(f"✅ [{game_id}] Bitti. Kalan Slot: {len(active_games)}/{SETTINGS['MAX_PARALLEL_GAMES']}", flush=True)

def main():
    start_time = time.time()
    
    try:
        with open("config.yml", "r") as f:
            config = yaml.safe_load(f)
    except:
        print("HATA: config.yml okunamadı.")
        return

    session = berserk.TokenSession(SETTINGS["TOKEN"])
    client = berserk.Client(session=session)
    try:
        my_id = client.account.get()['id']
    except:
        print("Lichess bağlantısı kurulamadı.")
        return

    bot = OxydanAegisV4(SETTINGS["ENGINE_PATH"], uci_options=config.get('engine', {}).get('uci_options', {}))
    active_games = set() 

    if config.get("matchmaking"):
        mm = Matchmaker(client, config, active_games) 
        threading.Thread(target=mm.start, daemon=True).start()

    print(f"🔥 Oxydan Aegis Hazır. ID: {my_id} | Max Slot: {SETTINGS['MAX_PARALLEL_GAMES']}", flush=True)

    while True:
        try:
            # Stream başlatılıyor
            for event in client.bots.stream_incoming_events():
                # 1. HER EVENTTE ZAMAN KONTROLÜ
                cur_elapsed = time.time() - start_time
                should_stop = os.path.exists("STOP.txt") or cur_elapsed > SETTINGS["MAX_TOTAL_RUNTIME"]
                
                # Yeni maç kabul etme sınırı (Son 15 dk kala kapıları kapat)
                close_to_end = cur_elapsed > (SETTINGS["MAX_TOTAL_RUNTIME"] - (SETTINGS["STOP_ACCEPTING_MINS"] * 60))

                if event['type'] == 'challenge':
                    ch_id = event['challenge']['id']
                    
                    if should_stop or close_to_end or len(active_games) >= SETTINGS["MAX_PARALLEL_GAMES"]:
                        client.challenges.decline(ch_id, reason='later')
                        # Eğer her şey bittiyse ve süre dolduysa tamamen çık
                        if should_stop and len(active_games) == 0: 
                            print("🏁 Tüm maçlar bitti, sistem güvenli kapatıldı.")
                            sys.exit(0)
                    else:
                        client.challenges.accept(ch_id)

                elif event['type'] == 'gameStart':
                    game_id = event['game']['id']
                    if game_id not in active_games and len(active_games) < SETTINGS["MAX_PARALLEL_GAMES"]:
                        active_games.add(game_id)
                        threading.Thread(
                            target=handle_game_wrapper,
                            args=(client, game_id, bot, my_id, active_games),
                            daemon=True
                        ).start()
                
                # Süre dolmuşsa ve bekleyen maç yoksa döngüden çık ve bitir
                if should_stop and len(active_games) == 0:
                    sys.exit(0)

        except Exception as e:
            if "429" in str(e):
                time.sleep(60)
            else:
                time.sleep(5)

if __name__ == "__main__":
    main()
