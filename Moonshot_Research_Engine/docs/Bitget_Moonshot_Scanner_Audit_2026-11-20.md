# Audit Bitget Moonshot Scanner — 20 November 2026

Kesimpulan: aplikasi berjalan dan dapat menjadi radar awal, tetapi rencana entry, identitas data DEX, serta klaim confidence belum cukup andal untuk menjadi dasar posisi futures. Audit ini memeriksa aplikasi yang disajikan server, respons API, dan JavaScript halaman. Source backend deployment, dashboard Render, dan akun Mira tidak diperiksa.

## Waktu dan cakupan

- Pemeriksaan utama: 03:40–03:45 UTC = 07:40–07:45 Armenia = 10:40–10:45 WIB, 20 November 2026.
- Satu scan eksplisit delapan simbol dengan fundamentals/on-chain aktif, min_asymmetry=0 untuk melihat hasil tanpa penyaringan skor.
- Satu scan auto-discovery enam simbol dengan filter UI default: turnover $500 ribu–$50 juta, min_asymmetry=55.
- Total: 8 hasil, 5 error, 1 kandidat tersaring dari 14 simbol yang dicoba. Ini pengujian fungsi, bukan backtest profitabilitas atau estimasi peluang naik.
- Endpoint dibaca: /api/market, /api/session, /api/learn/stats, /api/watch, /api/alerts, /api/candles, dan /api/scan.
- Tidak menambahkan watchlist, mengevaluasi/mengubah bobot, mengaktifkan auto-scan, atau mengirim notifikasi. Tidak menjalankan order atau akses akun exchange.
- Token akses tidak dicantumkan dalam laporan atau snapshot yang disimpan.

## Yang terbukti bekerja

- Halaman dan API merespons HTTP 200; hasil scan dikirim bertahap sebagai NDJSON.
- Data harga, OHLC, OI saat ini, funding, book imbalance, dan recent taker flow tersedia dalam payload hasil.
- Endpoint market melaporkan BTC/ETH dan regime RISK_OFF. Harga BTC berbeda antara kedua scan, sehingga bukan satu contoh statis yang identik.
- Funding EVAA 0.00005 pada payload konsisten dengan endpoint publik Bitget saat pemeriksaan. Artinya 0.005%; endpoint Bitget juga mengungkap interval 4 jam, informasi yang belum disertakan payload scanner.
- EVAA diberi CHASING (late), RSI sekitar 78.48, kenaikan 24 jam +41.75%, dan verdict AVOID / WAIT. Perlindungan mengejar harga muncul dalam hasil ini.
- Asymmetry dan trade quality dipisah. Backtest berjumlah kecil diberi NONE/INSUFFICIENT. Liquidation diberi nama proxy.

## Temuan prioritas

### P1 — Data DEX bercampur antar token dengan simbol sama

EVAA pada scanner:

- dex_chain: solana
- dex_liquidity_usd: 30,919,355.53
- dex_pair_age_days: sekitar 22.47

Dokumentasi resmi EVAA mencantumkan kontrak TON dan BSC; kontrak BSC adalah 0xaa036928c9c0Df07d525B55ea8EE690Bb5a628C1. Kontrak Solana dalam hasil pencarian belum terbukti merupakan representasi resmi EVAA.

Pencarian DEX Screener EVAA mengembalikan 30 pool dengan berbagai alamat token. Pool Solana terbesar yang ditemukan bernama EVAA Protocol, dengan harga sekitar $1.44, sedangkan kontrak BSC yang dicantumkan proyek sekitar $5.52 dan konsisten dengan harga CEX scanner $5.524. Likuiditas 21 pool BSC dengan kontrak resmi yang ada dalam hasil pencarian berjumlah sekitar $584,679.81; ini bukan klaim total likuiditas EVAA lintas semua chain/pool.

Bukti yang lebih kuat: scanner menampilkan likuiditas DEX SOL sebesar $32,502,594,966.39. Angka tersebut SAMA PERSIS dengan jumlah liquidity.usd seluruh 30 hasil search SOL, termasuk banyak baseToken.address berbeda. Karena itu jumlah tersebut bukan metrik likuiditas SOL yang identitasnya sudah diverifikasi. NEAR juga memiliki jumlah sekitar $13.074 miliar yang hampir identik dengan penjumlahan hasil pencarian simbol.

