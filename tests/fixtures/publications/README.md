# Sampel publikasi BI untuk tes angka emas

Satu folder per publikasi (kunci sama dengan `check_history.PUBLICATIONS`), lalu satu folder per
periode data yang dibahas rilisnya (`2026-07`, `2026-Q2`). Isinya:

- file Excel acuan dengan nama aslinya,
- `siaran_pers.pdf` dan/atau `laporan.pdf`,
- `golden.json` — angka yang dicetak BI dan baris tabel tempat angka itu berada.

File BI (`.xls`, `.xlsx`, `.pdf`, `.zip`) **tidak di-commit** (lihat `.gitignore`); hanya
`golden.json`, berkas ini, dan `show_table.py`. Tes `tests/test_publication_golden.py` melewati
(skip) edisi yang Excel-nya tidak ada — di CI semuanya dilewati — jadi jalankan di lokal sebelum
merge perubahan parser:

    .venv/Scripts/python -m pytest tests/test_publication_golden.py -v

Melihat apa yang dibaca parser dari satu sheet (berguna saat menulis `golden.json`):

    .venv/Scripts/python tests/fixtures/publications/show_table.py <publikasi> <file Excel> <sheet> <tahun> <periode>

Membaca teks rilis:

    .venv/Scripts/python -c "import pdfplumber,sys; print('\n'.join((p.extract_text() or '') for p in pdfplumber.open(sys.argv[1]).pages[:3]))" <file.pdf>

## Sumber (bi.go.id)

| Publikasi | Folder | Excel | Rilis |
|---|---|---|---|
| uang-beredar | 2026-07 | `/SEKI/tabel/TABEL1_1.xls`, `/SEKI/tabel/TABEL1_1_1.xls` | laporan Uang Beredar Juli 2026 |
| uang-primer-m0 | 2026-07 | `/SEKI/tabel/TABEL1_2.xls` (unduhan September 2026) | paragraf M0 di laporan Uang Beredar Juli 2026 |
| cadangan-devisa | 2026-08 | `/SEKI/tabel/TABEL5_9.xls` | siaran pers Cadangan Devisa Agustus 2026 |
| npi | 2026-Q2 | `/SEKI/tabel/TABEL5_1.xls` | laporan NPI Triwulan II 2026 |
| pii | 2026-Q2 | `/SEKI/tabel/TABEL5_39.xls` | laporan PII Triwulan II 2026 |
| sulni | 2026-07 | `/en/statistik/ekonomi-keuangan/sulni/Documents/SULNI-September-2026.zip` | siaran pers ULN Juli 2026 (No.28/188/DKom) |
| sk | 2026-08 | `/id/publikasi/laporan/Documents/Data-Series-SK-Agustus-2026.zip` | laporan Survei Konsumen Agustus 2026 |
| spe | 2026-07 | `/id/publikasi/laporan/Documents/Data-Series-SPE-Juli-2026.zip` | laporan Survei Penjualan Eceran Juli 2026 |
| skdu | 2026-Q2 | `/id/publikasi/laporan/Documents/Data-Series-SKDU-Triwulan-II-2026.zip` | laporan SKDU Triwulan II 2026 |
| sbank | 2026-Q2 | `/id/publikasi/laporan/Documents/Data-Series-Survei-Perbankan-Tw-II-2026.zip` | laporan Survei Perbankan Triwulan II 2026 |
| shpr | 2026-Q2 | `/id/publikasi/laporan/Documents/SHPR_Tw_II_2026.zip` | laporan SHPR Triwulan II 2026 |
| pmi | 2026-Q1 | `/id/publikasi/laporan/Documents/PMI-Triwulan-I-2026.zip` | laporan Prompt Manufacturing Index Triwulan I 2026 |

Zip generik tanpa periode (`/id/publikasi/laporan/Documents/SK.zip` dst.) berisi edisi lama;
pakai zip yang bertanggal. bi.go.id menolak curl/headless untuk halaman daftar; unduh lewat peramban.
