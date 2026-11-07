"""Build a trustworthy document manifest from PDF audit results."""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)

AUDIT_REQUIRED_FIELDS = (
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
)

METADATA_FIELDS = (
    "file_path",
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
    "determination_date",
    "promulgation_date",
    "legal_field",
    "language",
    "source",
    "signatory",
    "related_regulations",
    "metadata_source",
    "metadata_verified_at",
)

MANIFEST_FIELDS = (
    "document_id",
    "file_path",
    "filename",
    "sha256",
    "file_size_bytes",
    "page_count",
    "is_pdf_signature",
    "is_valid",
    "is_duplicate",
    "duplicate_of",
    "is_encrypted",
    "is_repaired",
    "sample_pages_checked",
    "sample_pages_with_text",
    "sample_text_chars",
    "is_scanned",
    "extraction_status",
    "ocr_recommended",
    "audit_error",
    "pdf_exists",
    "title",
    "document_type",
    "number",
    "year",
    "status",
    "status_raw",
    "status_notes",
    "subject",
    "determination_date",
    "promulgation_date",
    "legal_field",
    "language",
    "source",
    "signatory",
    "related_regulations",
    "detail_url",
    "pdf_url",
    "metadata_source",
    "metadata_verified_at",
    "title_source",
    "ready_for_extraction",
    "ready_for_active_index",
    "review_required",
    "review_reason",
)

REVIEW_FIELDS = (
    "document_id",
    "file_path",
    "filename",
    "title",
    "document_type",
    "number",
    "year",
    "status",
    "status_raw",
    "detail_url",
    "pdf_url",
    "metadata_source",
    "metadata_verified_at",
    "title_source",
    "review_reason",
    "is_valid",
    "is_duplicate",
    "is_encrypted",
    "extraction_status",
    "ocr_recommended",
    "audit_error",
)

TRUE_VALUES = {"true", "1", "yes", "y"}
FALSE_VALUES = {"false", "0", "no", "n"}
KNOWN_STATUS_VALUES = {"", "unknown", "tidak diketahui", "belum diketahui", "-"}
ACTIVE_STATUS_VALUES = {"berlaku", "aktif", "masih berlaku"}
INACTIVE_STATUS_VALUES = {"tidak berlaku", "tidak aktif", "dicabut", "sudah dicabut"}
NUMBERED_DOCUMENT_KEYWORDS = (
    "peraturan",
    "keputusan",
    "surat edaran",
    "instruksi",
)
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
YEAR_PATTERN = re.compile(r"^\d{4}$")


class ManifestValidationError(ValueError):
    """Raised when an input schema cannot produce a reliable manifest."""


@dataclass(frozen=True)
class BuildSummary:
    total_documents: int
    review_required: int
    ready_for_extraction: int
    ready_for_active_index: int
    unmatched_metadata: int
    duplicate_metadata_paths: int


@dataclass(frozen=True)
class MetadataIndex:
    rows: dict[str, dict[str, str]]
    duplicate_keys: frozenset[str]
    duplicate_rows: dict[str, dict[str, str]]
    invalid_rows: tuple[dict[str, str], ...]


def normalize_relative_path(value: str) -> str:
    """Normalize a safe relative path to POSIX form."""
    raw = value.strip()
    if not raw:
        raise ValueError("path kosong")
    if PureWindowsPath(raw).is_absolute() or PurePosixPath(raw).is_absolute():
        raise ValueError("path absolut tidak diizinkan")
    normalized = raw.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("path traversal tidak diizinkan")
    if ":" in parts[0]:
        raise ValueError("drive-qualified path tidak diizinkan")
    return PurePosixPath(*parts).as_posix()


def document_id_for_path(file_path: str) -> str:
    """Create a stable source-record identity independent of content hash."""
    canonical = normalize_relative_path(file_path).casefold()
    digest = hashlib.sha256(f"nara-document-v1\0{canonical}".encode()).hexdigest()
    return f"doc_{digest}"


def _invalid_document_id(raw_path: str, filename: str, checksum: str) -> str:
    identity = f"{raw_path.strip()}\0{filename.strip()}\0{checksum.strip()}"
    return f"doc_invalid_{hashlib.sha256(identity.encode()).hexdigest()}"


