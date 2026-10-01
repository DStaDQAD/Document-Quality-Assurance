# Riwayat Pengecekan (Check History) — Design

Tanggal: 2026-10-01
Status: disetujui per bagian dalam brainstorming, menunggu review spec

## Tujuan

Setiap pengecekan dokumen yang berhasil tersimpan, sehingga pengguna bisa membuka kembali
hasilnya (dokumen apa, kapan, siapa, hasil lengkapnya) tanpa mengulang pipeline yang memakan
1–3 menit dan biaya token.

### Keputusan dari pengguna

- **Tujuan:** riwayat yang bisa dibuka ulang (bukan sekadar audit/rekap, bukan bahan eval).
- **Visibilitas:** satu riwayat **bersama** untuk semua yang login (aplikasi memakai satu akun
  bersama). Nama pemeriksa diisi manual, opsional.
- **Penyimpanan:** Postgres gratis di **Neon**; fallback **SQLite lokal** bila tidak dikonfigurasi.
- **File asli (PDF/Word/Excel) tidak disimpan** — hanya hasilnya.

### Batasan

- Biaya $0. Render free tier: disk hilang setiap restart/deploy, jadi penyimpanan harus di luar.
- Data "internal biasa": boleh di cloud tepercaya, tidak di URL publik tanpa login.
- Riwayat tidak boleh pernah menggagalkan pengecekan.

### Di luar cakupan (YAGNI)

- Akun per orang / riwayat per orang.
- Menghapus entri riwayat dari UI/API.
- Penghapusan otomatis berdasarkan umur (ditambah bila kuota mendekati batas).
- Sistem migrasi skema.
- Menyimpan pengecekan yang gagal atau dibatalkan.

## Bagian 1 — Data & penyimpanan

### Modul `check_history.py`

Modul tersendiri yang tidak tahu apa-apa tentang pipeline. Antarmuka:

```python
def save_check(summary: CheckSummary, result_json: dict) -> str        # mengembalikan id
def list_checks(limit: int = 50, offset: int = 0, q: str = "") -> list[CheckSummary]
def get_check(check_id: str) -> dict | None                           # None bila tidak ada
```

- Memakai SQLAlchemy (sudah ada secara transitif lewat langchain) + `psycopg[binary]` untuk
  Postgres.
- URL database dari env `HISTORY_DATABASE_URL`. Bila kosong → SQLite `history.db` di folder
  aplikasi (gitignored + dockerignored). Sengaja terpisah dari `DATABASE_PATH` /
  `statistik_makro.db` (database ground-truth yang read-only).
- Engine dibuat sekali (lazy), `pool_pre_ping=True`, batas waktu koneksi ~10 detik
  (`connect_timeout` untuk Postgres).
- Tabel dibuat saat pertama dipakai / startup dengan `CREATE TABLE IF NOT EXISTS`
  (`metadata.create_all`).
- Bila berjalan di Render (env `RENDER` ada) tetapi `HISTORY_DATABASE_URL` kosong → satu
  `logger.warning` saat startup: riwayat akan hilang setiap deploy/restart.

### Tabel `checks`

| kolom | tipe | isi |
|---|---|---|
| `id` | string(36) PK | UUID4 (tidak bisa ditebak berurutan) |
| `created_at` | timestamp UTC | waktu pengecekan selesai; UI menampilkan WIB |
| `filename` | string | nama dokumen yang dicek |
| `file_kind` | string | `pdf` / `docx` |
| `checker_name` | string(80), nullable | nama pemeriksa, opsional |
| `mode` | string | `excel` / `internal` / `both` / `none` |
| `excel_files` | text (JSON list) | nama file Excel yang dipakai |
| `reference_files` | text (JSON list) | nama PDF/Word referensi |
| `n_facts` | int | `total_facts` |
| `n_match` | int | `entailed_count` |
| `n_mismatch` | int | `refuted_count` |
| `n_unverified` | int | `inconclusive_count` |
| `n_typos` | int | `typo_check.total_issues`, 0 bila null |
| `n_charts` | int | `len(chart_checks)` |
| `duration_s` | float | `StageTimer.total()` |
| `tokens_in`, `tokens_out` | int | dari usage handler (0 bila provider tidak melaporkan) |
| `result_gz` | bytes (bytea/blob) | `PairedVerificationResponse.model_dump(mode="json")` → JSON → gzip |

Index pada `created_at` (urutan daftar).

### Ukuran

Thumbnail grafik (base64 `data:image/...`) disimpan apa adanya karena itulah yang ditampilkan
ulang; gzip hampir tidak mengecilkannya. Perkiraan: puluhan KB per pengecekan tanpa grafik,
ratusan KB dengan grafik → ribuan pengecekan dalam ~0,5 GB free tier Neon (angka free tier
perlu dicek ulang saat membuat project).

## Bagian 2 — Kapan dan bagaimana disimpan

- **Titik simpan:** akhir `_run_paired_pipeline` di `main.py`, setelah `log_perf(...)` dan
  setelah response final dirakit. Kedua endpoint (`/api/verify-paired` dan
  `/api/verify-paired-stream`) memakai fungsi ini, jadi keduanya tersimpan tanpa duplikasi.
