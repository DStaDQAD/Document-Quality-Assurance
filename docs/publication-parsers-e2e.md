# Uji ujung-ke-ujung parser per publikasi (2026-10-07)

Satu pemeriksaan sungguhan per publikasi: Gemini 2.5 Flash, cek grafik dan cek ejaan mati,
riwayat lokal terpisah (bukan Neon). Rilis dan Excel dari
`tests/fixtures/publications/<publikasi>/<periode>/`; pemanggil: `POST /api/verify-paired`
dengan `publication=<kunci>`.

## Ringkasan

| Publikasi | Rilis | Sheet | Parser | Klaim | Sesuai | Tidak Sesuai | Tidak Cukup Data | Waktu | Lulus |
|---|---|---|---|---|---|---|---|---|---|
| sulni | siaran pers ULN Juli 2026 | TabI.1, Tbl III.2, Tbl II.2 | sulni ×3 | 17 | 14 | 0 | 3 | 46 s | ya |
| cadangan-devisa | siaran pers Cadev Agustus 2026 | 5.9 | cadangan-devisa | 3 | 3 | 0 | 0 | 11 s | ya |
| sk | laporan SK Agustus 2026 | Tabel 1, 2, 3 | sk ×3 | 93 | 51 | 0 | 42 | 67 s | ya |
| sbank | laporan SBank Tw II 2026 | Tabel1, Tabel3 | sbank ×2 | 44 | 27 | 2 | 15 | 29 s | tidak (pencocok) |
| uang-beredar | laporan M2 Juli 2026 | I.1A, I.1 | uang-beredar ×2 | 87 | 21 | 3 | 63 | 60 s | tidak (ekstraksi/pencocok) |
| uang-primer-m0 | laporan M2 Juli 2026 (paragraf M0) | I.2 (+ I.1A) | uang-primer-m0, bi | 93 | 30 | 8 | 55 | 65 s | klaim M0: ya; lainnya tidak |
| spe | laporan SPE Juli 2026 | Tabel 1, 2, 3, 5, 6, 9 | spe ×6 | 67 | 30 | 9 | 28 | 50 s | tidak (ekstraksi) |
| shpr | laporan SHPR Tw II 2026 | TABEL 1, TABEL 3 | shpr ×2 | 70 | 15 | 6 | 49 | 42 s | tidak (satuan) |
| pii | laporan PII Tw II 2026 | 5.39 | pii | 97 | 28 | 22 | 47 | 29 s | tidak (tanda, perubahan) |
| skdu | laporan SKDU Tw II 2026 | T1, T2, T4 | skdu ×3 | 188 | 79 | 34 | 75 | 66 s | tidak (pencocok) |
| npi | laporan NPI Tw II 2026 | 5.1 | npi | 298 | 34 | 74 | 190 | 86 s | tidak (tanda, rincian) |
| pmi | laporan PMI Tw I 2026 | T1 PMI, T2 | pmi ×2 | 0 | 0 | 0 | 0 | 7 s | tidak diuji (laporan berupa gambar) |

**Bagian parser lulus di semua publikasi:** setiap sheet dibaca parser publikasinya sendiri
(`excel_parsers` tidak pernah `generic`, `llm`, atau `pointer-only`), dan angka emas ke-12
publikasi cocok (`tests/test_publication_golden.py`, 187 kasus). Kriteria "tidak ada Tidak Sesuai
palsu" hanya terpenuhi di SULNI, Cadev, dan SK. Tidak Sesuai di publikasi lain berasal dari
ekstraksi klaim dan pencocokan sumber — bukan dari pembacaan tabel — dan dirinci di bawah.

Dibanding uji awal SULNI (pointer-only, 272 s, 10 dari 18 klaim Tidak Cukup Data, 1 Tidak Sesuai
palsu): 46 s, 3 Tidak Cukup Data yang memang di luar jangkauan tabel, 0 Tidak Sesuai.

## Perbaikan yang lahir dari uji ini (sudah di branch)

- **Judul SULNI menyebut "ULN"** (dan "Indonesia" untuk TabI): klaim "ULN Indonesia 454,8 / 4,9%"
  tadinya Tidak Cukup Data karena baris TOTAL hanya terjangkau lewat judul.
- **Seri sumber dipecah oleh judul:** tabel ULN swasta sempat menjawab pangsa sektor ULN
  pemerintah karena baris sektornya "Administrasi Pemerintah …" ikut "menutupi" kata pemerintah;
  kini seri dimenangkan tabel yang judulnya sendiri menyebut kata klaim (5 Tidak Sesuai palsu → 0).

## Catatan per publikasi

