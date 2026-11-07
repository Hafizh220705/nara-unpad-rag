import csv
import hashlib
from pathlib import Path

import pytest

from nara.ingestion.manifest import (
    AUDIT_REQUIRED_FIELDS,
    MANIFEST_FIELDS,
    METADATA_FIELDS,
    ManifestValidationError,
    build_document_manifest,
    document_id_for_path,
    normalize_relative_path,
)


def write_csv(
    path: Path, fieldnames: list[str] | tuple[str, ...], rows: list[dict]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def audit_row(file_path: str = "regulation.pdf", **overrides: object) -> dict[str, str]:
    filename = file_path.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    checksum = hashlib.sha256(file_path.encode()).hexdigest()
    row: dict[str, str] = {
        "file_path": file_path,
        "filename": filename,
        "file_size_bytes": "100",
        "sha256": checksum,
        "is_duplicate": "False",
        "duplicate_of": "",
        "is_pdf_signature": "True",
        "is_valid": "True",
        "is_encrypted": "False",
        "is_repaired": "False",
        "page_count": "2",
        "sample_pages_checked": "2",
        "sample_pages_with_text": "2",
        "sample_text_chars": "100",
        "is_scanned": "False",
        "ocr_recommended": "False",
        "extraction_status": "text_extractable",
        "error": "",
    }
    row.update({key: str(value) for key, value in overrides.items()})
    return row


def verified_metadata(
    file_path: str = "regulation.pdf", **overrides: str
) -> dict[str, str]:
    row = {
        "file_path": file_path,
        "title": "Peraturan Rektor tentang Pengujian",
        "document_type": "Peraturan Rektor",
        "number": "10",
        "year": "2026",
        "status": "Berlaku",
        "status_notes": "",
        "subject": "Pengujian",
        "detail_url": "https://jdih.unpad.ac.id/dokumen/uji",
        "pdf_url": "https://jdih.unpad.ac.id/dokumen/uji.pdf",
        "pdf_download_url": "",
        "determination_date": "2026-01-02",
        "promulgation_date": "2026-01-03",
        "legal_field": "Pendidikan",
        "language": "Indonesia",
        "source": "Universitas Padjadjaran",
        "signatory": "Rektor",
        "related_regulations": "",
        "metadata_source": "JDIH Unpad verified export",
        "metadata_verified_at": "2026-11-06",
    }
    row.update(overrides)
    return row


def build_fixture(
    tmp_path: Path,
    audit_rows: list[dict[str, str]],
    metadata_rows: list[dict[str, str]] | None = None,
) -> tuple[Path, Path]:
    audit_csv = tmp_path / "pdf_audit.csv"
    pdf_root = tmp_path / "pdf"
    pdf_root.mkdir()
    for row in audit_rows:
        raw = row["file_path"]
        if ".." in raw or Path(raw).is_absolute():
            continue
        target = pdf_root.joinpath(*raw.replace("\\", "/").split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.4\n%%EOF\n")
    write_csv(audit_csv, AUDIT_REQUIRED_FIELDS, audit_rows)

    metadata_csv = None
    if metadata_rows is not None:
        metadata_csv = tmp_path / "metadata.csv"
        fields = list(dict.fromkeys(key for row in metadata_rows for key in row))
        write_csv(metadata_csv, fields, metadata_rows)

    manifest = tmp_path / "documents_manifest.csv"
    review = tmp_path / "metadata_review.csv"
    build_document_manifest(
        audit_csv=audit_csv,
        pdf_root=pdf_root,
        metadata_csv=metadata_csv,
        manifest_output=manifest,
        review_output=review,
    )
    return manifest, review


def test_valid_audit_without_metadata_creates_extractable_review_record(
    tmp_path: Path,
) -> None:
    manifest, review = build_fixture(tmp_path, [audit_row("folder\\regulation.pdf")])

    record = read_csv(manifest)[0]
    review_record = read_csv(review)[0]

    assert list(record) == list(MANIFEST_FIELDS)
    assert record["file_path"] == "folder/regulation.pdf"
    assert record["title"] == "regulation"
    assert record["title_source"] == "filename_inferred"
    assert record["status"] == "Unknown"
    assert record["ready_for_extraction"] == "true"
    assert record["ready_for_active_index"] == "false"
    assert "metadata_not_found" in review_record["review_reason"]


def test_verified_berlaku_metadata_is_ready_for_active_index(tmp_path: Path) -> None:
    manifest, review = build_fixture(
        tmp_path,
        [audit_row("folder/regulation.pdf")],
        [verified_metadata("folder\\regulation.pdf")],
    )

    record = read_csv(manifest)[0]
    assert record["status"] == "Berlaku"
    assert record["status_raw"] == "Berlaku"
    assert record["title_source"] == "verified_metadata"
    assert record["ready_for_active_index"] == "true"
    assert read_csv(review) == []


def test_tidak_berlaku_is_preserved_but_excluded_from_active_index(
    tmp_path: Path,
) -> None:
    manifest, _ = build_fixture(
        tmp_path,
        [audit_row()],
        [verified_metadata(status="Tidak Berlaku")],
    )
    record = read_csv(manifest)[0]
    assert record["status"] == "Tidak Berlaku"
    assert record["ready_for_extraction"] == "true"
    assert record["ready_for_active_index"] == "false"


def test_unverified_berlaku_is_not_active(tmp_path: Path) -> None:
    manifest, review = build_fixture(
        tmp_path,
        [audit_row()],
        [verified_metadata(metadata_source="", metadata_verified_at="")],
    )
    record = read_csv(manifest)[0]
    assert record["status"] == "Berlaku"
    assert record["ready_for_active_index"] == "false"
    assert "metadata_unverified" in read_csv(review)[0]["review_reason"]


def test_document_ids_are_stable_and_path_based() -> None:
    assert normalize_relative_path("folder\\a.pdf") == "folder/a.pdf"
    assert document_id_for_path("folder\\a.pdf") == document_id_for_path("folder/a.pdf")
    assert document_id_for_path("folder/a.pdf") != document_id_for_path("other/a.pdf")


def test_exact_duplicate_is_not_ready_for_extraction(tmp_path: Path) -> None:
    checksum = hashlib.sha256(b"same").hexdigest()
    original = audit_row("a.pdf", sha256=checksum)
    duplicate = audit_row(
        "b.pdf",
        sha256=checksum,
        is_duplicate="True",
        duplicate_of="a.pdf",
    )
    manifest, review = build_fixture(tmp_path, [original, duplicate])
    records = {row["file_path"]: row for row in read_csv(manifest)}

    assert records["a.pdf"]["document_id"] != records["b.pdf"]["document_id"]
    assert records["b.pdf"]["ready_for_extraction"] == "false"
    assert "duplicate_pdf" in next(
        row["review_reason"] for row in read_csv(review) if row["file_path"] == "b.pdf"
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"is_valid": "False", "extraction_status": "read_error"}, "invalid_pdf"),
        (
            {"is_encrypted": "True", "extraction_status": "password_required"},
            "password_required",
        ),
    ],
)
def test_unusable_pdf_records_are_preserved_and_blocked(
    tmp_path: Path,
    overrides: dict[str, str],
    reason: str,
) -> None:
    manifest, review = build_fixture(tmp_path, [audit_row(**overrides)])
    assert read_csv(manifest)[0]["ready_for_extraction"] == "false"
    assert reason in read_csv(review)[0]["review_reason"]