- **Input baru:** form field opsional `checker_name: str = ""` pada kedua endpoint, diteruskan
  ke `_run_paired_pipeline`. Di-strip dan dipotong 80 karakter; string kosong → `NULL`.
- **Yang disimpan:** hanya pengecekan yang berhasil. Error (exception) dan pembatalan
  (task di-cancel karena klien putus) tidak sampai ke titik simpan.
- **Kegagalan simpan:** `save_check` dijalankan lewat `asyncio.to_thread`. Exception apa pun
  ditangkap → `logger.warning("history save failed: ...")`, response tetap dikirim.
- **Field response baru:** `PairedVerificationResponse.history_id: Optional[str] = None` — id
  bila tersimpan, `null` bila gagal. Diisi *setelah* simpan (id dibuat sebelum simpan, hasil yang
  disimpan tidak memuat `history_id` dirinya sendiri; saat dibaca ulang, endpoint detail
  mengisinya dengan id baris).
- **Konsekuensi yang diterima:** hasil baru dikirim setelah simpan selesai (biasanya < 1 detik,
  maks. ~10 detik bila database lambat), demi status "tersimpan" yang jujur.

## Bagian 3 — Endpoint baca & UI

### Endpoint (otomatis di balik login gate, seperti semua `/api/*`)

- `GET /api/history?limit=50&offset=0&q=` → `{"items": [CheckSummary...], "has_more": bool}`,
  terbaru dulu, tanpa `result_gz`. `q` = pencarian case-insensitive pada `filename` dan
  `checker_name`. `limit` dibatasi maks. 100.
- `GET /api/history/{id}` → JSON hasil lengkap (bentuk sama persis dengan response
  `verify-paired`, `history_id` diisi), dikirim langsung dari data yang didekompres tanpa
  validasi ulang Pydantic. Bila id tidak ada → 404.
- Bila database riwayat tidak bisa dihubungi → 503 dengan pesan yang jelas.

### UI (`static/index.html`)

- **Form upload:** input opsional "Nama pemeriksa", diingat di `localStorage` (dibungkus
  try/catch), dikirim sebagai `checker_name`.
- **Sidebar:** menu baru **"Riwayat"** (ikon Material `history`) di bawah "Verifikasi";
  `PANEL_TITLES` bertambah `history: "Riwayat"`.
- **Panel Riwayat:** kotak cari (debounce, memanggil `q`), tabel kolom Waktu (WIB) · Dokumen ·
  Pemeriksa · Mode · Hasil (✓ n / ✗ n / ? n) · Durasi, tombol "Muat lagi" (50 berikutnya), dan
  state kosong/error.
- **Membuka satu entri:** ambil `/api/history/{id}`, pindah ke panel Verifikasi, render dengan
  `renderPairedResponse(data)` yang sudah ada (tampilan identik), plus banner di atas hasil:
  "Dari riwayat · 28 Sep 2026 14.13 · oleh Ivan" dan tombol "← Kembali ke riwayat".
- **Tautan bisa dibagikan:** membuka entri mengubah URL menjadi `#riwayat/<id>`; memuat
  halaman dengan hash itu (setelah login) langsung membuka entri tersebut.
- **Setelah pengecekan baru:** tanda kecil "Tersimpan di riwayat" bila `history_id` ada, atau
  peringatan "Hasil ini tidak tersimpan di riwayat" bila `null`.

## Bagian 4 — Pengujian & deploy

### Pengujian

- `tests/conftest.py`: fixture autouse yang mengarahkan `HISTORY_DATABASE_URL` ke SQLite
  sementara (`tmp_path`) per tes dan mereset engine cache — tes tidak pernah menyentuh Neon.
- `tests/test_check_history.py` (unit): simpan→ambil identik (termasuk thumbnail base64);
  daftar terurut terbaru dulu dan tanpa `result_gz`; pencarian `q` pada nama file & pemeriksa;
  `limit`/`offset`/`has_more`; id tak dikenal → `None`.
- Tes endpoint (pipeline di-mock seperti tes paired yang ada): kedua endpoint menyimpan satu
  baris dan mengembalikan `history_id`; `checker_name` tersimpan; `save_check` dipaksa gagal →
  tetap 200 dengan `history_id: null`; pipeline error → tidak ada baris; `GET /api/history` dan
  `/api/history/{id}` (termasuk 404); 401 tanpa sesi saat gate aktif.
- UI: headless Chrome lewat CDP (pola yang sudah dipakai) — jalankan satu pengecekan mock,
  buka Riwayat, klik baris, hasil tampil; buka `#riwayat/<id>` langsung.

### Deploy

1. `requirements.txt` + `psycopg[binary]`; `.env.example` + `HISTORY_DATABASE_URL=`;
   `history.db` ke `.gitignore` dan `.dockerignore`; README diperbarui.
2. Manual oleh pengguna: buat project gratis di neon.tech (region Singapura), salin connection
   string (dengan `sslmode=require`), set sebagai env var `HISTORY_DATABASE_URL` di Render.