def _parse_bool(value: str, field: str, reasons: set[str]) -> bool:
    normalized = value.strip().casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    reasons.add(f"invalid_boolean:{field}")
    return False


def _parse_nonnegative_int(value: str, field: str, reasons: set[str]) -> int | None:
    try:
        parsed = int(value.strip())
    except (TypeError, ValueError):
        reasons.add(f"invalid_integer:{field}")
        return None
    if parsed < 0:
        reasons.add(f"invalid_integer:{field}")
        return None
    return parsed


def _normalize_status(value: str, reasons: set[str]) -> str:
    normalized = " ".join(value.split()).casefold()
    if normalized in ACTIVE_STATUS_VALUES:
        return "Berlaku"
    if normalized in INACTIVE_STATUS_VALUES:
        return "Tidak Berlaku"
    if normalized not in KNOWN_STATUS_VALUES:
        reasons.add("unsupported_status")
    return "Unknown"


def _normalize_date(value: str, field: str, reasons: set[str]) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        reasons.add(f"invalid_date:{field}")
        return raw


def _normalize_verified_at(value: str, reasons: set[str]) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw).isoformat()
        return datetime.fromisoformat(raw).isoformat()
    except ValueError:
        reasons.add("invalid_metadata_verified_at")
        return ""


def _valid_url(value: str, field: str, reasons: set[str]) -> bool:
    raw = value.strip()
    if not raw:
        return False
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        reasons.add(f"invalid_url:{field}")
        return False
    return True


def _normalize_year(value: str, reasons: set[str]) -> str:
    raw = value.strip()
    if not raw:
        return ""
    if not YEAR_PATTERN.fullmatch(raw):
        reasons.add("invalid_year")
    return raw


def _requires_number_year(document_type: str) -> bool:
    normalized = " ".join(document_type.split()).casefold()
    return any(keyword in normalized for keyword in NUMBERED_DOCUMENT_KEYWORDS)


def _provisional_title(filename: str) -> str:
    stem = Path(filename).stem
    return " ".join(stem.replace("_", " ").split())


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _row_key(raw_path: str) -> tuple[str, str | None]:
    try:
        normalized = normalize_relative_path(raw_path)
    except ValueError as exc:
        fallback = hashlib.sha256(raw_path.strip().casefold().encode()).hexdigest()
        return f"__invalid__:{fallback}", str(exc)
    return normalized.casefold(), None


def _read_csv(path: Path) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ManifestValidationError(f"CSV tidak memiliki header: {path}")
        fieldnames = tuple(field.strip() for field in reader.fieldnames if field)
        rows = [
            {
                str(key or "").strip(): str(value or "").strip()
                for key, value in row.items()
            }
            for row in reader
        ]
    return rows, fieldnames


def load_audit_rows(path: Path) -> list[dict[str, str]]:
    rows, fieldnames = _read_csv(path)
    missing = sorted(set(AUDIT_REQUIRED_FIELDS) - set(fieldnames))
    if missing:
        raise ManifestValidationError(
            f"Kolom audit wajib tidak tersedia: {', '.join(missing)}"
        )
    keys: list[str] = []
    for row in rows:
        key, _ = _row_key(row.get("file_path", ""))
        keys.append(key)
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise ManifestValidationError(
            "file_path audit tidak unik: " + ", ".join(duplicates[:5])
        )
    return rows


def load_metadata_index(path: Path | None) -> MetadataIndex:
    if path is None:
        return MetadataIndex({}, frozenset(), {}, ())
    rows, fieldnames = _read_csv(path)
    if "file_path" not in fieldnames:
        raise ManifestValidationError("Metadata CSV wajib memiliki kolom file_path")

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    invalid_rows: list[dict[str, str]] = []
    for row in rows:
        key, error = _row_key(row.get("file_path", ""))
        if error:
            invalid = dict(row)
            invalid["_metadata_path_error"] = error
            invalid_rows.append(invalid)
            continue
        grouped[key].append(row)

    duplicate_keys = frozenset(
        key for key, values in grouped.items() if len(values) > 1
    )
    unique_rows = {
        key: values[0] for key, values in grouped.items() if key not in duplicate_keys
    }
    return MetadataIndex(
        rows=unique_rows,
        duplicate_keys=duplicate_keys,
        duplicate_rows={key: grouped[key][0] for key in duplicate_keys},
        invalid_rows=tuple(invalid_rows),
    )