### sulni
- Tidak Cukup Data yang diharapkan: "ULN publik" (= Pemerintah dan Bank Sentral; butuh alias),
  gabungan empat sektor 80,6% (operasi pangsa gabungan, di luar cakupan), rasio ULN/PDB Juli 30,7%
  (tabel hanya triwulanan).

### cadangan-devisa
- Semua klaim (146,5; 145,3; bulan impor) Sesuai.

### sk
- 42 Tidak Cukup Data: klaim per kota, ekspektasi harga, dan tabel yang tidak diunggah (Tabel 4–9).

### sbank
- Prakiraan DPK Q3 2025 88,22% dicocokkan ke baris Deposito (58,75) alih-alih Total — pemilihan
  baris oleh pencocok; plus satu klaim tren "stabil" dari pasangan itu.

### uang-beredar / uang-primer-m0
- Semua klaim M0 Sesuai (17,1%; 13,8%; Rp2.254,5 T; giro BI adjusted; uang kartal; SBI swasta; giro
  swasta).
- "Melambat dari 15,9%" / "tumbuh positif": klaim tentang laju pertumbuhan dievaluasi sebagai tren
  level.
- Simpanan berjangka 4,8% / 6,2% berasal dari paragraf DPK (tabel DPK tidak diunggah); pencocok
  mengambil komponen M2 yang namanya sama.
- Tagihan bersih kepada pemerintah pusat (klaim M2) dicocokkan ke baris bernama sama di tabel M0.
- Uang elektronik 17,9 vs SEKI 17,84 — selisih data sungguhan antara laporan dan tabel.

### spe
- Pertumbuhan mtm per kota (Tabel 7, tidak diunggah) diekstrak sebagai yoy lalu dihitung dari
  indeks Tabel 5; "2,3 persen" dibandingkan dengan level indeks.

### shpr
- Pertumbuhan qtq nasional per tipe rumah tidak ada di sheet mana pun; klaim "persen" dibandingkan
  langsung dengan level indeks (satuan `Indeks (2018=100)` tidak dianggap tak sebanding dengan
  persen). Seharusnya Tidak Cukup Data.

### pii
- Tanda: "kewajiban neto 197,4" vs posisi neto −197,4.
- Perubahan ("turun sebesar 16,8 miliar") diekstrak sebagai nilai dan dibandingkan dengan level.
- Posisi neto per komponen tidak ada di tabel; pencocok mengambil angka bruto sisi kewajiban/aset.

### skdu
- Kapasitas produksi (T2) dicocokkan ke SBT kegiatan usaha (T1) untuk sektor yang sama.
- Banyak angka SBT sub-LU/periode lain dicocokkan ke baris LU induk.
- SKDU dibaca pembaca generik yang sama seperti sebelum branch ini (parser `skdu` hanya membungkus
  dan merapikan satuan), jadi ini perilaku lama.

### npi
- Tanda: rilis menulis "defisit 12,5" (positif), tabel −12,49; impor dicatat negatif (debit).
- Rincian per negara tujuan/komoditas (ekspor ke Tiongkok, impor minyak) tidak ada di sheet 5.1 dan
  dicocokkan ke baris induk "Nonmigas > Ekspor".
- Rasio "% PDB" dihitung sebagai TB / baris "% PDB".

### pmi
- Halaman laporan berupa gambar: lapisan teks 187 karakter, tersaring jadi 0, dan fallback visi
  tidak terpicu, sehingga tidak ada klaim. Angka emas PMI dibaca dari render halaman.

## Perbaikan lanjutan

### 1. Tanda "defisit / kewajiban neto" (2026-10-07, `paired_verifier._reinterpret_signed_level`)

Klaim nilai yang Tidak Sesuai dinilai ulang bila kalimatnya menyebut defisit, kewajiban neto,
arus keluar neto, atau kontraksi, nilai tabelnya negatif, dan angka klaim sama dengan besarnya.
Uji ulang dengan Gemini:

| Publikasi | Tidak Sesuai sebelum | Tidak Sesuai sesudah | Klaim yang terselamatkan |
|---|---|---|---|
| npi | 74 | 62 | 9 (mis. defisit TB 12,5 vs −12,49; NPI defisit 0,9 vs −0,88) |
| pii | 22 | 13 | 3 (kewajiban neto 197,4 dan 223,0; IL kewajiban neto 168,8) |

Jumlah klaim berubah antar-run (ekstraksi LLM tidak deterministik), jadi angka sebelum/sesudah
adalah perbandingan kasar; semua klaim yang terselamatkan dicek manual dan benar.

### 2. Persen vs level indeks (`paired_verifier`, `_is_plain_index`)

