# Parser Khusus per Publikasi — Design

Tanggal: 2026-10-07
Status: disetujui pengguna 2026-10-07. Rencana implementasi:
`docs/superpowers/plans/2026-10-07-publication-parsers.md`. Bagian "Kondisi awal", "Arsitektur",
"Sampel dan angka emas" dan "Cakupan sheet" diperbarui setelah semua sampel terkumpul dan
prototipe pembaca dicoba pada sheet asli (lihat catatan "Pembaruan" di tiap bagian).

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
| Uang Beredar | SEKI `TABEL1_1.xls` (I.1), `TABEL1_1_1.xls` (I.1A) | bi |
| Uang Primer (M0) | SEKI `TABEL1_2.xls` (I.2) | bi |
| Cadangan Devisa | SEKI `TABEL5_9.xls`, sheet 5.9 | bi |
| NPI | SEKI `TABEL5_1.xls` (5.1), triwulanan | generic, label bernomor ("1.0 > I. Transaksi Berjalan") |
| PII | SEKI `TABEL5_39.xls` (5.39), triwulanan | generic, label bernomor |
| Survei Konsumen | `Tabel Series Survei Konsumen Agustus 2026.xlsx` (9 sheet) | generic (semua) |
| SKDU | `Tabel SKDU 17 Lapangan Usaha Tw II-2026.xlsx` (11 sheet) | generic (semua) |
| Survei Perbankan | `Data Series Survei Perbankan … Triwulan II 2026.xlsx` (5 sheet) | generic, label Indonesia+Inggris tergabung |
| PMI | `PMI-Triwulan-I-2026.xlsx` (2 sheet) | generic, label tergabung |
| SPE | `Tabel Series SPE - Juli 2026.xlsx` (9 sheet) | bi, label rusak (angka) |
| SHPR | `SHPR_Tw_II_2026.xlsx` (3 sheet) | **gagal semua** |
| SULNI | zip SULNI September 2026 (3 workbook, 25 sheet) | **gagal semua**, 15–20 detik/sheet |

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

- `SHEET_PATTERNS`: pola regex nama sheet yang dicakup (dicocokkan utuh, tanpa beda huruf
  besar/kecil, spasi tepi diabaikan). Pola, bukan daftar nama, karena BI kadang mengganti nama
  sheet antaredisi (PMI: `T1 - Komponen PMI` → `T1 PMI`).
- `parse(data, sheet_name) -> TableData` — mengembalikan model tabel yang sudah dipakai
  pipeline (`table_model.TableData`, sumbu temporal: (label, tahun, bulan/`Q1`..`Q4`) → nilai,
  plus satuan). Melempar `PublicationParseError` dengan pesan spesifik bila format tidak sesuai.
- **Pembaruan — tiga cara membaca, dipilih per publikasi:**
  - UB, M0, Cadev membungkus `excel_parser_bi.parse_bi_table` (tabel SEKI bulanan); perilaku
    tidak berubah, hanya hasilnya diperiksa utuh (ada angka, tidak ada label kembar).
  - SK dan SKDU membungkus `table_parser_generic.parse_generic_table`, yang sudah membaca
    keduanya dengan benar; hasilnya diperiksa berupa deret waktu.
  - NPI, PII, SULNI, SPE, SBank, SHPR, PMI memakai satu mesin pembaca deret waktu di
    `_common.py`; modulnya hanya menyatakan `SheetSpec` per pola sheet: kolom label, bentuk
    header (baris tahun di atas baris periode, atau satu baris `Q1-2026`), cara hierarki
    disimpan (indentasi sel — NPI/PII; penomoran `2.1.1.` — SULNI; kolom tempat teks mulai —
    SULNI TabI.7; baris judul seksi tanpa angka — SHPR, SPE Tabel 9), dan pita kolom
    (SHPR Tabel 3: `TRIWULANAN (QTQ)` vs `TAHUNAN (YOY)`).

`_common.py` berisi potongan yang memang sama di banyak format, agar tiap parser tetap pendek:

