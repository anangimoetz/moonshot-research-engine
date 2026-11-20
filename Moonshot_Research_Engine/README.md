# Moonshot Research Engine v3.1

## Mulai deploy untuk penerima yang sudah ditentukan

ID Telegram penerima scanner telah diisi **6720326248**. Upload isi folder ini ke root repo GitHub, lalu buat **Render Blueprint** dari repo agar `render.yaml` diterapkan (bukan hanya menyambungkan Web Service dengan konfigurasi default).

Render akan meminta **SCANNER_TELEGRAM_BOT_TOKEN** pada form rahasia saat pertama membuat Blueprint. Masukkan token baru dari BotFather di Render, bukan di GitHub. Token yang pernah dibagikan di chat tidak disertakan paket. Tekan **Start** pada bot Anda agar bot dapat mengirim DM. Setelah deploy, worker mengantrekan pemberitahuan startup, menjalankan scan bergilir 5 menit dan laporan sesi. Telegram belum diuji nyata dari layanan Render; periksa pesan dan health setelah deploy.

Blueprint memakai **layanan berbayar + disk persisten**. Ketersediaan 24/7 bergantung pada host dan koneksi data. Watcher Rose tetap memerlukan session akun sumber yang diotorisasi terpisah; token bot saja tidak mengaktifkan penerimaan post Rose.

## Chart live v3.1

TradingView Lightweight Charts **5.3.0** disertakan lokal dengan lisensi Apache-2.0, integritas npm dan atribusi. Pilihan timeframe 1m/5m/15m/1h/4h/1d; spot/perpetual terpisah. Historical seed diambil melalui REST publik lalu candle diperbarui dari WebSocket Bitget, Binance, Bybit. Terdapat reconnect, gap-triggered history recovery, stale-event rejection, EMA13/21 candle tertutup dan garis rencana yang cocok produknya. Chart live tidak mengubah keputusan research snapshot atau meluluskan entry.

**Bitget daily memakai REST UTC berkala**, karena stream daily v3 tidak dicampur dengan daily 00UTC. Status ini ditulis REST UTC, bukan LIVE. Feed chart berjalan ketika browser terbuka; worker scanner dan pengiriman berjalan di server walaupun browser ditutup. Browser harus dapat menjangkau domain WebSocket exchange; pembatasan jaringan/geografis ditampilkan sebagai error/reconnecting. Jangan menyebut semua feed server sudah streaming: discovery/analisis server masih memakai jadwal REST.

Pada layanan Render yang sudah ada, `sync:false` tidak menambahkan secret baru saat update Blueprint: masukkan token baru di service **Environment** secara manual sebelum deploy ulang. Role baca MCP tidak menerima token bot dan tidak memaparkan secret.

