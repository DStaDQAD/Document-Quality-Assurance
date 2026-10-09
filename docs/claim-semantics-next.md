# Perbaikan semantik klaim — usulan A–H (dikerjakan 2026-10-09)

Disusun 2026-10-08. Lanjutan dari `docs/publication-parsers-e2e.md` (bagian "Perbaikan lanjutan").

## Status saat disimpan

- `main`: parser per publikasi + perbaikan 1–3 (tanda defisit/kewajiban neto, persen vs indeks,
  jenis tabel pertumbuhan). Belum di-push.
- Branch `fix-claim-semantics` (**belum di-merge**, menunggu keputusan pengguna), 5 commit:
  aturan prompt 2e/2f (mtm/qtq bukan yoy; perubahan sebesar X = `diff`), laju mtm/qtq tidak dijawab
  tabel yoy (`_claimed_rate_kind`), rincian yang tidak ada di tabel (`_breakdown_the_table_lacks`),
  seri penuh dimenangkan sumber yang nilainya sama dengan klaim (`_rank_key`). Suite 1320 hijau.
  Hasil uji Gemini: SPE 11→2, NPI 62→36, PII 13→8, SKDU 34→29 Tidak Sesuai.
- `.gitignore` di working tree berisi baris `presentation/` milik pengguna yang sengaja tidak
  di-commit.

## Usulan A–G (menunggu persetujuan)

| # | Sisa Tidak Sesuai | Usulan | Di mana |
|---|---|---|---|
| A | NPI: "defisit … meningkat" / "defisit lebih rendah" dinilai pada saldo negatif | Klaim tren yang kalimatnya menyebut defisit/kewajiban neto (`_NEGATIVE_BALANCE_WORDS`), pada seri negatif, dinilai pada **besarnya** (nilai mutlak) | `paired_verifier._compute_trend` |
| B | NPI: "3,3% dari PDB" dihitung TB ÷ baris "Transaksi Berjalan (% PDB)" | Bila penyebut rasio sudah berupa baris rasio "(% PDB)" metrik yang sama, baca nilai baris itu langsung (dengan aturan tanda defisit) | `paired_verifier._compute_ratio` |
| C | NPI: "transaksi modal **dan** finansial surplus 12,0" | Aturan prompt: metrik "A dan B" yang di tabel ada sebagai dua baris → `sum` dua baris pada periode yang sama | `structured_extractor` prompt |
| D | PII: "kenaikan posisi AFLN investasi lainnya sebesar 8,7" | Perluas aturan 2f ke bentuk kata benda (kenaikan/penurunan/peningkatan … sebesar X) → `diff` | `structured_extractor` prompt |
| E | PII: posisi **neto** per komponen dicocokkan ke baris bruto | Klaim yang menyebut "neto/bersih" tidak dijawab baris yang tidak menyebut neto/bersih → Tidak Cukup Data | `paired_verifier` (loop kandidat) |
| F | SKDU: "SBT **harga jual** Perdagangan" jatuh ke sheet Kegiatan Usaha | Kata topik di **depan** nama baris yang tidak ada di tabel (judul, baris, **satuan**) juga menolak sumber; satuan dihitung agar "SBT …" tetap cocok dengan sheet SBT | `paired_verifier._breakdown_the_table_lacks` + `TableData.words_absent` |
| G | PMI: laporan berupa gambar, 0 klaim (teks 187 karakter, tersaring jadi 0) | Jalankan pembacaan visi juga saat teks per halaman nyaris kosong, bukan hanya saat kosong sama sekali | `pdf_extraction` |

Tidak diusulkan: neraca perdagangan nonmigas 11,5 vs 12,14 (kemungkinan beda basis — temuan
sungguhan); legenda grafik SPE terbaca sebagai klaim tren (2 kasus; aturannya berisiko membuang
kalimat asli).

## Cara kerja yang disepakati

- Satu desain singkat per perbaikan, disetujui pengguna dulu; TDD (tes gagal dulu) per perbaikan.
- Penjaga: seluruh suite, eval Layer-1 (`tests/test_eval.py`), tes angka emas
  (`.venv/Scripts/python -m pytest tests/test_publication_golden.py`, file BI hanya ada lokal).
- Uji ulang dengan Gemini per publikasi yang terdampak (±Rp700 per pemeriksaan), dijalankan
  **satu per satu di depan** (proses latar belakang pernah dihentikan karena memori):
  skrip di scratchpad sesi lama (`e2e_run.py`) — isinya: `TestClient(main.app).post("/api/verify-paired",
  params={"run_typo_check": "false", "check_charts": "false", "publication": <kunci>,
  "sheet_names": "<sheet1>,<sheet2>"}, files=[pdf_file, excel_file...])` dengan
  `APP_USERNAME=""` dan `HISTORY_DATABASE_URL=""` di-set di Python, dan
  `check_history.LOCAL_DB_PATH` diarahkan ke folder sementara.
- Commit tanpa trailer `Co-Authored-By`; merge/push hanya bila pengguna minta.


## Hasil (2026-10-09, branch `fix-claim-semantics-2`, belum di-merge)

A–H dikerjakan (H = kolom sisa template di Excel PMI Tw IV-2025 T2), ditambah perbaikan yang
ditemukan saat uji Gemini: arah tren defisit diambil dari kata kerjanya (LLM kadang menulis
`is_decreasing`, kadang `is_increasing` untuk "defisit melebar"), bentuk kata benda
("peningkatan defisit …"), aturan tanda defisit untuk `sum` dan `diff`, baris (% PDB) metrik lain
bukan penyebut, posisi neto di kalimat yang sama ("… dibandingkan dengan 237,7"), dan pointer
tidak ditanya tentang posisi neto. Suite 1354 hijau, eval Layer-1 dan ejaan 100%.

| Publikasi | Tidak Sesuai sebelum | sesudah | Catatan |
|---|---|---|---|
| NPI | 34 (baseline jalur tabel, run yang sama) | 22 | Sesuai 38 → 54 |
| PII | 13 → 8 (gelombang lalu) | 2 | |
| SKDU | 29 | 18 | harga jual/investasi/inflasi hilang |
| PMI | 0 klaim terbaca | 53 klaim, 38 Sesuai, 6 Tidak Sesuai | lihat temuan 2 |

### Temuan baru (belum diusulkan perbaikannya)

1. **Pointer menunjuk sel total untuk rincian yang tidak ada di tabel.** NPI: "impor minyak",
   "ekspor LNG", "impor gas" ditunjuk ke sel Migas/Barang total. Jumlahnya berubah-ubah antar run
   (baseline 11 vonis pointer, run lain 47, run lain 2) — ini variasi LLM di pass pointer.
2. **Visi salah baca digit di PMI.** Laporan menulis "49,32%" dan "53,20%"; visi membaca 49,22
   dan 52,2, sehingga Tidak Sesuai palsu. Kandidat: render resolusi lebih tinggi, atau angka yang
   juga ada di lapisan teks diambil dari sana.
3. **Pertumbuhan baris debit (impor, negatif di SEKI V.1)** dihitung dengan tanda terbalik
   (−20,3% padahal impor naik 20,3%).
4. **SKDU:** angka SBT sub-LU masih jatuh ke baris LU induk; kapasitas produksi ke T1.