- membaca workbook sekali: **pembaruan** — openpyxl harus memuat seluruh .xlsx untuk melihat
  sel gabungan (±9 detik untuk satu workbook SULNI), sedangkan mengubah sheet yang sudah dimuat
  menjadi grid hampir gratis. Maka pembukaan pertama sebuah workbook mengubah semua sheet-nya
  sekaligus dan menyimpannya di memori (per isi file); sheet berikutnya, dan grid untuk
  *cell-pointer* yang dibaca `verify_paired` setelah parsing, diambil dari memori;
- normalisasi periode: bulan Indonesia/Inggris, tanda `*`/`**` dibuang, triwulan
  (`Tw II`, `Q2`, `QII`, `II`, `Triwulan II`) → `Q2`, tahun berupa angka, teks, atau tanggal;
  **kolom tahunan (tahun tanpa bulan/triwulan) dilewati**; tahun yang hanya tertulis di kolom
  pertama sebuah tahun diteruskan ke kanan; kolom cermin bahasa Inggris di kanan yang mengulang
  periode yang sama diabaikan (yang paling kiri menang); sheet yang menumpuk beberapa blok
  dengan header sendiri (SBank Tabel 3–4) dibaca per blok;
- pembersihan label: penomoran (`I.`, `A.`, `a.`, `1.`, `2.1.1.`, `-`) dibuang tanpa merusak
  singkatan (`A.D.B`, `I.B.R.D`); label dwibahasa `Indonesia / English` diambil bagian
  Indonesianya; rujukan rumus (`TOTAL (1+2)`) dan tanda catatan kaki (`*`, `¹`, `2)`) dibuang;
  label yang berulang diberi awalan induk **seperlunya saja**: satu tingkat
  (`Barang > Ekspor`), atau lebih bila masih kembar
  (`Utang Jangka Pendek > Pemerintah dan Bank Sentral > Pemerintah`); label unik tetap polos,
  seperti parser BI;
- satuan dari sel berkurung di atas header (`(Juta USD / Million of USD)` → `Juta USD`), atau
  ditetapkan modul bila sheet tidak mencantumkannya (SHPR, SBank, PMI, SPE).

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
cakupan dan memakai rantai lama.

**Pembaruan — daftar final (prototipe membaca semuanya dari sampel):**

| Publikasi | Sheet yang dicakup |
|---|---|
| Uang Beredar | `I.1`, `I.1A` (sheet historis `Th 1985-1992` dst. tidak) |
| Uang Primer (M0) | `I.2` |
| Cadangan Devisa | `5.9` |
| NPI | `5.1` |
| PII | `5.39` |
| SULNI | `TabI.1`–`TabI.7`, `Tbl II.1`–`Tbl II.6`, `Tbl III.1`–`Tbl III.10` (bukan `Tbl II.7`, `Tbl II.8`) |
| SK | `Tabel 1`–`Tabel 9` |
| SKDU | `T1 …`–`T10 …`, `T7b …` |
| SPE | `Tabel 1`–`Tabel 9` |
| SBank | `Tabel1`–`Tabel4`, `Tabel 5 (disc)` |
| SHPR | `TABEL 1`–`TABEL 3` |
| PMI | `T1 …`, `T2 …` |

## Sampel dan angka emas

### Struktur folder

File BI (Excel, PDF) ada di folder ini secara lokal tetapi di-gitignore; hanya `golden.json`
yang di-commit.

```
tests/fixtures/publications/
  <kunci-publikasi>/
    <periode data>/             # YYYY-MM atau YYYY-Qn: periode yang dibahas rilisnya
      <file Excel acuan, nama asli>
      siaran_pers.pdf           # halaman siaran pers dicetak ke PDF
      laporan.pdf               # bila publikasi punya laporan
      golden.json
```