Klaim nilai dalam persen tidak lagi dibandingkan dengan sheet indeks tanpa tanda % (`Indeks`,
`Indeks (2018=100)`); PMI memakai satuan sheet-nya sendiri `%, Indeks` sehingga "52,03%" tetap
dibandingkan. SHPR: enam klaim qtq per tipe rumah yang tadinya Tidak Sesuai (0,40 vs 114,01) kini
Tidak Cukup Data; Sesuai 15 → 21.

### 3. Tabel pertumbuhan menyebut jenis pertumbuhannya

SPE Tabel 2/4/6/8 `%, yoy`, Tabel 3/7 `%, mtm`; SHPR Tabel 3 `%, qtq & yoy` (pita di label baris
menentukan jenisnya). Klaim yoy membaca sel tabel yoy langsung, dan tidak dijawab sel mtm/qtq.

| Publikasi | Tidak Sesuai sebelum (awal → sesudah no. 2) | sesudah no. 3 | Sesuai |
|---|---|---|---|
| shpr | 6 → 8 | **0** | 23 |
| spe | 9 → 14 | 11 | 30 |

Sisa 11 di SPE satu pola: ekstraktor memberi label yoy pada klaim "(mtm)", lalu pertumbuhan yoy
dihitung dari indeks. Perbaikannya ada di prompt ekstraktor (belum dikerjakan).

### 4. mtm/qtq bukan yoy, perubahan sebagai diff, rincian yang tidak ada, seri penuh (2026-10-08)

- Prompt ekstraktor (aturan 2e, 2f): laju "(mtm)/(qtq)/(ctc)" adalah nilai persen, bukan yoy;
  "naik/turun sebesar X miliar" adalah `diff` periode sebelumnya → sekarang, bertanda minus bila turun.
- Klaim persen bertanda mtm/qtq/yoy tidak dijawab tabel pertumbuhan berjenis lain (jenis dibaca dari
  tanda setelah angka klaim di kalimatnya).
- Kata di belakang istilah yang dicocokkan baris, yang tidak ada di mana pun dalam tabel ("ekspor
  nonmigas ke **Tiongkok**", "impor barang **konsumsi**"), membuat sumber itu tidak menjawab.
- Seri penuh di semua kunci peringkat (SKDU: label sektor sama di tiap sheet) dimenangkan sumber yang
  nilainya sama dengan angka klaim.

| Publikasi | Tidak Sesuai sebelum | sesudah | Sesuai |
|---|---|---|---|
| spe | 11 | **2** | 37 |
| npi | 62 | 36 | 41 |
| pii | 13 | 8 | 26 |
| skdu | 34 | 29 | 95 |

Sisa:
- **spe (2):** teks legenda grafik ("IPR (yoy) meningkat …") terbaca sebagai klaim tren.
- **npi (36):** tren pada saldo defisit ("defisit … meningkat" = saldo makin negatif), rasio "% PDB"
  dihitung TB ÷ baris % PDB, "transaksi modal dan finansial" adalah jumlah dua baris, neraca
  perdagangan nonmigas berbasis lain (11,5 vs 12,14).
- **pii (8):** perubahan per komponen ("kenaikan AFLN investasi lainnya 8,7") dan posisi neto
  komponen yang tidak ada di tabel.
- **skdu (29):** klaim tentang Tabel 5–7 (harga jual, inflasi, investasi) yang tidak diunggah di run
  ini, jatuh ke baris sektor Tabel 1 — kata topiknya ("SBT harga jual …") ada di DEPAN nama sektor,
  sedangkan aturan rincian hanya menolak kata di belakang.

## Tindak lanjut (di luar parser)

1. **Kata arah pada angka:** "defisit X", "kewajiban neto X", "terkontraksi X" → −X, dan sisi debit
   NPI (impor) yang dicatat negatif.
2. **Rincian yang tidak ada di sheet** (per negara, per komoditas, per kota yang tidak diunggah)
   jangan dijawab baris induknya — seharusnya Tidak Cukup Data.
3. **Perubahan vs level** ("naik/turun sebesar X") dan **laju vs level** ("melambat dari 15,9%").
4. **Satuan indeks vs persen** tidak boleh dibandingkan.
5. **Laporan berupa gambar** (PMI): fallback visi untuk narasi.
6. **Alias** label: "ULN publik" → Pemerintah dan Bank Sentral; "KFLN"/"AFLN" → Kewajiban/Aset.
7. **SKDU:** baris total nasional dinamai "JASA LAINNYA > TOTAL" oleh pembaca generik; beri SKDU
   spesifikasi mesin sendiri agar TOTAL tetap polos.