Inferensi dari kecocokan angka: backend kemungkinan menjumlahkan hasil pencarian berdasarkan simbol. Implementasi pastinya memerlukan source backend. Yang terverifikasi adalah hasilnya tercemar token yang tidak memiliki identitas terkonfirmasi.

Perbaikan: petakan exchange asset ke project ID dan chain + kontrak resmi; terima hanya pool dari token yang telah diverifikasi; deduplikasi pool; simpan pair address, kontrak, URL sumber, waktu, dan perbedaan harga terhadap CEX. Jika pemetaan belum tersedia, status UNKNOWN, bukan LOW RISK. Likuiditas pool tetap perlu dibedakan dari jumlah yang benar-benar dapat dieksekusi pada batas slippage tertentu.

### P1 — Label SHORT tetapi panel rencana tetap LONG

Contoh NEAR:

- direction=SHORT, direction_score=-55
- entry_low=3.0151967; entry_high=3.0895900
- stop=2.8116815
- tp1=3.4770000; tp2=3.7745290
- teks trigger: Enter on 4H close above ... OR scale a limit bid ...

Ini geometri long. ICP dan LINK juga berlabel SHORT tetapi memiliki stop di bawah entry dan target di atas. Jika direction dimaksudkan hanya sebagai bias, namanya perlu market_bias dan rencana perlu memiliki plan_side tersendiri. Jika benar-benar short, level dan teks harus dihitung ulang.

Perbaikan: LONG mewajibkan stop < entry < tp1 < tp2; SHORT mewajibkan tp2 < tp1 < entry < stop. AVOID/WAIT tidak boleh disajikan seolah rencana entry siap dieksekusi.

### P1 — Entry pullback dan breakout memakai satu set RR/target

BLESS:

- midpoint zona pullback sekitar 0.01134832
- trigger breakout=0.0193847
- TP1=0.0193847, sama dengan trigger
- TP2=0.01611462, lebih rendah daripada TP1 dan harga entry breakout
- RR TP1 yang ditampilkan=5.058, dihitung dari zona bawah, bukan entry breakout

Jika masuk pada breakout tersebut, RR ke TP1 adalah 0 dan TP2 sudah berada di bawah entry. EVAA, NEAR dan ICP juga memiliki trigger sama dengan TP1. ICP memiliki TP2 di bawah TP1. Ini berlaku pada payload yang diuji; bukan proyeksi harga.

LINK dan SOL menampilkan RR TP1 sekitar 1.5 dari zona pullback, di bawah minimum 2R rulebook. Setelah masuk pada trigger breakout, RR TP1 lebih kecil lagi: sekitar 0.082R untuk LINK dan 0.637R untuk SOL sebelum biaya.

Perbaikan: pisahkan dua objek rencana, masing-masing dengan entry aktual, invalidation, target terurut, biaya/slippage, expiry dan RR tersendiri. Evaluasi minimum RR dengan definisi exit yang eksplisit; jangan memperbaiki RR hanya dengan menaikkan target secara mekanis.

### P1 — Fundamentals aktif tetapi seluruh hasil UNKNOWN

Kedelapan hasil, termasuk SOL/LINK/NEAR/ICP, memiliki market_cap_usd, fdv_usd, circulating_pct, dan chg_30d_pct=null serta cap_tier=UNKNOWN. Penyebabnya belum diketahui: bisa koneksi, rate limit, mapping, atau error penanganan data. Tidak ada error per sumber yang menjelaskannya.

Karena itu skor asymmetry saat pemeriksaan tidak dapat diperlakukan sebagai penilaian small cap/FDV/supply yang lengkap. Payload juga belum menyajikan bukti unlock, penerima vesting, revenue/adopsi, VC, treasury, atau katalis dari sumber primer.

Perbaikan: tambahkan source health, coverage, fetch status dan umur data. Tampilkan FUNDAMENTALS UNAVAILABLE secara mencolok. Jangan mengganti data tidak diketahui dengan asumsi bullish atau rata-rata skor yang tampak lengkap.

### P2 — Confidence belum terkalibrasi; learning belum memiliki bukti