**Pembaruan:** folder dinamai menurut **periode data** yang dibahas rilis (ULN Juli 2026 →
`sulni/2026-07`, NPI Tw II → `npi/2026-Q2`), bukan bulan rilis — satu rilis selalu membahas satu
periode, sedangkan file Excel-nya bisa sudah berisi bulan sesudahnya (tabel M0 unduhan September
memuat Juli yang dibahas laporan Juli).

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
| PMI | zip `PMI-Triwulan-I-2026.zip` di halaman Laporan BI |

### `golden.json`

**Pembaruan:** periode terakhir dicatat per sheet (satu workbook bisa bulanan dan triwulanan,
mis. SPE Tabel 1 vs Tabel 4), dan setiap angka menyebut workbook-nya (UB dan SULNI punya
beberapa workbook).

```json
{
  "publication": "sulni",
  "data_period": "2026-07",
  "release": "Siaran Pers No.28/188/DKom, 15 September 2026; zip SULNI edisi September 2026",
  "sheets": {
    "TABEL_INDONESIA Sep26_value.xlsx": {
      "TabI.1": {"year": 2026, "period": "Jul"},
      "TabI.7": {"year": 2026, "period": "Q2"}
    }
  },
  "values": [
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "TOTAL",
     "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001, "decimals": 1,
     "expected": 454.8, "quote": "Posisi ULN Indonesia pada Juli 2026 tercatat sebesar 454,8 miliar dolar AS"},
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "Swasta",
     "query": "ULN swasta", "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1,
     "expected": -1.2, "quote": "mengalami kontraksi sebesar 1,2% (yoy)"}
  ]
}
```

- `kind`: `value` (nilai di sel) atau `yoy` (dihitung dari periode yang sama tahun sebelumnya).
- `scale` (opsional): pengali dari satuan tabel ke satuan narasi.
- `decimals`: presisi angka di siaran pers; cocok bila nilai tabel dibulatkan ke presisi itu
  sama dengan angka rilis (selisih ≤ setengah satuan terakhir).
- `query` (opsional): frasa narasi yang harus mendarat di `label` lewat `lookup_fuzzy`.
- `quote`: kutipan dari siaran pers/laporan, untuk penelusuran.
- Target 5–15 angka per publikasi; Cadev hanya punya dua angka yang ada di tabel (posisi akhir
  bulan ini dan bulan lalu), dan itu cukup.

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

Branch `publication-parsers`, commit per langkah. **Pembaruan:** sampel ke-12 publikasi sudah
terkumpul di `tests/fixtures/publications/` (belum di-commit); urutan rinci ada di rencana
implementasi:

1. **Fondasi:** pembacaan workbook sekali, `_common.py` (sel header, label, mesin deret waktu),
   registri, parameter `publication` diteruskan ke `_parse_table_with_fallback`, cadangan ke
   rantai lama; modul UB, M0, Cadev (pembungkus parser BI).
2. **Tes angka emas:** aturan `.gitignore` untuk file BI, harness tes, angka emas UB, M0, Cadev.
3. **SULNI**, lalu **NPI/PII**, **SK/SKDU**, **SPE**, **SBank**, **SHPR**, **PMI** — masing-masing
   langsung dengan angka emasnya.
4. **Uji ujung-ke-ujung** ke-12 publikasi dan laporannya.

## Risiko

- **Format berubah antar edisi** (kolom/judul bergeser): tes emas gagal di edisi baru, dan
  di aplikasi parser publikasi melempar galat lalu jatuh ke rantai lama — terdeteksi, tidak diam.
- **Tes emas tidak jalan di CI** karena file BI tidak di-commit: perubahan parser yang merusak
  sebuah publikasi baru ketahuan saat tes dijalankan di lokal. Mitigasi: menjalankan tes emas
  lokal adalah syarat sebelum merge; tes unit `_common.py` dengan grid sintetis tetap di CI.
- **PMI:** sumber Excel resmi belum pasti; bila berbeda dari sampel, parser PMI menyesuaikan.
- **Label Inggris** di narasi: dijaga tes `query`; alias di `TableData` disiapkan sebagai
  pekerjaan lanjutan bila perlu.