def _resolve_pdf(pdf_root: Path, canonical_path: str) -> tuple[Path, bool]:
    base = pdf_root.resolve()
    candidate = (base / Path(*PurePosixPath(canonical_path).parts)).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("resolved path keluar dari PDF root")
    return candidate, candidate.is_file()


def _metadata_values(
    metadata: Mapping[str, str] | None,
    filename: str,
    reasons: set[str],
) -> tuple[dict[str, str], bool]:
    if metadata is None:
        reasons.update(
            {
                "metadata_not_found",
                "metadata_unverified",
                "missing_document_type",
                "missing_source_url",
                "title_unverified",
                "unknown_legal_status",
            }
        )
        return (
            {
                "title": _provisional_title(filename),
                "document_type": "",
                "number": "",
                "year": "",
                "status": "Unknown",
                "status_raw": "",
                "status_notes": "",
                "subject": "",
                "determination_date": "",
                "promulgation_date": "",
                "legal_field": "",
                "language": "",
                "source": "",
                "signatory": "",
                "related_regulations": "",
                "detail_url": "",
                "pdf_url": "",
                "metadata_source": "",
                "metadata_verified_at": "",
                "title_source": "filename_inferred",
            },
            False,
        )

    status_raw = metadata.get("status", "").strip()
    status = _normalize_status(status_raw, reasons)
    if status == "Unknown":
        reasons.add("unknown_legal_status")

    title = metadata.get("title", "").strip()
    metadata_source = metadata.get("metadata_source", "").strip()
    verified_at = _normalize_verified_at(
        metadata.get("metadata_verified_at", ""), reasons
    )
    metadata_verified = bool(metadata_source and verified_at)
    if not metadata_verified:
        reasons.add("metadata_unverified")

    if not title:
        title = _provisional_title(filename)
        title_source = "filename_inferred"
        reasons.add("title_unverified")
    elif metadata_verified:
        title_source = "verified_metadata"
    else:
        title_source = "metadata_csv_unverified"
        reasons.add("title_unverified")

    document_type = metadata.get("document_type", "").strip()
    if not document_type:
        reasons.add("missing_document_type")

    number = metadata.get("number", "").strip()
    year = _normalize_year(metadata.get("year", ""), reasons)
    if _requires_number_year(document_type):
        if not number:
            reasons.add("missing_number")
        if not year:
            reasons.add("missing_year")

    detail_url = metadata.get("detail_url", "").strip()
    pdf_url = (
        metadata.get("pdf_url", "").strip()
        or metadata.get("pdf_download_url", "").strip()
    )
    detail_url_valid = _valid_url(detail_url, "detail_url", reasons)
    pdf_url_valid = _valid_url(pdf_url, "pdf_url", reasons)
    if not detail_url_valid and not pdf_url_valid:
        reasons.add("missing_source_url")

    values = {
        "title": title,
        "document_type": document_type,
        "number": number,
        "year": year,
        "status": status,
        "status_raw": status_raw,
        "status_notes": metadata.get("status_notes", "").strip(),
        "subject": metadata.get("subject", "").strip(),
        "determination_date": _normalize_date(
            metadata.get("determination_date", ""), "determination_date", reasons
        ),
        "promulgation_date": _normalize_date(
            metadata.get("promulgation_date", ""), "promulgation_date", reasons
        ),
        "legal_field": metadata.get("legal_field", "").strip(),
        "language": metadata.get("language", "").strip(),
        "source": metadata.get("source", "").strip(),
        "signatory": metadata.get("signatory", "").strip(),
        "related_regulations": metadata.get("related_regulations", "").strip(),
        "detail_url": detail_url,
        "pdf_url": pdf_url,
        "metadata_source": metadata_source,
        "metadata_verified_at": verified_at,
        "title_source": title_source,
    }
    return values, metadata_verified


