# Pemetaan aturan prompt lama → prompt strict

Tanggal: 2026-10-26. Status: menunggu pemeriksaan pemilik. Belum ada yang diubah.

## Tujuan

Pelajaran yang sudah ada di jalur lama dipindahkan ke prompt strict **sekali jalan**. Sebelumnya
pelajaran ini baru ketahuan satu per satu saat pengukuran menemukan kesalahan. Putaran 4 menunjukkan
bahayanya: kuantor "sebagian besar" dan "kecuali" sudah ditangani jalur lama, tetapi belum dibawa.
Aturan putaran 3 ("PMI ekspansi = di atas 50", tanpa kuantor) lalu membuat AI menguji **semua** Sub-LU.

Sumber yang diperiksa:
- prompt lama `structured_extractor._SYSTEM_PROMPT`: bagian operasi, dua jenis tabel, aturan 1–9;
- konvensi yang dikerjakan **mesin** lama setelah ekstraksi: `_dominant_period`, baseline tren
  satu periode, `group_scope`, `_attach_group_exclusions`, `_attach_rankings` / `_check_ranking`;
- prompt strict `strict/prompt.py`, aturan 1–13.

Status:
- **Tercakup**: strict sudah punya aturan yang setara.
- **Tidak perlu**: aturan itu ada untuk pencocokan nama atau tebakan baris, dan strict tidak menebak baris.
- **Perlu dibawa**: diusulkan masuk prompt strict sebagai aturan umum.

## A. Operasi (bagian "Available operations" di prompt lama)

| Lama | Isi | Status | Di strict |
|---|---|---|---|
| value | satu titik, level | Tercakup | aturan 5, 10 |
| yoy_growth | titik tahun lalu diambil otomatis | Tercakup (bentuk lain) | aturan 5: sel tabel pertumbuhan, atau 2 sel berjarak setahun |
| average, sum | setiap bulan disebut satu per satu | Tercakup | aturan 5, 6 |
| diff | [sebelum, sesudah] | Tercakup | aturan 5, 10; mesin mengurutkan sel satu baris |
| ratio | beberapa pembilang lalu penyebut | Tercakup | aturan 5, 13 |
| tren | satu fakta per metrik; satu periode boleh, baseline dilengkapi mesin | Tercakup (bentuk lain) | aturan 5: ≥2 periode berurutan per baris; mesin menilai per baris |
| ambang | titik acuan tetap (PMI 50, SBT 0, IKK 100); nilai + ambang = dua fakta | Tercakup | aturan 5, 11 |
| tabel non-deret-waktu (`col_label`) | | Tidak perlu | sheet kategori tetap lewat jalur lama (`applies`) |

## B. Aturan bernomor prompt lama

| Lama | Isi | Status | Di strict / usulan |
|---|---|---|---|
| 1 | angka disalin persis | Tercakup | aturan 7 |
| 2 | "(yoy)" → yoy_growth | Tercakup | aturan 5 |
| 2b | kontraksi = laju negatif; is_decreasing hanya bila tanpa angka | Tercakup | aturan 7 (minus), 10 (angka = klaim nilai) |
| **2c** | perbandingan dua laju pertumbuhan = dua klaim yoy, bukan tren atas level | **Perlu dibawa** | "tabungan tumbuh 8,9% (yoy), meningkat dari 8,4%" → dua klaim pertumbuhan; "meningkat" membandingkan **lajunya**, bukan levelnya |
| **2d** | angka milik periode laporan kecuali kalimat mengikatnya ke periode lain | **Perlu dibawa** | digabung dengan aturan 4: angka tanpa periode = periode laporan; "pada Maret 2026 sebesar 8,4%" = Maret |
| 2e | (mtm)/(qtq) bukan yoy | Tercakup sebagian | aturan 5 menyebut "rate printed in its own table"; **dipertegas**: "(mtm)/(qtq)/secara bulanan" tidak pernah yoy_growth |
| 2f | perubahan sebesar X = diff bertanda | Tercakup | aturan 5, 10 |
| **2g** | satu angka untuk dua akun bernama ("transaksi modal dan finansial") = sum | **Perlu dibawa** | saat ini hanya ada di catatan NPI; dijadikan umum |
| 3 | nilai + pertumbuhan di satu kalimat = dua klaim | Tercakup | aturan 10 |
| 4 | metric_label dari daftar baris kalau cocok | Tidak perlu | strict selalu menyebut sel persis (aturan 1) |
| 4a | jangan pakai "Total" tanpa keterangan | Tidak perlu | sel menyebut tabel, jadi "Total" mana yang dimaksud selalu jelas |
| **4b / 4c** | pertahankan kata cakupan ("kredit investasi **UMKM**"); nama baris yang berulang butuh cakupan | **Perlu dibawa (sebagai aturan memilih sel)** | pilih baris dari populasi yang dibahas paragraf: kredit UMKM di paragraf UMKM, simpanan berjangka **uang kuasi** vs **DPK** |
| 5 | setiap bulan dalam rentang disebut | Tercakup | aturan 6 |
| 6 | col_label hanya untuk tabel non-waktu | Tidak perlu | lihat A |
| 7 | anchor_quote substring persis | Tercakup | aturan 7 |
| 8 | klaim rincian = seri rincian itu; satu fakta per kelompok | Tercakup | aturan 2, 6 |
| **9** | kuantor kelompok | **Perlu dibawa (inti putaran ini)** | lihat C |