Referensi: [Render secret prompts](https://render.com/docs/blueprint-spec#prompting-for-secret-values), [TradingView real-time updates](https://tradingview.github.io/lightweight-charts/examples/data/realtime-updates), [Bitget candles](https://www.bitget.com/docs/uta/websocket/public/Candlesticks-Channel), [Bybit candles](https://bybit-exchange.github.io/docs/v5/websocket/public/kline).

Tes JavaScript tambahan: `node --test moonshot_scanner/tests/chart.test.cjs`. Screenshot browser lokal dan hasil live WebSocket publik ada dalam `validation/`.

Repo mandiri untuk discovery crypto dan audit Rose, dibangun dari audit source **Trading Backtest Bitget.zip** dan aturan trading dalam percakapan. Mulai membaca di sini. Runtime baru berada di `moonshot_scanner/`; source lama di `reference/original_source/` hanya bahan audit dan sengaja memakai ekstensi `.txt`.

**Status paket:** implementasi lokal dan pengujian offline/live public-data tersedia. Belum dideploy ke Render, belum tersambung ke akun Telegram, belum ada pengiriman nyata. Bukan mesin profit yang sudah terbukti. Tidak ada fungsi membuka/menutup order, wallet, deposit atau transfer.

## Perubahan utama

- Bitget, Binance, Bybit public market data; tiap venue/produk tetap terpisah. Universe crypto disaring dari tokenized stocks/RWA dan diputar antar siklus, bukan hanya mengejar top gainers.
- Discovery tetap terlihat untuk coin dengan histori pendek. Identitas tidak diketahui tidak dianggap aman atau fundamental kuat.
- CoinGecko ID dikurasi; DEX harus cocok chain, kontrak base **dan quote**, pool dideduplikasi, perbedaan harga dan reserve mencurigakan ditandai. Pool TVL bukan likuiditas keluar yang dapat dieksekusi.
- Candle UTC dengan open/close time dan penanda closed/developing. EMA13/21/50/200, RSI, ATR, pivot, kompresi dan TA4h terhadap median 42 candle sebelumnya. Konfirmasi tidak memakai candle berjalan.
- Discovery terpisah dari entry. Rencana pullback/breakout terpisah; long/short dicerminkan; stop mengikuti struktur; target harus level historis yang defensibel; minimum **2R setelah asumsi biaya/slippage**. Tidak ada runner 3x yang dikarang.
- OI disimpan dalam base units, baseline 4h/24h selaras waktu; startup belum memiliki histori berarti WARMUP, bukan delta 0%. Funding menyertakan interval sebenarnya dan jadwal settlement.
- Spread, depth, slippage pada notional referensi dan flow sampel berlabel jelas. Tidak mengarang CVD atau liquidation tape.
- Regime BTC/ETH, return correlation/beta tersinkron dan residual strength; tidak mengklaim coin kebal crash BTC.
- Bukti supply/unlock, katalis, macro dan transfer disimpan dengan sumber, masa berlaku dan scope aset. Data hilang tetap terlihat dan dapat menahan READY.
- SQLite WAL untuk job, snapshot, OI, watchlist dan outbox; level watch immutable. Worker scan, jadwal, watch dan delivery berjalan terpisah; restart recovery, dedup dan alarm gagal.
- Sesi Armenia/WIB dengan DST London/New York; jam tidak mendapat skor arah tetap. Watcher MTProto Rose yang sudah dibuat di chat diintegrasikan melalui `ROSE_ANALYSIS_ENGINE=moonshot_scanner`.
- HTTP feed dan MCP read-only untuk konsumen seperti Mira, tanpa izin trading. Akses Mira belum diuji dari akun Anda.
- Backtest replay menerima sinyal historis eksplisit, memakai next-bar fill, biaya, stop-first saat urutan intrabar ambigu, gap stop dan equity awal. Tidak ada sinyal berarti NOT_RUN. Model bobot otomatis ditahan dalam SHADOW_ONLY; tidak menampilkan confidence palsu.

## Jalankan lokal

Python 3.11+:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SCANNER_TOKEN = 'isi-token-acak-panjang-anda'
python -m moonshot_scanner --serve-local
```

Buka `http://127.0.0.1:8000`, masukkan token. Server lokal ini hanya untuk pemeriksaan. File `.env.example` adalah contoh; tidak dibaca otomatis. Gunakan process environment atau Render Environment untuk konfigurasi.

Scan satu kali tanpa delivery:

```powershell
python -m moonshot_scanner --once --exchange bitget --symbols EVAAUSDT,BLESSUSDT
python -m moonshot_scanner --status
python -m unittest discover -s moonshot_scanner/tests
python -m unittest rose_monitor.test_monitor rose_monitor.test_push
python -m rose_monitor.engine_selfcheck
```

## Deploy Render sendiri

Upload **isi folder ini** ke root repo GitHub, bukan seluruh workspace sebelumnya. Render membaca `render.yaml`. Build `pip install -r requirements.txt`; start `python -m moonshot_scanner.supervise`.

**Blueprint yang disediakan memakai instance berbayar `starter` dan disk persisten 1 GB. Menerapkannya dapat menimbulkan biaya Render; paket ini tidak membeli atau membuat layanan.** Periksa biaya di dashboard sebelum membuat layanan. Pilih free hanya untuk demo manual: jangan mengklaim watcher 24/7 atau histori tahan restart pada free/ephemeral storage.

Blueprint menyimpan state di `/var/data/moonshot`, mengaktifkan auto-scan 5 menit, dan membiarkan watcher Rose OFF sampai akun diotorisasi. Token dashboard/read-only/session secret dibuat Render; ambil token dashboard di Environment secara pribadi. Auto-deploy dimatikan supaya perubahan GitHub tidak langsung mengganti produksi tanpa pemeriksaan.

`SCANNER_TRUST_PROXY=1` hanya ketika aplikasi berada di belakang satu ingress terpercaya seperti Render. `SCANNER_TRUSTED_HOSTS` dapat diisi domain sendiri; di Render hostname diambil dari `RENDER_EXTERNAL_HOSTNAME`. Untuk direct local gunakan proxy trust OFF. Jangan mempublikasikan secret, session Telegram, database atau file `.env` ke GitHub.

## Aktifkan laporan dan Rose

1. Set `SCANNER_TELEGRAM_BOT_TOKEN` dan `SCANNER_TELEGRAM_CHAT_ID` secara pribadi untuk laporan scanner. Buka chat dengan bot terlebih dahulu. Webhook HTTPS opsional: `SCANNER_WEBHOOK_URL`.
   Untuk menemukan chat ID dan menguji bot secara lokal, jalankan `python -m rose_monitor.configure_delivery --output .env.telegram.local --test`. Token dimasukkan tersembunyi, bukan pada URL browser atau chat. Helper menulis `ROSE_BOT_TOKEN` / `ROSE_CHAT_ID`; jika bot yang sama dipakai scanner, masukkan nilainya juga ke `SCANNER_TELEGRAM_BOT_TOKEN` / `SCANNER_TELEGRAM_CHAT_ID` di Render Environment. File helper tidak otomatis dibaca runtime. `.env.*` dan `*.env` diabaikan Git; tetap jangan membagikan file itu. Jangan jalankan pengambilan update bersamaan dengan receiver lain milik bot tersebut.
2. Untuk Rose, set `ROSE_TG_API_ID`, `ROSE_TG_API_HASH`, `ROSE_BOT_TOKEN`, `ROSE_CHAT_ID`; akun sumber harus sudah menjadi anggota channel. Bot pengiriman bukan pengganti akun sumber.
3. Otorisasi lokal: `python -m moonshot_scanner --authorize-rose`. OTP/2FA dimasukkan lokal, jangan dikirim ke chat ini. Pindahkan file session secara privat ke folder `ROSE_STATE_DIR` pada disk persisten. Default server: `<SCANNER_STATE_DIR>/rose`.
4. Baru set `SCANNER_ROSE_ENABLED=1`. Supervisor menolak startup watcher tanpa session dan konfigurasi lengkap. `ROSE_DAILY_ENABLED=0`; watcher memproses event new/edit, bukan laporan harian pengganti listener.
5. Buktikan satu event baru diterima, dianalisis dan dikirim ketika laptop/browser mati. Uji restart, catchup, edit dan duplikasi. Lihat `/api/health`, `/api/rose/recent` serta log sebelum menyebutnya aktif.

Telegram delivery bersifat **at-least-once**: dedup antrean tidak bisa menjamin tidak ada pengiriman ulang jika server mati setelah Telegram menerima tetapi sebelum acknowledgement tersimpan. Event ID memungkinkan identifikasi ulang. Webhook menyertakan Idempotency-Key.

`/healthz` hanya membuktikan web hidup. `/readyz` memeriksa worker. `/api/health` menampilkan status worker, kanal, bukti delivery dan sumber Rose secara terpisah. `ready_for_24_7` mengacu worker pasar + persistent state + auto-scan + bukti delivery, bukan bukti Rose sudah tersambung atau model profit sudah tervalidasi.

## Hubungkan Mira

Endpoint sesudah deploy: `https://DOMAIN-ANDA/mcp`, Streamable HTTP stateless, auth header `Authorization: Bearer SCANNER_READ_TOKEN`. Tools: `market_latest`, `scanner_health`, `session_brief`, `rose_recent`. Semua hanya membaca. HTTP alternatif `/api/feed/latest`. Token baca tidak bisa menjalankan scan atau mengubah bukti.

Server lokal diuji dengan initialize/tools/list/tools/call. Penerimaan transport/header oleh akun Mira dan aktivasi connector masih harus diuji di Mira; paket ini tidak mengubah paket langganan atau skill Mira.

## Bukti dan batas cakupan

Registry awal berisi **11 project terverifikasi**, bukan seluruh pasar. Memperluas universe CEX tidak otomatis memverifikasi identitas/fundamental setiap coin. Tambahkan pemetaan ke `asset_registry.json` hanya dengan sumber yang benar; jangan mengambil kontrak berdasarkan ticker/logo/TVL terbesar.

Supply/unlock, berita, VC, whale attribution dan kalender macro **belum memiliki feed universal otomatis**. Operator dapat menambahkan bukti bersumber melalui POST `/api/evidence`; `verified` berarti pernyataan review operator, bukan verifikasi otomatis dari sekadar URL. Format contoh ada di `docs/EVIDENCE_AND_ACCEPTANCE.md`. DEX-first discovery, OCR chart, model global-politik terkalibrasi dan integrasi Mira langsung tidak diklaim aktif. Jangan menyembunyikan batas ini dengan skor tinggi.

Dokumentasi perbandingan enam repo dan audit awal ada di `docs/`. CCXT, Freqtrade, Cryptofeed, NautilusTrader, Hummingbot dan Telethon menjadi referensi desain; framework trading tersebut tidak disalin/diaktifkan. Tidak ada klaim bahwa sistem telah diuji profitabel sampai 2027. Perlu prospective paper tracking dan evaluasi out-of-sample sebelum menganggap ranking sebagai edge.

## Migrasi dari v2

Gunakan konfigurasi baru `SCANNER_*`, bukan start command lama `moonshot_web:app`. Jangan impor `.moonshot_oi_state.json` sebagai histori OI 4h/24h: waktunya belum memenuhi kontrak baru. Jangan impor bobot learned lama sebagai probabilitas. Cache CoinGecko lama berbasis ticker, hasil scan lama dan secret tidak disertakan runtime. Reference source disimpan sebagai `.txt` untuk audit, bukan jalur fallback yang diam-diam berjalan.

Simpan backup konsisten SQLite dengan SQLite backup API atau ketika semua proses berhenti; menyalin hanya file utama saat WAL aktif dapat menghilangkan transaksi. Satu disk/SQLite ini untuk satu host. Jangan memakai dua service Render terpisah yang menganggap satu disk lokal bisa dibagi.

Retensi dibatasi agar candle berulang tidak memenuhi disk: 300 snapshot terbaru menyimpan candle lengkap, chart terakhir setiap aset dipertahankan, maksimal sekitar 5.000 snapshot ringkasan riset, dan OI 35 hari. Job selesai/error/outbox juga dibatasi. Ini bukan arsip seluruh histori selamanya. Ekspor/backup sebelum batas retensi bila diperlukan untuk riset jangka panjang. Event Rose dan evidence perlu pemantauan kapasitas tersendiri; ukuran disk dan laju post menentukan kebutuhan arsip berikutnya.