def _audit_values(row: Mapping[str, str], reasons: set[str]) -> dict[str, object]:
    checksum = row.get("sha256", "").strip().lower()
    if not SHA256_PATTERN.fullmatch(checksum):
        reasons.add("invalid_checksum")

    integer_fields = (
        "file_size_bytes",
        "page_count",
        "sample_pages_checked",
        "sample_pages_with_text",
        "sample_text_chars",
    )
    integers = {
        field: _parse_nonnegative_int(row.get(field, ""), field, reasons)
        for field in integer_fields
    }
    booleans = {
        field: _parse_bool(row.get(field, ""), field, reasons)
        for field in (
            "is_pdf_signature",
            "is_valid",
            "is_duplicate",
            "is_encrypted",
            "is_repaired",
            "is_scanned",
            "ocr_recommended",
        )
    }
    audit_error = row.get("error", "").strip()
    if audit_error:
        reasons.add("audit_error")
    if not booleans["is_valid"]:
        reasons.add("invalid_pdf")
    if booleans["is_duplicate"]:
        reasons.add("duplicate_pdf")
    if booleans["is_encrypted"] or row.get("extraction_status") == "password_required":
        reasons.add("password_required")

    return {
        "sha256": checksum,
        **integers,
        **booleans,
        "duplicate_of": row.get("duplicate_of", "").strip(),
        "extraction_status": row.get("extraction_status", "").strip(),
        "audit_error": audit_error,
    }


def _build_manifest_record(
    row: Mapping[str, str],
    metadata_index: MetadataIndex,
    pdf_root: Path,
    audit_keys: frozenset[str],
    checksum_by_key: Mapping[str, str],
) -> dict[str, str]:
    reasons: set[str] = set()
    raw_path = row.get("file_path", "").strip()
    filename = row.get("filename", "").strip()
    key, path_error = _row_key(raw_path)
    if path_error:
        reasons.add("unsafe_file_path")
        file_path = raw_path.replace("\\", "/")
        document_id = _invalid_document_id(raw_path, filename, row.get("sha256", ""))
        pdf_exists = False
    else:
        file_path = normalize_relative_path(raw_path)
        document_id = document_id_for_path(file_path)
        try:
            _, pdf_exists = _resolve_pdf(pdf_root, file_path)
        except ValueError:
            pdf_exists = False
            reasons.add("unsafe_file_path")
        if not pdf_exists:
            reasons.add("missing_pdf_file")
        if filename and PurePosixPath(file_path).name.casefold() != filename.casefold():
            reasons.add("filename_path_mismatch")

    audit = _audit_values(row, reasons)

    if key in metadata_index.duplicate_keys:
        reasons.add("metadata_duplicate_path")
        metadata = None
    else:
        metadata = metadata_index.rows.get(key)
    legal, metadata_verified = _metadata_values(metadata, filename, reasons)

    duplicate_of = str(audit["duplicate_of"])
    is_duplicate = bool(audit["is_duplicate"])
    if is_duplicate:
        duplicate_key, duplicate_error = _row_key(duplicate_of)
        if (
            duplicate_error
            or duplicate_key not in audit_keys
            or checksum_by_key.get(duplicate_key) != audit["sha256"]
        ):
            reasons.add("duplicate_reference_invalid")
    elif duplicate_of:
        reasons.add("duplicate_reference_inconsistent")

    extraction_status = str(audit["extraction_status"])
    ready_for_extraction = bool(
        pdf_exists
        and audit["is_valid"]
        and audit["is_pdf_signature"]
        and not is_duplicate
        and not audit["is_encrypted"]
        and extraction_status != "password_required"
        and "unsafe_file_path" not in reasons
        and "invalid_checksum" not in reasons
    )

    has_valid_source_url = _valid_url(
        legal["detail_url"], "detail_url", set()
    ) or _valid_url(legal["pdf_url"], "pdf_url", set())
    core_identity_ready = bool(
        legal["title"]
        and legal["title_source"] == "verified_metadata"
        and legal["document_type"]
        and (
            not _requires_number_year(legal["document_type"])
            or (legal["number"] and YEAR_PATTERN.fullmatch(legal["year"]))
        )
    )
    blocking_reasons = {
        "audit_error",
        "metadata_duplicate_path",
        "unsafe_file_path",
        "invalid_checksum",
        "invalid_year",
        "unsupported_status",
        "duplicate_reference_invalid",
        "filename_path_mismatch",
    }
    has_blocking_reason = bool(reasons.intersection(blocking_reasons)) or any(
        reason.startswith(
            (
                "invalid_boolean:",
                "invalid_date:",
                "invalid_integer:",
                "invalid_url:",
            )
        )
        for reason in reasons
    )
    ready_for_active_index = bool(
        ready_for_extraction
        and legal["status"] == "Berlaku"
        and metadata_verified
        and has_valid_source_url
        and core_identity_ready
        and not has_blocking_reason
    )

    review_reason = "|".join(sorted(reasons))
    record: dict[str, str] = {
        "document_id": document_id,
        "file_path": file_path,
        "filename": filename,
        "sha256": str(audit["sha256"]),
        "file_size_bytes": _optional_int_text(audit["file_size_bytes"]),
        "page_count": _optional_int_text(audit["page_count"]),
        "is_pdf_signature": _bool_text(bool(audit["is_pdf_signature"])),
        "is_valid": _bool_text(bool(audit["is_valid"])),
        "is_duplicate": _bool_text(is_duplicate),
        "duplicate_of": duplicate_of.replace("\\", "/"),
        "is_encrypted": _bool_text(bool(audit["is_encrypted"])),
        "is_repaired": _bool_text(bool(audit["is_repaired"])),
        "sample_pages_checked": _optional_int_text(audit["sample_pages_checked"]),
        "sample_pages_with_text": _optional_int_text(audit["sample_pages_with_text"]),
        "sample_text_chars": _optional_int_text(audit["sample_text_chars"]),
        "is_scanned": _bool_text(bool(audit["is_scanned"])),
        "extraction_status": extraction_status,
        "ocr_recommended": _bool_text(bool(audit["ocr_recommended"])),
        "audit_error": str(audit["audit_error"]),
        "pdf_exists": _bool_text(pdf_exists),
        **legal,
        "ready_for_extraction": _bool_text(ready_for_extraction),
        "ready_for_active_index": _bool_text(ready_for_active_index),
        "review_required": _bool_text(bool(reasons)),
        "review_reason": review_reason,
    }
    return record


