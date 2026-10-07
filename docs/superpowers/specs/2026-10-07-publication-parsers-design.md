# Parser Khusus per Publikasi — Design

Tanggal: 2026-10-07
Status: disetujui per bagian dalam brainstorming, menunggu review spec

## Tujuan

Setiap tabel acuan dari ke-12 publikasi BI yang disertai siaran pers terbaca **tanpa AI** oleh
parser yang dibuat khusus untuk publikasi itu, dan kebenarannya dibuktikan dengan angka di
siaran pers/laporan edisi terbaru.

Pemicu: uji SULNI 2026-10-07 (siaran pers ULN Juli 2026 vs tabel SULNI edisi September 2026).
Ketiga tier parser gagal di semua sheet SULNI, sehingga sumbernya jatuh ke *pointer-only*:
pembacaan Excel butuh 180 dari 272 detik, 10 dari 18 klaim jadi Tidak Cukup Data padahal
angkanya ada di tabel, dan satu klaim jadi Tidak Sesuai palsu karena AI menunjuk kolom
Agustus 2025 alih-alih Juli 2025.

### Keputusan dari pengguna

- **Sumber acuan:** file Excel publik di bi.go.id (tabel SEKI, zip data series di halaman
  Laporan, zip SULNI) — bukan file internal compiler.
- **Pemilihan sheet tetap manual** oleh pemeriksa, seperti sekarang.
- **Satu parser khusus per publikasi** (bukan memperluas parser generik).
- **Bukti selesai:** tes angka emas per publikasi (tanpa LLM) **ditambah** satu uji
  ujung-ke-ujung dengan Gemini per publikasi.
- **Sampel disimpan di folder repo, rapi per publikasi, tetapi file BI tidak di-commit.**
  Excel dan PDF siaran pers/laporan di `tests/fixtures/publications/` masuk `.gitignore`, sama
  seperti kebiasaan `sample_data/`. Yang di-commit hanya `golden.json` (definisi tes buatan kita).
- **Label dwibahasa:** cukup bagian Indonesia (lihat Arsitektur).

### Di luar cakupan (dicatat untuk nanti)

- Pemilihan sheet otomatis per publikasi; unggah zip langsung.
- Operasi "pangsa gabungan beberapa baris" (klaim seperti "empat sektor = 80,6% ULN swasta").
  Sampai ada, klaim seperti itu idealnya Tidak Cukup Data, bukan Tidak Sesuai.
- Kesalahan AI menunjuk kolom di jalur *cell-pointer*; diharapkan tidak lagi terpakai untuk
  sheet yang dicakup parser publikasi.
- Pengambilan Excel otomatis dari bi.go.id (situs menolak curl/headless).

## Kondisi awal (audit 2026-10-07, tanpa LLM)

| Publikasi | Sampel | Tier yang membaca hari ini |
|---|---|---|
| Uang Beredar | SEKI `TABEL1_1.xls`, sheet I.1 | bi |
| Cadangan Devisa | SEKI `TABEL5_9.xls`, sheet 5.9 | bi |
| Survei Konsumen | `Tabel Series SK Juni 2026.xlsx` (9 sheet) | generic (semua) |
| Survei Perbankan | `Data Series Survei Perbankan Tw II 2026.xlsx` (5 sheet) | generic (label dwibahasa panjang) |
| PMI | `Tabel PMI-BI Tw II 2026.xlsx` (2 sheet) | generic |
| SULNI | zip SULNI September 2026 (3 workbook, 25 sheet) | **gagal semua**, 15–20 detik/sheet |
| M0, NPI, PII, SPE, SKDU, SHPR | belum ada | belum diketahui |

## Arsitektur

### Paket `publication_parsers/`

```
publication_parsers/
  __init__.py          # registri: kunci publikasi -> modul parser; parse_for_publication(...)
  _common.py           # bahan bersama (lihat di bawah)
  uang_beredar.py      cadangan_devisa.py   uang_primer.py   npi.py      pii.py      sulni.py
  sk.py                spe.py               skdu.py          sbank.py    shpr.py     pmi.py
```

Kunci publikasi sama dengan `PUBLICATIONS` di `static/index.html` dan
`check_history.PUBLICATIONS` (`uang-beredar`, `uang-primer-m0`, `npi`, `pii`,
`cadangan-devisa`, `sulni`, `sk`, `spe`, `skdu`, `pmi`, `sbank`, `shpr`).

Setiap modul:

- `SHEETS`: daftar (atau pola) nama sheet yang dicakup.
- `parse(workbook, sheet_name) -> TableData` — mengembalikan model tabel yang sudah dipakai
  pipeline (`table_model.TableData`, sumbu temporal: (label, tahun, bulan/`Q1`..`Q4`) → nilai,
  plus satuan). Melempar `PublicationParseError` dengan pesan spesifik bila format tidak sesuai.
- Untuk UB dan Cadev: membungkus `excel_parser_bi.parse_bi_table` yang sudah terbukti; perilaku
  tidak berubah.

`_common.py` berisi potongan yang memang sama di banyak format, agar tiap parser tetap pendek:

- membaca workbook sekali (xls via xlrd, xlsx via openpyxl read-only, nilai terhitung) dan
  menyimpannya per isi file selama satu pemeriksaan;
- normalisasi periode: bulan Indonesia/Inggris, tanda `*`/`**` dibuang, triwulan
  (`Tw II`, `Q2`, `II`, `Triwulan II`) → `Q2`, tahun berupa angka, teks, atau tanggal;
- pembersihan label: penomoran (`1.`, `2.1`, `-`) dibuang; label dwibahasa `Indonesia / English`
  diambil bagian Indonesianya; label yang berulang di beberapa induk diberi awalan induknya
  (`Lembaga Keuangan > Bank`), seperti parser BI;
- satuan dari judul tabel (`Juta USD`, `Miliar Rp`, `Indeks`, `%`).

### Pemilihan parser saat pemeriksaan

- `main.py` sudah menerima `publication`; nilainya diteruskan ke `verify_paired` dan ke
  `_parse_table_with_fallback` (parameter baru, opsional).
- Bila ada publikasi dan sheet termasuk `SHEETS` modulnya → parser publikasi dipakai; nama
  parser dilaporkan sebagai kunci publikasi (mis. `excel_parsers: ["sulni"]`).
- Bila parser publikasi melempar galat, atau sheet di luar cakupan, atau publikasi kosong →
  rantai lama (bi → generic → llm → pointer-only) berjalan seperti sekarang, dan galatnya
  dicatat sebagai peringatan. Hasil tidak pernah lebih buruk dari hari ini.

### Label dwibahasa (koreksi atas Bagian 3 brainstorming, disetujui pengguna)

`TableData` belum mendukung nama alternatif untuk label. Label disimpan **bagian Indonesia
saja**; bagian Inggris dibuang. Ekstraksi klaim sudah memberi LLM daftar label baris, sehingga
frasa Inggris di narasi ("nonfinancial corporations") tetap dipetakan ke label Indonesia
("Bukan Lembaga Keuangan"). Tes `query` di angka emas (lihat bawah) menjaga hal ini. Bila ternyata
tidak cukup, dukungan alias di `TableData` menjadi pekerjaan terpisah.

### Cakupan sheet

Setiap parser menangani **semua sheet deret waktu** di workbook publikasinya. Sheet bukan
deret waktu (mis. SULNI Tbl II.7 jadwal pembayaran, Tbl II.8 daftar seri SBN) dicatat di luar
cakupan dan memakai rantai lama. Daftar final per publikasi dicantumkan di rencana
implementasi setelah sampel terkumpul.

## Sampel dan angka emas

### Struktur folder

File BI (Excel, PDF) ada di folder ini secara lokal tetapi di-gitignore; hanya `golden.json`
yang di-commit.

```
tests/fixtures/publications/
  <kunci-publikasi>/
    <edisi YYYY-MM>/            # bulan rilis siaran pers
      <file Excel acuan, nama asli>
      siaran_pers.pdf           # halaman siaran pers dicetak ke PDF
      laporan.pdf               # bila publikasi punya laporan
      golden.json
```

Sumber per publikasi:

| Publikasi | File Excel acuan |
|---|---|
| Uang Beredar | SEKI Tabel I.1 dan I.1.A |
| Uang Primer (M0) | SEKI Tabel I.2 |
| NPI | SEKI Tabel V.1 |
| PII | SEKI Tabel V.39 |
| Cadangan Devisa | SEKI Tabel V.9 |
| SULNI | zip SULNI (TABEL_INDONESIA, TABEL_PEMERINTAH, TABEL_SWASTA) |
| SK, SPE, SKDU, SBank, SHPR | zip data series di halaman Laporan BI |
| PMI | `Tabel PMI-BI` (sampel pengguna; sumber resmi di bi.go.id perlu dipastikan) |

### `golden.json`