## C. Kuantor kelompok (aturan lama 9) dalam bentuk strict

Jalur lama memakai `group_scope` ditambah logika mesin. Strict memakai sel dan rumus:

| Kalimat | Lama | Usulan strict |
|---|---|---|
| "**seluruh / semua** kota naik" | `group_scope='all'` | sel semua anggota, `all_members=true`, operasi tren/ambang (sudah ada, aturan 6) |
| "**sebagian besar / mayoritas** Sub-LU ekspansi" | `group_scope='majority'` | sel semua anggota, `all_members=true`, `formula` hitung: `(c1>50)+(c2>50)+...+(c14>50) > 7`, yaitu lebih dari separuh; `claimed_value_raw` kosong (kondisi). **Bukan** ambang per sel. |
| "naik di seluruh tingkat pendidikan, **kecuali** SMA (turun menjadi 118,6)" | 'all' + fakta X terpisah (`_attach_group_exclusions`) | (1) tren atas **semua anggota kecuali SMA**, `all_members=false`; (2) tren SMA ke arah sebaliknya; (3) klaim nilai 118,6 |
| "A dan B naik, **kelompok lainnya** turun" | `group_scope='rest'` | (1) A dan B; (2) semua anggota **selain** A dan B |
| "**beberapa** kota, terutama A, B" | hanya yang disebut | hanya A dan B (sudah ada, aturan 6) |
| "**12 dari 18** kota melambat" | tidak ada (komposisi) | rumus hitung, `all_members=true` (sudah ada, aturan 5) |
| "indeks **tertinggi** pada A (54,94%), B, C" | `_attach_rankings` + `_check_ranking` | **Usulan opsional**: klaim nilai per angka (sudah ada) + rumus peringkat `c_A >= max(c_lain…)` atas semua anggota. Tanpa ini, kata "tertinggi" tidak dicek, tetapi juga tidak membuat vonis salah. |

Aturan ambang di putaran 3 juga diperbaiki: *"fase ekspansi = di atas 50 **untuk setiap baris yang dicakup kalimat**; dengan 'sebagian besar' → rumus hitung (baris di atas)"*.

## D. Konvensi mesin lama yang perlu dicek di strict

| Lama | Isi | Status |
|---|---|---|
| `_dominant_period` | tren/ambang tanpa tanggal = periode laporan | **Perlu dibawa**, digabung dengan 2d: AI wajib menulis periode; yang tak bertanggal = periode laporan |
| baseline tren satu periode | mesin mencari periode sebelumnya | Tercakup: strict mewajibkan AI menyebut periode sebelumnya |
| `_reinterpret_signed_level`, NET_ALIASES | defisit / kewajiban neto | Tercakup: aturan 8, 12 |
| `_stable_band` | "relatif stabil" | Tercakup: dipakai ulang oleh mesin strict |
| penjaga baris (bagian, item absen, frasa angka, …) | menahan tebakan baris | Tidak perlu: strict tidak menebak; data yang tidak ada = aturan 3 |

## Ringkasan perubahan yang diusulkan

Ada lima tambahan, semuanya umum (lintas publikasi):
1. **Kuantor**: mayoritas → rumus hitung; kecuali → semua-kecuali-X + X; kelompok lainnya → sisanya. Aturan ambang diberi catatan kuantor.
2. **2c**: perbandingan dua laju = dua klaim pertumbuhan.
3. **2d + periode laporan**: angka atau tren tanpa periode = periode laporan.
4. **2g**: satu angka untuk dua akun = sum.
5. **4b/4c**: memilih sel dari populasi yang dibahas paragraf (UMKM, uang kuasi vs DPK).

Ditambah dua penegasan: (mtm)/(qtq) bukan yoy (2e), dan rumus peringkat untuk "tertinggi" (opsional).

## Pengukuran setelah perubahan

- 3 run DeepSeek strict + Tinjauan AI pada 19 dokumen yang sama.
- Dianggap berhasil hanya kalau **ketiga run** memenuhi syarat dibanding sistem sekarang (Gemini + Tinjauan AI: Tidak Sesuai palsu 4, kesalahan nyata 11/20, benar 87,4%).
- Lalu edisi keempat sebagai ujian yang belum pernah dilihat.
