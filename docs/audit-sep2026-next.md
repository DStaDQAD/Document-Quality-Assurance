# Audit SULNI & SK September 2026: perbaikan berikutnya

Tanggal audit: 2026-10-14. Dokumen ini adalah titik awal sesi perbaikan berikutnya.

## Latar belakang

Tiga pemeriksaan nyata dijalankan: dokumen Word vs semua sheet Excel, mode `excel`, cek grafik mati, `AI_REVIEW=1`. Hasilnya sudah disalin ke Riwayat produksi (Neon).

| Dokumen | Klaim | Sesuai | Tidak Sesuai | Tidak Cukup Data |
|---|---|---|---|---|
| SULNI: Siaran Pers ULN Sept'26 (data Juli 2026) | 17 | 14 | 0 | 3 |
| SK: Laporan Survei Konsumen September 2026 | 90 | 83 | 0 | 7 |
| SK: Narasi SK September 2026 | 7 | 7 | 0 | 0 |

Setiap vonis lalu diaudit secara mandiri. Setiap sel dibaca ulang dengan pembaca openpyxl sendiri (bukan parser aplikasi), baris yang dipilih dinilai manual, dan klaim yang belum terjawab dihitung manual.

- **Benar:** ke-104 vonis Sesuai benar, dan tidak ada Tidak Sesuai palsu.
- **Tidak Cukup Data:** 9 klaim sebenarnya benar, dan 1 klaim memang tepat Tidak Cukup Data.
- **Terlewat:** 1 klaim benar hilang saat ekstraksi.
- **Belum teruji:** ketiga dokumen tidak mengandung kesalahan, jadi **kemampuan menemukan Tidak Sesuai belum diuji**.