- Semua 8 hasil memiliki confidence=60.
- /api/learn/stats sebelum dan sesudah scan pertama menunjukkan total_predictions=0, predictions=[], stats={}.
- Seluruh bobot=1.0.
- Konfigurasi menampilkan min_samples=15, horizon_hours=72, hit_threshold_pct=8.
- Backtest per hasil hanya 0–3 trade.

Confidence 60% tidak boleh diterjemahkan menjadi peluang profit 60%. Nilai itu belum didukung kalibrasi historis atau uji out-of-sample yang ditunjukkan aplikasi. Tidak adanya jurnal untuk kandidat AVOID bisa disengaja; audit ini tidak menyimpulkan journaling rusak.

Perbaikan: gunakan nama heuristic confidence/data quality jika itulah maknanya; jangan tambahkan tanda probabilitas. Pisahkan event hit +8%, return akhir, TP-before-SL, entry terisi atau tidak, biaya/funding, dan expectancy. Uji model/bobot pada periode masa depan yang tidak dipakai melatih, dengan split waktu, pencegahan overlap label, baseline, serta interval ketidakpastian. Ambang 15 observasi sendiri tidak membuktikan edge.

### P2 — Backtest drawdown mencurigakan

ICP memiliki 1 trade, win rate 0%, expectancy -1.068458R, tetapi max drawdown 0R. Dengan modal awal sebagai puncak equity, satu trade rugi harus menghasilkan drawdown. NEAR memiliki 3 trade, win rate 0%, expectancy -1.051520R, tetapi max drawdown hanya 2.099838R, lebih kecil dari kerugian total sekitar 3.154561R.

Ini menunjukkan kemungkinan titik awal equity tidak dimasukkan ke running peak. Penyebab kode perlu diperiksa, tetapi angka drawdown ini belum layak dipakai mengukur risiko.

Perbaikan: awali equity curve dengan 0R sebelum trade pertama; uji satu loss, tiga loss, profit lalu loss, serta urutan TP/SL intrabar yang ambigu. Jangan menganggap backtest kecil sebagai bukti trading edge.

### P2 — Status candle dan umur data belum eksplisit

/api/candles BLESS mengembalikan 200 candle 4H. Candle terakhir dimulai 20 November 00:00 UTC dan baru selesai 04:00 UTC; pemeriksaan berlangsung sekitar 03:43 UTC. Jadi candle terakhir masih DEVELOPING. Respons hanya memiliki time, OHLC dan volume, tanpa close_time/is_closed, category, source observation timestamp, atau fetch age.

Ini membuktikan chart memuat candle berjalan; BELUM membuktikan engine konfirmasi memakai candle berjalan karena source backend deployment belum tersedia.

Perbaikan: metadata venue/produk/granularity, open_time, close_time, is_closed dan observed_at/fetched_at wajib tersedia. Konfirmasi hanya memakai candle selesai; candle berjalan dilabeli DEVELOPING. TA4h dibanding median tujuh hari harus memakai quote turnover dan candle berdurasi sama. Definisi vol_wakeup_ratio saat ini belum dapat diverifikasi dari payload.

### P2 — Auto-discovery membuang sebagian kandidat dengan histori pendek

Auto-discovery memilih CTUSDT, TRUSTUSDT, RDKILYUSDT, RGYREUSDT, DOSUSDT, ARIAUSDT.

- CT gagal karena hanya memiliki 52 candle 1D.
- RDKILY gagal dengan 75 candle 1D.
- RGYRE gagal dengan 76 candle 1D.
- ARIA tersaring.
- TRUST dan DOS menghasilkan payload, tetapi keduanya AVOID/WAIT.

Tidak cukup histori perlu menahan status READY dan indikator panjang, tetapi tidak perlu menghapus kandidat dari tahap discovery. Mesin sebaiknya tetap menampilkan EARLY / HISTORY INSUFFICIENT. Jenis instrumen juga perlu eksplisit agar universe crypto tidak diam-diam bercampur dengan produk lain.

AKEUSDT dan PIEVERSEUSDT gagal pada ticker SPOT Bitget dalam scan eksplisit. Itu keterbatasan pasangan/venue yang diuji, bukan bukti aset tidak memiliki pasar di tempat lain.

### P2 — Dua filter frontend dapat memberi kesan aman/siap yang keliru

Pada JavaScript halaman yang disajikan server:

1. Filter Low liq-risk lolos ketika !r.onchain_ok. Artinya ketiadaan data DEX dapat lolos sebagai kandidat risiko likuiditas rendah.
2. Filter Breakout ready menggunakan ((phase EARLY_BREAKOUT atau COILED_EARLY) dan verdict BUY) ATAU verdict STRONG. Karena OR terakhir berada di luar syarat phase, STRONG dapat lolos tanpa phase yang diminta.

Perbaikan: UNKNOWN menjadi kategori sendiri. READY harus memerlukan satu fungsi gate bersama yang mencakup data freshness, entry trigger, regime, invalidation, dan RR; bukan sekadar substring BUY/STRONG.

### P2 — Otomasi dan pengiriman belum aktif

/api/alerts melaporkan:

```json
{"autoscan_min": 0.0, "channels": {"telegram": false, "webhook": false}, "events": []}
```

/api/watch kosong. Tombol Auto-refresh di frontend hanya memanggil checkWatch() dan loadAlerts() setiap 60 detik selama halaman berjalan; bukan menjalankan scan market-wide dan bukan listener Rose.

Status ini berarti tidak ada auto-scan atau kanal Telegram/webhook yang aktif menurut API saat pemeriksaan. Belum ada bukti sumber Rose -> penerimaan event -> analisis -> pengiriman yang berfungsi.

Jika instance menggunakan Render Free, layanan dapat tidur setelah 15 menit tanpa traffic masuk dan file lokal dapat hilang saat restart/redeploy/spin-down. Paket hosting dan penyimpanan instance ini belum diperiksa. Jangan menganggap domain onrender.com sendiri membuktikan penggunaan Free atau Paid.

Perbaikan: worker persisten, database tahan restart, heartbeat/error alarm, deduplikasi/catchup, dan uji pengiriman saat browser/laptop mati. Integrasi Mira harus diuji terpisah dengan hak baca dan format data yang benar; keberadaan endpoint HTTP tidak otomatis membuktikan Mira telah terhubung.

## Snapshot hasil — bukan rekomendasi posisi

| Simbol | Quality | Asymmetry | Arah yang dilabel | Verdict | Catatan |
|---|---:|---:|---|---|---|
| BLESS | 51 | 51 | NEUTRAL | AVOID / WAIT | TP1 = breakout; TP2 < TP1 |
| EVAA | 43 | 39 | LONG | AVOID / WAIT | CHASING, +41.75% 24h; identitas DEX bermasalah |
| NEAR | 35 | 61 | SHORT | AVOID / WAIT | Panel rencana tetap long |
| ICP | 31 | 51 | SHORT | AVOID | Panel long; max DD 0R pada satu loss |
| LINK | 34 | 51 | SHORT | AVOID | Panel long; TP1 1.5R dari zona |
| SOL | 39 | 52 | NEUTRAL | AVOID / WAIT | Total DEX persis sama dengan 30 hasil search berbeda alamat |
| TRUST | 62 | 61 | LONG | AVOID | COILED_EARLY bukan berarti READY |
| DOS | 57 | 70 | LONG | AVOID / WAIT | COILED_EARLY; cap UNKNOWN |

Semua hasil dilaporkan dalam regime BTC RISK_OFF. Tidak ada kandidat READY/BUY dalam pengujian ini. Skor tabel berasal dari aplikasi yang sedang diaudit, bukan penilaian independen atas kualitas proyek.

## Sumber dan bukti

- Aplikasi: https://bitget-moonshot-scanner.onrender.com/ (akses memakai token yang diberikan pengguna; tidak disalin ke laporan).
- Kontrak resmi EVAA: https://evaa.gitbook.io/evaa-docs-2.0/details-of-protocol/token-and-tokenomics/usdevaa-token
- Dokumentasi DEX Screener: https://docs.dexscreener.com/api/reference
- Funding Bitget: https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market
- Hosting Render: https://render.com/docs/free
- Snapshot lokal tersanitasi: ../work/scanner_review/page.html, test_scan.ndjson, auto_scan.ndjson, market.json, session.json, learn.json, learn_after.json, watch.json, alerts.json, candles_bless.json, dex_evaa.json, dex_near.json, dex_sol.json, bitget_evaa_funding.json.

Tidak ada penilaian probabilitas next-call, tidak ada prediksi moonshot berdasarkan skor yang belum divalidasi, dan tidak ada perubahan pada aplikasi deployment.
