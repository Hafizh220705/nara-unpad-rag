# NARA Unpad

NARA (Navigasi Aturan dan Regulasi berbasis AI) adalah proyek RAG untuk
menelusuri dokumen hukum dan kebijakan resmi Universitas Padjadjaran.

> Tanya Aturan, Temukan Jawaban.

## Status pengembangan

Pengembangan saat ini berfokus pada fondasi data. Koleksi PDF lokal disimpan di
`data/raw/pdf/`, sedangkan hasil audit disimpan di `data/manifests/`. Data mentah
dan hasil pemrosesan tidak dilacak oleh Git sesuai konfigurasi `.gitignore`.

## Persiapan lingkungan

Proyek menggunakan Python 3.11 atau lebih baru dan layout paket `src/`.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Audit PDF

Audit tidak mengubah PDF sumber. Jalankan pilot terlebih dahulu:

```powershell
python scripts/audit_pdf.py --limit 5
```

Audit seluruh koleksi hanya ketika memang diperlukan:

```powershell
python scripts/audit_pdf.py
```

Output default:

- `data/manifests/pdf_audit.csv`
- `data/manifests/pdf_audit_summary.json`

Indikator `is_scanned` dan `ocr_recommended` merupakan heuristik berdasarkan
halaman sampel, bukan hasil OCR atau pemeriksaan setiap halaman.

## Impor dokumen lokal

Importer lokal dipakai untuk menambahkan PDF yang diperoleh secara manual atau
melalui ekspor resmi. Isi salinan template metadata di
`examples/metadata/jdih_local_metadata_template.csv`, lalu jalankan:

```powershell
python scripts/ingest_local_documents.py `
  --metadata examples/metadata/jdih_local_metadata_template.csv `
  --input-dir D:\path\ke\folder\sumber
```

PDF akan divalidasi sebelum disalin ke `data/raw/pdf/`. Hasil dan kegagalan
dicatat secara append-only di `data/manifests/local_ingest.jsonl`. Status hukum
yang tidak dinyatakan secara eksplisit akan tetap diklasifikasikan `unknown`.

## Document manifest dan metadata hukum

Stage 2 menggabungkan atribut teknis dari `pdf_audit.csv` dengan metadata hukum
terverifikasi yang bersifat opsional. PDF sumber tidak diubah.

Tanpa metadata terverifikasi:

```powershell
python scripts/build_document_manifest.py
```

Dengan metadata terverifikasi:

```powershell
python scripts/build_document_manifest.py `
  --metadata data/manifests/metadata_verified.csv
```

Template metadata tersedia di
`examples/metadata/metadata_verified_template.csv`. `file_path` adalah matching
key dan dinormalisasi ke path relatif POSIX. Baris metadata ganda atau path yang
tidak aman tidak akan ditempelkan secara diam-diam.

Output default:

- `data/manifests/documents_manifest.csv`
- `data/manifests/metadata_review.csv`

`document_id` adalah SHA-256 penuh atas canonical relative path dengan namespace
versi. ID ini mewakili source record, sedangkan kolom `sha256` tetap menjadi
identitas konten untuk deteksi duplikat.

Status hukum dinormalisasi menjadi `Berlaku`, `Tidak Berlaku`, atau `Unknown`.
Nilai sumber tetap disimpan di `status_raw`. Status `Berlaku` saja tidak cukup
untuk active index: metadata juga harus memiliki provenance, tanggal verifikasi,
source URL, judul terverifikasi, dan identitas dokumen yang memadai.

`ready_for_extraction` hanya menilai usability PDF dan tidak diblokir oleh
metadata hukum yang belum lengkap. `ready_for_active_index` bersifat lebih ketat
dan hanya bernilai benar untuk regulasi aktif yang telah terverifikasi.

Review queue menggunakan reason code yang dapat diproses mesin, antara lain:
`metadata_not_found`, `metadata_unverified`, `unknown_legal_status`,
`title_unverified`, `missing_source_url`, `metadata_duplicate_path`,
`invalid_pdf`, `duplicate_pdf`, `password_required`, `unsafe_file_path`,
`invalid_year`, dan `invalid_date:<field>`.

Keterbatasan: judul dari filename hanya provisional, status hukum tidak pernah
disimpulkan dari nama file atau isi PDF, dan validasi metadata tidak menggantikan
verifikasi manual terhadap sumber resmi JDIH Unpad.

## Pemeriksaan kualitas

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

Jangan commit atau push PDF mentah, file `.env`, maupun artefak pemrosesan besar.
