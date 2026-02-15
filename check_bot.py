import chess
import os
import sys
import importlib.util
import time

def run_diagnostic():
    print("🛠️ Oxybullet v1 Pre-Flight Diagnostics...")
    
    # 1. Dosya Yollarını Tanımla
    main_script = "lichess-bot.py"
    exe_path = "./src/Ethereal"
    
    # 2. Dosya Kontrolleri
    if not os.path.exists(main_script):
        print(f"❌ ERROR: {main_script} bulunamadı!")
        sys.exit(1)
        
    if not os.path.exists(exe_path):
        print(f"❌ ERROR: Motor dosyası (binary) {exe_path} konumunda yok!")
        sys.exit(1)

    try:
        # 3. Dinamik Olarak Modülü Yükle
        spec = importlib.util.spec_from_file_location("lichess_bot_module", main_script)
        lichess_bot_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lichess_bot_module)
        
        # Sınıfı modülün içinden al (V4 olarak güncellendi)
        # Eğer main_script içinde sınıf adını V4 yaptıysan burayı da V4 yapmalısın
        OxydanAegisV4 = getattr(lichess_bot_module, "OxydanAegisV4")
        
        print("✅ Module loaded successfully.")

        # 4. Motor Havuzu Başlatma Testi (Düşük Hash ile)
        # UCI ayarlarını V4'ün beklediği formatta gönderiyoruz
        bot = OxydanAegisV4(exe_path, uci_options={"Hash": 16, "Threads": 2})
        board = chess.Board()
        
        # 5. Hamle Üretme Testi
        print("♟️ Testing pool-based engine move generation...")
        # V4 yapısında get_best_move artık havuzdan motor çekiyor
        move = bot.get_best_move(board, 10000, 10000, 1000, 1000)
        
        if move and move in board.legal_moves:
            print(f"✅ SUCCESS: Engine produced legal move: {move.uci()}")
            
            # --- KRİTİK DEĞİŞİKLİK: HAVUZU GÜVENLİ BOŞALTMA ---
            print("🧹 Cleaning up engine pool processes...")
            
            # Havuzdaki tüm motorları tek tek çek ve kapat
            closed_engines = 0
            while not bot.engine_pool.empty():
                try:
                    # Motoru havuzdan al
                    engine = bot.engine_pool.get_nowait()
                    
                    # Motorun kapanması için QUIT komutu gönder ve kısa bir süre bekle
                    engine.quit() 
                    closed_engines += 1
                except Exception as e:
                    print(f"⚠️ Bir motor kapatılırken hata oluştu: {e}")
                finally:
                    # Havuz mantığında her get() için task_done() çağırmak iyidir
                    bot.engine_pool.task_done()

            # İşletim sistemine motorların kapanması için zaman tanı
            time.sleep(1) 
            print(f"✅ {closed_engines} motor başarıyla kapatıldı ve süreçler temizlendi.")
            print("✅ Diagnostics passed. Ready for deployment.")
            
            # Başarılı çıkış - 0 koduyla çıkması sistemin botu başlatmasına izin verir
            os._exit(0)  # sys.exit yerine os._exit bazen thread'leri daha temiz öldürür
        else:
            print("❌ ERROR: Engine failed to produce a valid move!")
            sys.exit(1)

if __name__ == "__main__":
    run_diagnostic()