def test_missing_source_url_blocks_active_index(tmp_path: Path) -> None:
    metadata = verified_metadata(detail_url="", pdf_url="")
    manifest, review = build_fixture(tmp_path, [audit_row()], [metadata])
    assert read_csv(manifest)[0]["ready_for_active_index"] == "false"
    assert "missing_source_url" in read_csv(review)[0]["review_reason"]


def test_duplicate_metadata_keys_are_not_silently_attached(tmp_path: Path) -> None:
    manifest, review = build_fixture(
        tmp_path,
        [audit_row()],
        [verified_metadata(), verified_metadata(title="Konflik")],
    )
    record = read_csv(manifest)[0]
    assert record["title_source"] == "filename_inferred"
    assert record["ready_for_active_index"] == "false"
    assert "metadata_duplicate_path" in read_csv(review)[0]["review_reason"]


def test_unmatched_duplicate_metadata_is_reported(tmp_path: Path) -> None:
    _, review = build_fixture(
        tmp_path,
        [audit_row()],
        [verified_metadata("missing.pdf"), verified_metadata("missing.pdf")],
    )
    reasons = [row["review_reason"] for row in read_csv(review)]
    assert any(
        "metadata_duplicate_path" in reason and "metadata_unmatched_path" in reason
        for reason in reasons
    )