```json
{
  "publication": "sulni",
  "edition": "2026-09",
  "latest_period": {"year": 2026, "period": "Jul"},
  "sheets": {"TABEL_INDONESIA Sep26_value.xlsx": ["TabI.1", "TabI.2"]},
  "values": [
    {"sheet": "TabI.1", "label": "Total (1+2)", "query": "ULN Indonesia",
     "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001, "decimals": 1,
     "expected": 454.8, "quote": "tercatat sebesar 454,8 miliar dolar AS"},
    {"sheet": "TabI.1", "label": "Swasta", "query": "ULN swasta",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1,
     "expected": -1.2, "quote": "mengalami kontraksi sebesar 1,2% (yoy)"}
  ]
}
```

- `kind`: `value` (nilai di sel) atau `yoy` (dihitung dari periode yang sama tahun sebelumnya).
- `scale` (opsional): pengali dari satuan tabel ke satuan narasi.
- `decimals`: presisi angka di siaran pers; pembandingan memakai pembulatan ini.
- `quote`: kutipan dari siaran pers/laporan, untuk penelusuran.
- Target 5–15 angka per publikasi.

### Tes (pytest, tanpa LLM)

Tes menemukan semua folder edisi secara otomatis; edisi baru cukup ditambahkan sebagai folder.
Karena file BI tidak di-commit, tes sebuah edisi **dilewati (skip) bila file Excel-nya tidak ada**
— di GitHub Actions semuanya dilewati; tes ini dijalankan di lokal, dan wajib hijau sebelum
merge. Tes unit `_common.py` memakai grid sintetis kecil sehingga tetap jalan di CI.

1. **Terbaca oleh parser publikasi:** setiap sheet di `sheets` diparse oleh modul publikasinya
   tanpa jatuh ke rantai lama; tabel tidak kosong, satuan terisi, periode terakhir =
   `latest_period`.
2. **Angka emas cocok:** nilai pada `label`/`year`/`period` (dikali `scale`, dibulatkan
   `decimals`) sama dengan `expected`; untuk `yoy`, pertumbuhan dihitung dari tabel.
3. **Nama di narasi mendarat di baris yang benar:** `query` lewat pencocok label pipeline
   (`label_match_score` / `lookup_fuzzy`) memilih `label`.

## Uji ujung-ke-ujung

Satu pemeriksaan sungguhan per publikasi dengan Gemini di aplikasi lokal (riwayat terpisah,
bukan riwayat produksi), memakai siaran pers/laporan dan sheet acuan edisi yang sama. Hasilnya
dibandingkan per klaim dengan angka emas dan pengecekan manual (seperti tabel SULNI), dicatat di
`docs/publication-parsers-e2e.md`. Perkiraan biaya ±Rp700 × 12, beberapa menit per publikasi.

Kriteria lulus per publikasi: tidak ada Tidak Sesuai palsu; klaim yang angkanya ada di sheet
tercakup tidak jatuh ke Tidak Cukup Data karena masalah parsing.

## Urutan pengerjaan

Branch `publication-parsers`, commit per langkah:

1. **Fondasi:** aturan `.gitignore` untuk file BI di `tests/fixtures/publications/`, `_common.py`, registri, parameter `publication` diteruskan ke
   `_parse_table_with_fallback`, cadangan ke rantai lama, workbook dibaca sekali; modul
   `uang_beredar` dan `cadangan_devisa` (pembungkus parser BI) beserta angka emasnya.
2. **SULNI:** parser + angka emas edisi 2026-09; ulangi uji siaran pers ULN Juli 2026.
3. **Kumpulkan sampel** M0, NPI, PII, SPE, SKDU, SHPR, dan I.1.A (Excel + siaran pers/laporan).
4. **Parser per publikasi** satu per satu (M0, NPI, PII, SK, SPE, SKDU, SBank, SHPR, PMI),
   masing-masing langsung dengan angka emasnya.
5. **Uji ujung-ke-ujung** ke-12 publikasi dan laporannya.

## Risiko

- **Format berubah antar edisi** (kolom/judul bergeser): tes emas gagal di edisi baru, dan
  di aplikasi parser publikasi melempar galat lalu jatuh ke rantai lama — terdeteksi, tidak diam.
- **Tes emas tidak jalan di CI** karena file BI tidak di-commit: perubahan parser yang merusak
  sebuah publikasi baru ketahuan saat tes dijalankan di lokal. Mitigasi: menjalankan tes emas
  lokal adalah syarat sebelum merge; tes unit `_common.py` dengan grid sintetis tetap di CI.
- **PMI:** sumber Excel resmi belum pasti; bila berbeda dari sampel, parser PMI menyesuaikan.
- **Label Inggris** di narasi: dijaga tes `query`; alias di `TableData` disiapkan sebagai
  pekerjaan lanjutan bila perlu.
