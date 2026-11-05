"""Audit PDF lokal tanpa mengubah berkas sumber."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pymupdf

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOGGER = logging.getLogger(__name__)

FIELDS = [
    "file_path",
    "filename",
    "file_size_bytes",
    "sha256",
    "is_duplicate",
    "duplicate_of",
    "is_pdf_signature",
    "is_valid",
    "is_encrypted",
    "is_repaired",
    "page_count",
    "sample_pages_checked",
    "sample_pages_with_text",
    "sample_text_chars",
    "is_scanned",
    "ocr_recommended",
    "extraction_status",
    "error",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def has_pdf_signature(path: Path) -> bool:
    """Periksa signature PDF pada 1024 byte pertama."""
    with path.open("rb") as stream:
        return b"%PDF-" in stream.read(1024)


def select_sample_pages(total: int, maximum: int) -> list[int]:
    """Pilih sampel merata dari awal, tengah, dan akhir dokumen."""
    if total <= maximum:
        return list(range(total))
    if maximum == 1:
        return [total // 2]
    return sorted({round(i * (total - 1) / (maximum - 1)) for i in range(maximum)})


def path_for_summary(path: Path) -> str:
    """Gunakan path portabel untuk lokasi di dalam repository."""
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def audit_one(path: Path, root: Path, sample_pages: int, min_chars: int) -> dict:
    """Audit satu PDF dan kembalikan record sesuai schema audit."""
    relative_path = path.relative_to(root).as_posix()
    record = dict.fromkeys(FIELDS, "")
    record.update(
        file_path=relative_path,
        filename=path.name,
        is_duplicate=False,
        is_pdf_signature=False,
        is_valid=False,
        is_encrypted=False,
        is_repaired=False,
        ocr_recommended="",
        is_scanned="",
        extraction_status="not_checked",
    )

    try:
        record["file_size_bytes"] = path.stat().st_size
        record["sha256"] = sha256_file(path)
        record["is_pdf_signature"] = has_pdf_signature(path)

        if not record["is_pdf_signature"]:
            record["extraction_status"] = "invalid_signature"
            record["error"] = "Header %PDF- tidak ditemukan pada 1024 byte pertama"
            return record

        with pymupdf.open(str(path)) as document:
            record["is_encrypted"] = bool(document.needs_pass)
            record["is_repaired"] = bool(document.is_repaired)

            if document.needs_pass:
                record["is_valid"] = True
                record["extraction_status"] = "password_required"
                return record

            total = document.page_count
            record["page_count"] = total
            if total <= 0:
                record["extraction_status"] = "no_pages"
                record["error"] = "PDF tidak memiliki halaman"
                return record

            sample = select_sample_pages(total, sample_pages)
            chars_by_page = []
            for page_index in sample:
                page_text = document.load_page(page_index).get_text("text")
                chars_by_page.append(len(page_text.strip()))

            record["sample_pages_checked"] = len(sample)
            record["sample_pages_with_text"] = sum(
                count >= min_chars for count in chars_by_page
            )
            record["sample_text_chars"] = sum(chars_by_page)

            # Heuristik ini tidak membuktikan seluruh PDF berupa hasil scan.
            low_text_pages = sum(count < min_chars for count in chars_by_page)
            needs_ocr = low_text_pages > len(sample) / 2
            record["is_scanned"] = needs_ocr
            record["ocr_recommended"] = needs_ocr
            record["is_valid"] = True
            record["extraction_status"] = (
                "likely_scanned_or_low_text" if needs_ocr else "text_extractable"
            )

    except (OSError, RuntimeError, ValueError) as exc:
        record["is_valid"] = False
        record["extraction_status"] = "read_error"
        record["error"] = f"{type(exc).__name__}: {exc}"

    return record


def run_audit(
    input_dir: Path,
    output_path: Path,
    *,
    sample_pages: int = 5,
    min_chars: int = 40,
    limit: int = 0,
) -> dict[str, object]:
    """Audit sebuah direktori PDF dan tulis CSV beserta ringkasan JSON."""
    if sample_pages < 1 or min_chars < 1 or limit < 0:
        raise ValueError("sample_pages dan min_chars harus >= 1; limit harus >= 0")

    input_dir = input_dir.resolve()
    output_path = output_path.resolve()

    if not input_dir.is_dir():
        raise FileNotFoundError(f"Folder PDF tidak ditemukan: {input_dir}")

    files = sorted(
        (
            item
            for item in input_dir.rglob("*")
            if item.is_file()
            and not item.is_symlink()
            and item.suffix.lower() == ".pdf"
        ),
        key=lambda item: item.as_posix().casefold(),
    )
    if limit:
        files = files[:limit]
    if not files:
        raise ValueError(f"Tidak ditemukan file PDF pada {input_dir}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    known_hashes: dict[str, str] = {}
    counts = {
        "total_pdf": 0,
        "valid_pdf": 0,
        "invalid_pdf": 0,
        "duplicates": 0,
        "password_required": 0,
        "ocr_recommended": 0,
        "text_extractable": 0,
    }

    with output_path.open("w", newline="", encoding="utf-8-sig") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()

        for index, path in enumerate(files, start=1):
            record = audit_one(path, input_dir, sample_pages, min_chars)
            checksum = record["sha256"]
            if checksum:
                if checksum in known_hashes:
                    record["is_duplicate"] = True
                    record["duplicate_of"] = known_hashes[checksum]
                    counts["duplicates"] += 1
                else:
                    known_hashes[checksum] = record["file_path"]

            counts["total_pdf"] += 1
            counts["valid_pdf" if record["is_valid"] else "invalid_pdf"] += 1
            if record["extraction_status"] == "password_required":
                counts["password_required"] += 1
            elif record["ocr_recommended"] is True:
                counts["ocr_recommended"] += 1
            elif record["extraction_status"] == "text_extractable":
                counts["text_extractable"] += 1

            writer.writerow(record)
            if index % 25 == 0 or index == len(files):
                LOGGER.info("Diproses %s/%s PDF", index, len(files))

    summary = {
        "audited_at_utc": datetime.now(UTC).isoformat(),
        "input_dir": path_for_summary(input_dir),
        "csv_path": path_for_summary(output_path),
        "sample_pages_per_pdf": sample_pages,
        "min_chars_per_page": min_chars,
        "note": "is_scanned/ocr_recommended adalah indikasi dari halaman sampel, bukan OCR atau pemeriksaan seluruh halaman.",
        **counts,
    }
    summary_path = output_path.with_name(f"{output_path.stem}_summary.json")
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
        newline="\n",
    )

    LOGGER.info("Audit selesai: %s", counts)
    LOGGER.info("CSV: %s", output_path)
    LOGGER.info("Ringkasan: %s", summary_path)
    return summary