Bahan audit ada di `eval/cases/audit_sep2026/` (di-ignore git karena data internal). Isinya: hasil JSON ketiga pemeriksaan, log, `run_check.py` (runner), `audit.py` (pembaca sel mandiri), dan `reask.py` (tanya ulang Tinjauan AI). Path di skrip masih menunjuk ke `%TEMP%\claude\check\`, jadi sesuaikan dulu sebelum dipakai. Berkas sumber ada di `sample_data/sharepoint/sk_sep26/` dan `sample_data/sharepoint/sulni_tabel/`.

## Klaim uji (kebenaran menurut tabel, sudah dicek manual)

| ID | Klaim (ringkas) | Vonis sistem | Kebenaran | Penyebab |
|---|---|---|---|---|
| SULNI #4 | "peningkatan ULN publik" | TCD | Sesuai: baris "Pemerintah dan Bank Sentral" 258.752,0 → 260.282,9 | padanan istilah; Tinjauan AI tidak stabil (berhasil saat ditanya ulang) |
| SULNI #16 | pangsa 4 sektor ULN swasta 80,6% | TCD | Sesuai: (53.380,0 + 39.306,7 + 35.800,6 + 28.247,8) / 194.476,9 = 80,59% | belum ada operasi "jumlah beberapa baris ÷ total" |
| SULNI #17 | rasio ULN/PDB 30,7% "Juli 2026" | TCD | **TCD tepat**: TabI.7 hanya per triwulan (Q2-26 = 30,63) | alasan yang ditulis sistem kurang tepat ("PDB tidak ditemukan") |
| SULNI (hilang) | lembaga keuangan kontraksi 0,3% (yoy) | tidak diekstrak | Sesuai: 38.670,4 vs 38.794,5 = −0,32% | bug: "−0,3" dibuang `_finalize_facts` karena tidak ada di teks |
| SK [32] | IKLK optimis hanya pada usia 20–30, usia lainnya pesimis | TCD | Sesuai: 109,2 vs 99,9 / 99,8 / 96,3 / 85,0 | klaim campuran; ekstraktor salah label "IKLK > SMA" |
| SK [68] | IEKLK naik di seluruh jenjang kecuali akademi/diploma (turun ke 127,5) | TCD | Sesuai | kuantor "seluruh … kecuali" |
| SK [71] | IEKLK naik di sebagian besar kelompok usia | TCD | Sesuai: 3 dari 5 naik | kuantor "sebagian besar" |
| SK [74], [75] | IEKU naik di usia 20–40, kelompok lainnya turun | TCD | Sesuai | kelompok digabung dalam satu klaim; "lainnya" |
| SK [87] | porsi konsumsi naik di seluruh kelompok kecuali Rp1–2 jt (77,2%) | TCD | Sesuai | kuantor |
| SK [90] | porsi tabungan turun di sebagian besar kelompok | TCD | Sesuai: 4 dari 5 turun, 1 tetap | kuantor |

**Temuan Tinjauan AI saat ditanya ulang** (detail di `reask.py`). Dari 7 klaim berkuantor di SK:

- **4 jawaban AI salah atau bertentangan dengan alasannya sendiri**, dan semuanya **ditahan oleh pengaman kode**:
  - [68]: AI salah membaca "kecuali";
  - [71]: AI menafsirkan "sebagian besar" sebagai "semua";
  - [74], [75]: vonis bertentangan dengan alasan AI sendiri.
- **2 jawaban keliru** "periode tidak jelas" ([87], [90]).
- **1 jawaban benar tapi ditolak kode** ([32]), karena mesin penghitung hanya bisa satu operasi untuk satu baris.

Penyebab dari sisi desain:
- *thinking budget* 1024 untuk batch berisi sampai 30 klaim;
- kolom `verdict` diisi sebelum `explanation`;
- format jawaban hanya untuk klaim satu baris;
- label ekstraktor yang keliru ikut dikirim sebagai petunjuk.

## Aturan main (WAJIB)

1. **Umum, bukan khusus kasus.**
   - Tidak boleh ada nama publikasi, indikator, kota, atau kelompok yang ditulis langsung di aturan kode atau prompt untuk menambal kasus di atas.
   - Daftar kelompok diambil dari **struktur tabel** (baris saudara di bawah induk yang sama), bukan dari daftar tetap.
   - Contoh di prompt harus berasal dari minimal dua publikasi berbeda, atau berupa contoh netral.
   - Uji unit memakai tabel sintetis yang bukan data BI.
2. **Tidak boleh mengubah yang sudah benar menjadi salah.** Sebelum mengubah apa pun, rekam **baseline**:
   - `pytest`;
   - `python -m eval.run_comparison_eval --fail-under 1.0`;
   - uji golden publikasi (`tests/test_publication_golden.py`, hanya lokal);
   - `python -m eval.run_ai_review_eval judge --tag t1024a` (memutar ulang jawaban tersimpan, tanpa biaya);
   - pemeriksaan e2e ke-12 publikasi ditambah ketiga dokumen di atas, dengan vonis per klaim disimpan.

   Setelah perubahan, jalankan semuanya lagi dan bandingkan **per klaim**:
   - Perubahan yang diharapkan hanya **Tidak Cukup Data → Sesuai** untuk klaim di tabel atas, ditambah klaim kontraksi yang kembali muncul.
   - Setiap **Sesuai → apa pun**, dan setiap **Tidak Sesuai baru**, harus dijelaskan satu per satu dan disetujui pengguna.
   - Ingat variasi LLM: bandingkan dengan baseline yang dijalankan di hari yang sama, dengan `git worktree` dari main.
3. **Deteksi Tidak Sesuai tetap jalan.** Uji dengan dokumen berisi kesalahan sengaja (misalnya `sample_data/test case salah_*`, ditambah beberapa kesalahan sisipan pada klaim berkuantor: satu kelompok dibalik arahnya, atau "seluruh" padahal ada yang turun). Semua kesalahan sisipan harus tertangkap sebagai Tidak Sesuai.
4. **Prinsip arsitektur tetap:** AI hanya mengurai kalimat atau memetakan, kode yang menalar dan menghitung. AI tidak boleh memberi vonis sendiri.

## Daftar perbaikan

### A. Ekstraksi klaim
- **A1 (tinggi). SELESAI 2026-10-14.** Klaim kontraksi tidak boleh terbuang. Saat memeriksa anchor atau nilai di `_finalize_facts`, terima juga nilai tanpa tanda minus bila klaimnya kontraksi (aturan 2b). Pastikan nilai bertanda tetap dipakai untuk perbandingan.
  - Perbaikan: pengaman "angka tidak ada di teks" juga mencari angka tanpa tanda minus di depan (berlaku juga untuk `diff` turun di aturan 2f). `claimed_value` tetap bertanda. Uji: `test_extraction_keeps_a_contraction_whose_minus_sign_the_prompt_added` dan `test_extraction_still_drops_a_negative_number_that_is_nowhere_in_the_text`.
  - Cek ulang: pytest 1428 lulus, comparison eval 100%, golden 187 lulus, `judge --tag t1024a` tidak berubah. SULNI e2e: 18 klaim (16/0/2); "lembaga keuangan kontraksi 0,3%" kembali dan **Sesuai** (−0,32%). Tidak ada Sesuai yang berubah; selisih lain hanya variasi label LLM. "ULN publik" kali ini Sesuai (variasi Tinjauan AI, bukan efek A1).
  - Sisa kecil: anchor dari model ("sebesar 0,3% (yoy)") tidak verbatim, jadi kutipan yang ditampilkan hanya anchor itu, bukan kalimat penuh. Catatan "KONFLIK SUMBER" berasal dari Tbl III.5 (ULN swasta menurut kelompok kreditor, definisi lain); perilaku ini sudah ada sebelumnya.
- **A2 (tinggi).** Klaim multi-kelompok dicatat sebagai struktur: indikator, kelompok yang disebut, arah atau batas, dan kuantor (`seluruh` / `kecuali` / `sebagian besar` / `lainnya` / kelompok bernama), bukan satu tren dengan banyak metrik.
- **A3 (sedang).** Kelompok bernama selalu dipecah per kelompok secara konsisten. Contoh yang sudah berhasil: SK [60]–[64], peringkat kota [11]–[16].
- **A4 (sedang).** Label kelompok harus sesuai dimensi kalimat (usia vs pendidikan vs pengeluaran).

### B. Mesin pembanding (kode)
- **B1 (tinggi).** Kode menilai kuantor: `seluruh` = semua kelompok, `kecuali X` = X berkebalikan, `sebagian besar` = lebih dari separuh, `lainnya` = sisa kelompok. "Sebagian besar" **jangan** dipecah menjadi klaim per kelompok, karena akan menghasilkan Tidak Sesuai palsu.
- **B2 (tinggi).** Daftar kelompok untuk `seluruh` dan `lainnya` dilengkapi kode dari tabel (baris saudara), bukan dari ingatan AI.
- **B3 (sedang).** Operasi baru: pangsa gabungan, yaitu jumlah beberapa baris ÷ total (SULNI #16).
- **B4 (sedang).** Klaim peringkat ("tertinggi/terbesar di A, B, C") dicek sebagai urutan teratas, bukan hanya naik atau turun.
- **B5 (rendah).** Alasan Tidak Cukup Data yang lebih tepat, misalnya "tabel hanya punya data triwulanan".

### C. Tinjauan AI
- **C1 (tinggi, murah).** Urutan field jawaban: alasan dan sel dulu, baru `verdict`.
- **C2 (sedang).** Batch lebih kecil (5–10 klaim) dengan budget berpikir yang sebanding.
- **C3 (sedang).** Label dan operasi dari ekstraktor ditandai "mungkin keliru" di prompt.
- **C4 (sedang).** Jawaban boleh berisi beberapa baris, masing-masing dengan arah atau nilai, supaya kode bisa memverifikasi klaim majemuk.
- **C5 (rendah).** Tanya dua kali dan terima hanya kalau sepakat.

### D. Model
- **D1 (rendah).** Eskalasi ke model lebih kuat (misalnya Gemini 2.5 Pro) hanya untuk sisa klaim sulit, setelah A–C. Lihat memori `llm-model-choice`: jangan ganti model tanpa angka.
- **D2 (rendah).** Uji cepat varian (budget besar / penjelasan dulu / satu klaim per panggilan / Pro). Ubah satu hal per varian, ulangi 3×, sertakan klaim salah sisipan.

### E. Pengujian
- **E1 (tinggi).** Masukkan klaim di tabel atas ke bank soal atau eval sebagai kasus tetap.
- **E2 (tinggi).** Dokumen dengan kesalahan sengaja untuk menguji deteksi Tidak Sesuai.

## Urutan yang disarankan

1. A1.
2. E1 + E2, lalu rekam baseline (aturan main #2).
3. A2 + B1 + B2 (satu paket kuantor).
4. C1.
5. B3, B4.
6. Sisanya bila masih perlu.
