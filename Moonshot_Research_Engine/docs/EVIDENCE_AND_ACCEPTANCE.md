# Evidence contract dan acceptance sebelum produksi

POST `/api/evidence` membutuhkan admin Bearer token. Contoh bentuk data (contoh di bawah bukan bukti tentang aset nyata):

```json
{"symbol":"TESTUSDT","kind":"unlock","observed_at":"2026-11-20T00:00:00Z","expires_at":"2026-11-21T00:00:00Z","source_url":"https://issuer.example/tokenomics","verified":true,"facts":{"lookahead_days":30,"circulating_pct_unlock_30d":0.5,"recipients":["team"],"notes":"Review official schedule and transfers separately"}}
```

Kinds: unlock, catalyst, fundamental, whale_transfer, security, macro. Wildcard `*` hanya untuk macro; tidak dapat meluluskan unlock semua coin. Evidence di masa depan/kedaluwarsa tidak fresh. `ACTIVE DISTRIBUTION CONFIRMED` memerlukan bukti transfer beridentitas yang cocok asetnya; transfer ke exchange tetap bukan bukti order jual sudah dieksekusi.

Acceptance runtime:

1. Paket dapat diinstall dan semua tes offline lulus. Python/dependency versions dan snapshot live dicatat.
2. Data dari masing-masing venue memiliki observed/fetched time; candle harian 00 UTC; developing candle tidak dipakai konfirmasi; data rusak/stale menahan READY.
3. Satu scan gagal tidak menghilangkan kandidat lain atau errornya. Coin baru dengan histori pendek tetap bisa dipantau.
4. Long/short, zona entry, stop, target dan biaya konsisten. Target tidak dipindahkan untuk membuat RR terlihat bagus.
5. OI warming-up ditampilkan sampai baseline 4h/24h benar-benar tersedia. Tidak memakai USD-notional change sebagai base OI change.
6. Stop proses di tengah job, hidupkan kembali; hasil/job tidak hilang. Uji outbox retry dan edit event dedup. Jangan menganggap mock delivery sebagai tes Telegram nyata.
7. Kirim satu notifikasi nyata ke penerima sendiri; simpan bukti event ID. Uji source Rose saat laptop mati dan setelah restart; source yang belum diuji tetap NOT_CONNECTED/NOT_VERIFIED.
8. Verifikasi disk persisten dengan tulis record, redeploy/restart, baca record sama. Boolean konfigurasi saja tidak membuktikan mount tersedia.
9. Jadwal London 07:45 pada Nov20 = 11:45 AMT / 14:45 WIB. Tes perbedaan DST AS dan UK Maret2027.
10. Paper tracking prospektif: simpan discovery asli, rencana sebelum entry, fill/expiry, TP-before-SL, biaya/funding dan return distribusi. Optimasi bobot tidak masuk live tanpa split waktu/purge, baseline dan evaluasi data yang belum dipakai melatih.

Kriteria di atas meliputi integritas sistem. Tidak menjanjikan moonshot, win rate, atau hasil minimum investasi.
