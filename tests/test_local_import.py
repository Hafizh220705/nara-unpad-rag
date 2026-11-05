import csv
import json
from pathlib import Path

import pytest

from nara.ingestion.local_import import (
    METADATA_INPUT_FIELDS,
    classify_legal_status,
    import_documents,
    validate_local_pdf,
)


def write_pdf(path: Path, body: bytes = b"test") -> None:
    path.write_bytes(b"%PDF-1.4\n" + body + b"\n%%EOF\n")


def test_metadata_template_matches_import_interface() -> None:
    project_root = Path(__file__).resolve().parents[1]
    template = (
        project_root / "examples" / "metadata" / "jdih_local_metadata_template.csv"
    )

    with template.open(encoding="utf-8-sig", newline="") as handle:
        fieldnames = next(csv.reader(handle))

    assert fieldnames == list(METADATA_INPUT_FIELDS)


def test_import_is_deterministic_and_resumable(tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    write_pdf(input_dir / "source.pdf")
    manifest = tmp_path / "manifest.jsonl"
    pdf_dir = tmp_path / "pdf"
    records = [
        {
            "title": "Peraturan Uji",
            "detail_url": "https://jdih.unpad.ac.id/dokumen/uji",
            "pdf_download_url": "https://jdih.unpad.ac.id/dokumen/uji/pdf/download",
            "source_file": "source.pdf",
            "source_content_type": "application/pdf",
            "status": "Berlaku",
        }
    ]

    first = import_documents(
        records,
        input_dir=input_dir,
        pdf_dir=pdf_dir,
        manifest=manifest,
    )
    second = import_documents(
        records,
        input_dir=input_dir,
        pdf_dir=pdf_dir,
        manifest=manifest,
    )

    assert first.imported == 1
    assert second.skipped == 1
    lines = manifest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    output = json.loads(lines[0])
    assert output["document_id"] == "8bf2803c22664f5f"
    assert output["download_status"] == "imported"
    assert output["index_eligible"] is True
    assert (pdf_dir / f"{output['document_id']}.pdf").is_file()


def test_duplicate_pdf_checksum_is_skipped(tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    write_pdf(input_dir / "a.pdf", b"same")
    write_pdf(input_dir / "b.pdf", b"same")
    manifest = tmp_path / "manifest.jsonl"
    result = import_documents(
        [
            {"title": "A", "source_file": "a.pdf"},
            {"title": "B", "source_file": "b.pdf"},
        ],
        input_dir=input_dir,
        pdf_dir=tmp_path / "pdf",
        manifest=manifest,
    )

    assert result.imported == 1
    assert result.skipped == 1


def test_invalid_pdf_is_recorded_as_failure(tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    (input_dir / "fake.pdf").write_text("not a pdf", encoding="utf-8")
    manifest = tmp_path / "manifest.jsonl"

    result = import_documents(
        [{"title": "Fake", "source_file": "fake.pdf"}],
        input_dir=input_dir,
        pdf_dir=tmp_path / "pdf",
        manifest=manifest,
    )

    assert result.failed == 1
    output = json.loads(manifest.read_text(encoding="utf-8"))
    assert output["download_status"] == "failed"
    assert "Signature %PDF-" in output["error"]


def test_content_type_and_status_classification(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    write_pdf(source)

    with pytest.raises(ValueError, match="Content-Type sumber bukan PDF"):
        validate_local_pdf(source, "text/html; charset=UTF-8")

    assert classify_legal_status(" Berlaku ") == "berlaku"
    assert classify_legal_status("Tidak Berlaku") == "tidak_berlaku"
    assert classify_legal_status("") == "unknown"
    assert classify_legal_status("dicabut sebagian") == "unknown"
