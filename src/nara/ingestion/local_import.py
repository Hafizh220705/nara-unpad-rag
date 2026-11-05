"""Import manually downloaded or officially exported JDIH documents."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

CATALOG_FIELDS = (
    "document_id",
    "title",
    "document_type",
    "number",
    "year",
    "status",
    "status_notes",
    "subject",
    "detail_url",
    "pdf_url",
    "pdf_download_url",
    "document_form",
    "teu_badan",
    "determination_date",
    "promulgation_date",
    "legal_field",
    "language",
    "source",
    "signatory",
    "related_regulations",
)
METADATA_INPUT_FIELDS = (
    *CATALOG_FIELDS[1:],  # document_id dibuat deterministik saat impor
    "source_file",
    "source_content_type",
)
SUCCESS_STATUSES = {"imported", "duplicate"}
PDF_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}


@dataclass(frozen=True)
class ImportSummary:
    imported: int = 0
    skipped: int = 0
    failed: int = 0


def classify_legal_status(value: str) -> str:
    """Classify only explicit source values; never infer missing legal status."""
    normalized = " ".join(value.split()).casefold()
    if normalized == "berlaku":
        return "berlaku"
    if normalized == "tidak berlaku":
        return "tidak_berlaku"
    return "unknown"


def read_metadata(path: Path) -> Iterator[dict[str, str]]:
    """Read UTF-8 CSV or newline-delimited JSON metadata."""
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            yield from csv.DictReader(handle)
        return
    if suffix == ".jsonl":
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError(f"Baris JSONL {line_number} bukan objek")
                yield {str(key): str(item or "") for key, item in value.items()}
        return
    raise ValueError("Metadata harus berformat .csv atau .jsonl")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_local_pdf(path: Path, declared_content_type: str = "") -> str:
    """Validate a local PDF and return its SHA-256 checksum."""
    if not path.is_file():
        raise ValueError(f"Berkas tidak ditemukan: {path}")
    if path.suffix.casefold() != ".pdf":
        raise ValueError(f"Ekstensi berkas bukan .pdf: {path.name}")
    media_type = declared_content_type.partition(";")[0].strip().casefold()
    if media_type and media_type not in PDF_CONTENT_TYPES:
        raise ValueError(f"Content-Type sumber bukan PDF: {declared_content_type}")
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise ValueError(f"Signature %PDF- tidak ditemukan: {path.name}")
    return sha256_file(path)


def _safe_source_path(input_dir: Path, value: str) -> Path:
    if not value:
        raise ValueError("Kolom source_file/local_pdf/filename kosong")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError("Path sumber harus relatif terhadap --input-dir")
    base = input_dir.resolve()
    source = (base / relative).resolve()
    if source != base and base not in source.parents:
        raise ValueError("Path sumber keluar dari --input-dir")
    return source


def _document_id(record: Mapping[str, str], checksum: str = "") -> str:
    detail_url = record.get("detail_url", "").strip()
    if detail_url:
        parsed = urlsplit(detail_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"detail_url tidak valid: {detail_url}")
        identity = detail_url.rstrip("/")
    elif checksum:
        identity = checksum
    else:
        identity = "|".join(
            record.get(key, "").strip() for key in ("title", "number", "year")
        )
        if not identity.strip("|"):
            raise ValueError("Metadata tidak memiliki identitas dokumen")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _load_completed(manifest: Path) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    checksums: set[str] = set()
    if not manifest.is_file():
        return ids, checksums
    with manifest.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("download_status") not in SUCCESS_STATUSES:
                continue
            if row.get("document_id"):
                ids.add(row["document_id"])
            if row.get("pdf_sha256"):
                checksums.add(row["pdf_sha256"])
    return ids, checksums


def _append_jsonl(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def _base_output(record: Mapping[str, str]) -> dict[str, object]:
    output: dict[str, object] = {
        field: record.get(field, "").strip() for field in CATALOG_FIELDS
    }
    status_class = classify_legal_status(str(output["status"]))
    output.update(
        {
            "status_class": status_class,
            "index_eligible": status_class == "berlaku",
            "acquisition_method": "manual_or_official_import",
            "source_file": record.get(
                "source_file", record.get("local_pdf", record.get("filename", ""))
            ).strip(),
            "source_content_type": record.get("source_content_type", "").strip(),
            "local_pdf": "",
            "pdf_sha256": "",
            "download_status": "failed",
            "error": "",
            "scraped_at": record.get("scraped_at", "").strip()
            or datetime.now(UTC).isoformat(),
        }
    )
    return output


def import_documents(
    records: Iterable[Mapping[str, str]],
    *,
    input_dir: Path,
    pdf_dir: Path,
    manifest: Path,
    limit: int = 0,
) -> ImportSummary:
    """Validate and import PDF records with append-only checkpointing."""
    if limit < 0:
        raise ValueError("limit tidak boleh negatif")
    completed_ids, completed_checksums = _load_completed(manifest)
    imported = skipped = failed = 0

    for position, raw_record in enumerate(records, start=1):
        if limit and position > limit:
            break
        record = {str(key): str(value or "") for key, value in raw_record.items()}
        output = _base_output(record)
        try:
            source_name = str(output["source_file"])
            source = _safe_source_path(input_dir, source_name)
            checksum = validate_local_pdf(source, str(output["source_content_type"]))
            document_id = _document_id(record, checksum)
            output["document_id"] = document_id
            output["pdf_sha256"] = checksum

            if document_id in completed_ids or checksum in completed_checksums:
                skipped += 1
                continue

            pdf_dir.mkdir(parents=True, exist_ok=True)
            destination = pdf_dir / f"{document_id}.pdf"
            if destination.exists() and sha256_file(destination) != checksum:
                raise ValueError(
                    f"Tujuan {destination.name} sudah ada dengan checksum berbeda"
                )
            if not destination.exists():
                shutil.copyfile(source, destination)

            output["local_pdf"] = destination.as_posix()
            output["download_status"] = "imported"
            _append_jsonl(manifest, output)
            completed_ids.add(document_id)
            completed_checksums.add(checksum)
            imported += 1
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if not output.get("document_id"):
                try:
                    output["document_id"] = _document_id(record)
                except ValueError:
                    output["document_id"] = hashlib.sha256(
                        json.dumps(record, sort_keys=True).encode("utf-8")
                    ).hexdigest()[:16]
            output["error"] = str(exc)
            _append_jsonl(manifest, output)
            failed += 1

    return ImportSummary(imported=imported, skipped=skipped, failed=failed)