def _optional_int_text(value: object) -> str:
    return "" if value is None else str(value)


def _review_record(record: Mapping[str, str]) -> dict[str, str]:
    return {field: record.get(field, "") for field in REVIEW_FIELDS}


def _orphan_metadata_review(
    row: Mapping[str, str], reason_codes: Iterable[str]
) -> dict[str, str]:
    raw_path = row.get("file_path", "").strip().replace("\\", "/")
    return {
        "document_id": "",
        "file_path": raw_path,
        "filename": PurePosixPath(raw_path).name if raw_path else "",
        "title": row.get("title", "").strip(),
        "document_type": row.get("document_type", "").strip(),
        "number": row.get("number", "").strip(),
        "year": row.get("year", "").strip(),
        "status": "Unknown",
        "status_raw": row.get("status", "").strip(),
        "detail_url": row.get("detail_url", "").strip(),
        "pdf_url": row.get("pdf_url", "").strip(),
        "metadata_source": row.get("metadata_source", "").strip(),
        "metadata_verified_at": row.get("metadata_verified_at", "").strip(),
        "title_source": "metadata_csv_unmatched",
        "review_reason": "|".join(sorted(set(reason_codes))),
        "is_valid": "",
        "is_duplicate": "",
        "is_encrypted": "",
        "extraction_status": "",
        "ocr_recommended": "",
        "audit_error": "",
    }