def test_unsafe_path_is_preserved_for_review_without_file_access(
    tmp_path: Path,
) -> None:
    manifest, review = build_fixture(tmp_path, [audit_row("../outside.pdf")])
    record = read_csv(manifest)[0]
    assert record["file_path"] == "../outside.pdf"
    assert record["pdf_exists"] == "false"
    assert record["ready_for_extraction"] == "false"
    assert "unsafe_file_path" in read_csv(review)[0]["review_reason"]


def test_invalid_date_and_year_are_flagged(tmp_path: Path) -> None:
    metadata = verified_metadata(year="20xx", determination_date="31/12/2026")
    manifest, review = build_fixture(tmp_path, [audit_row()], [metadata])
    assert read_csv(manifest)[0]["ready_for_active_index"] == "false"
    reasons = read_csv(review)[0]["review_reason"]
    assert "invalid_year" in reasons
    assert "invalid_date:determination_date" in reasons


def test_invalid_verification_date_does_not_verify_metadata(tmp_path: Path) -> None:
    metadata = verified_metadata(metadata_verified_at="06/11/2026")
    manifest, review = build_fixture(tmp_path, [audit_row()], [metadata])
    record = read_csv(manifest)[0]
    assert record["metadata_verified_at"] == ""
    assert record["ready_for_active_index"] == "false"
    assert "invalid_metadata_verified_at" in read_csv(review)[0]["review_reason"]


def test_missing_required_audit_column_is_fatal(tmp_path: Path) -> None:
    row = audit_row()
    row.pop("sha256")
    audit_csv = tmp_path / "audit.csv"
    write_csv(audit_csv, list(row), [row])
    pdf_root = tmp_path / "pdf"
    pdf_root.mkdir()

    with pytest.raises(ManifestValidationError, match="sha256"):
        build_document_manifest(
            audit_csv=audit_csv,
            pdf_root=pdf_root,
            manifest_output=tmp_path / "manifest.csv",
            review_output=tmp_path / "review.csv",
        )


def test_duplicate_audit_paths_are_fatal(tmp_path: Path) -> None:
    audit_csv = tmp_path / "audit.csv"
    write_csv(audit_csv, AUDIT_REQUIRED_FIELDS, [audit_row(), audit_row()])
    pdf_root = tmp_path / "pdf"
    pdf_root.mkdir()

    with pytest.raises(ManifestValidationError, match="tidak unik"):
        build_document_manifest(
            audit_csv=audit_csv,
            pdf_root=pdf_root,
            manifest_output=tmp_path / "manifest.csv",
            review_output=tmp_path / "review.csv",
        )


def test_unmatched_metadata_is_written_to_review_queue(tmp_path: Path) -> None:
    _, review = build_fixture(
        tmp_path,
        [audit_row()],
        [verified_metadata("missing.pdf")],
    )
    rows = read_csv(review)
    assert any("metadata_unmatched_path" in row["review_reason"] for row in rows)


def test_repeated_execution_is_idempotent(tmp_path: Path) -> None:
    audit_csv = tmp_path / "audit.csv"
    pdf_root = tmp_path / "pdf"
    pdf_root.mkdir()
    (pdf_root / "regulation.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    write_csv(audit_csv, AUDIT_REQUIRED_FIELDS, [audit_row()])
    manifest = tmp_path / "manifest.csv"
    review = tmp_path / "review.csv"

    kwargs = {
        "audit_csv": audit_csv,
        "pdf_root": pdf_root,
        "manifest_output": manifest,
        "review_output": review,
    }
    build_document_manifest(**kwargs)
    first = (manifest.read_bytes(), review.read_bytes())
    build_document_manifest(**kwargs)

    assert (manifest.read_bytes(), review.read_bytes()) == first


def test_verified_metadata_template_matches_supported_fields() -> None:
    project_root = Path(__file__).resolve().parents[1]
    template = project_root / "examples" / "metadata" / "metadata_verified_template.csv"
    with template.open(encoding="utf-8-sig", newline="") as handle:
        assert next(csv.reader(handle)) == list(METADATA_FIELDS)