def build_records(
    audit_rows: Sequence[Mapping[str, str]],
    metadata_index: MetadataIndex,
    pdf_root: Path,
    *,
    limit: int = 0,
) -> tuple[list[dict[str, str]], list[dict[str, str]], int]:
    """Build deterministic manifest and review records without writing files."""
    if limit < 0:
        raise ValueError("limit tidak boleh negatif")

    keyed_rows: list[tuple[str, Mapping[str, str]]] = []
    checksum_by_key: dict[str, str] = {}
    for row in audit_rows:
        key, _ = _row_key(row.get("file_path", ""))
        keyed_rows.append((key, row))
        checksum_by_key[key] = row.get("sha256", "").strip().lower()
    keyed_rows.sort(key=lambda item: item[0])
    all_audit_keys = frozenset(key for key, _ in keyed_rows)
    if limit:
        keyed_rows = keyed_rows[:limit]

    manifests = [
        _build_manifest_record(
            row,
            metadata_index,
            pdf_root,
            all_audit_keys,
            checksum_by_key,
        )
        for _, row in keyed_rows
    ]
    ids = [record["document_id"] for record in manifests]
    duplicate_ids = [key for key, count in Counter(ids).items() if count > 1]
    if duplicate_ids:
        raise ManifestValidationError(
            "document_id tidak unik: " + ", ".join(duplicate_ids[:5])
        )

    reviews = [
        _review_record(record)
        for record in manifests
        if record["review_required"] == "true"
    ]

    unmatched_count = 0
    for key, row in sorted(metadata_index.rows.items()):
        if key not in all_audit_keys:
            reviews.append(_orphan_metadata_review(row, {"metadata_unmatched_path"}))
            unmatched_count += 1
    for key, row in sorted(metadata_index.duplicate_rows.items()):
        if key not in all_audit_keys:
            reviews.append(
                _orphan_metadata_review(
                    row,
                    {"metadata_duplicate_path", "metadata_unmatched_path"},
                )
            )
            unmatched_count += 1
    for row in metadata_index.invalid_rows:
        reviews.append(
            _orphan_metadata_review(
                row,
                {"metadata_invalid_path", row.get("_metadata_path_error", "")},
            )
        )
        unmatched_count += 1

    reviews.sort(key=lambda row: (row["file_path"].casefold(), row["review_reason"]))
    return manifests, reviews, unmatched_count


def _write_csv_temp(
    path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, str]]
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    if temp_path is None:
        raise RuntimeError(f"Gagal membuat temporary output untuk {path}")
    return temp_path


def write_outputs_atomic(
    manifest_path: Path,
    review_path: Path,
    manifests: Sequence[Mapping[str, str]],
    reviews: Sequence[Mapping[str, str]],
) -> None:
    """Write both output files through same-directory temporary files."""
    if manifest_path.resolve() == review_path.resolve():
        raise ValueError("Output manifest dan review harus berbeda")
    manifest_temp = _write_csv_temp(manifest_path, MANIFEST_FIELDS, manifests)
    review_temp: Path | None = None
    try:
        review_temp = _write_csv_temp(review_path, REVIEW_FIELDS, reviews)
        manifest_temp.replace(manifest_path)
        review_temp.replace(review_path)
    finally:
        manifest_temp.unlink(missing_ok=True)
        if review_temp is not None:
            review_temp.unlink(missing_ok=True)


def build_document_manifest(
    *,
    audit_csv: Path,
    pdf_root: Path,
    manifest_output: Path,
    review_output: Path,
    metadata_csv: Path | None = None,
    limit: int = 0,
) -> BuildSummary:
    """Load, validate, map, and atomically write manifest artifacts."""
    if not audit_csv.is_file():
        raise FileNotFoundError(f"Audit CSV tidak ditemukan: {audit_csv}")
    if not pdf_root.is_dir():
        raise FileNotFoundError(f"PDF root tidak ditemukan: {pdf_root}")
    if metadata_csv is not None and not metadata_csv.is_file():
        raise FileNotFoundError(f"Metadata CSV tidak ditemukan: {metadata_csv}")

    input_paths = {audit_csv.resolve()}
    if metadata_csv is not None:
        input_paths.add(metadata_csv.resolve())
    if (
        manifest_output.resolve() in input_paths
        or review_output.resolve() in input_paths
    ):
        raise ValueError("Output tidak boleh menimpa input")

    audit_rows = load_audit_rows(audit_csv)
    metadata_index = load_metadata_index(metadata_csv)
    manifests, reviews, unmatched = build_records(
        audit_rows,
        metadata_index,
        pdf_root,
        limit=limit,
    )
    write_outputs_atomic(manifest_output, review_output, manifests, reviews)

    summary = BuildSummary(
        total_documents=len(manifests),
        review_required=sum(row["review_required"] == "true" for row in manifests),
        ready_for_extraction=sum(
            row["ready_for_extraction"] == "true" for row in manifests
        ),
        ready_for_active_index=sum(
            row["ready_for_active_index"] == "true" for row in manifests
        ),
        unmatched_metadata=unmatched,
        duplicate_metadata_paths=len(metadata_index.duplicate_keys),
    )
    LOGGER.info("Document manifest selesai: %s", summary)
    return summary
