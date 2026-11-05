import csv
import json
import shutil
from pathlib import Path

import pymupdf
import pytest

from nara.ingestion.pdf_audit import (
    PROJECT_ROOT,
    audit_one,
    path_for_summary,
    run_audit,
    select_sample_pages,
)


def write_pdf(path: Path, text: str = "") -> None:
    document = pymupdf.open()
    page = document.new_page()
    if text:
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def test_select_sample_pages_covers_document_boundaries() -> None:
    selected = select_sample_pages(total=10, maximum=5)

    assert selected[0] == 0
    assert selected[-1] == 9
    assert len(selected) == 5
    assert selected == sorted(set(selected))


def test_path_for_summary_is_portable_inside_repository(tmp_path: Path) -> None:
    repository_path = PROJECT_ROOT / "data" / "manifests" / "pdf_audit.csv"

    assert path_for_summary(repository_path) == "data/manifests/pdf_audit.csv"
    assert path_for_summary(tmp_path.resolve()) == str(tmp_path.resolve())


def test_audit_one_distinguishes_text_and_low_text_pdf(tmp_path: Path) -> None:
    text_pdf = tmp_path / "text.pdf"
    blank_pdf = tmp_path / "blank.pdf"
    write_pdf(text_pdf, "Peraturan Universitas Padjadjaran " * 4)
    write_pdf(blank_pdf)

    text_record = audit_one(text_pdf, tmp_path, sample_pages=5, min_chars=40)
    blank_record = audit_one(blank_pdf, tmp_path, sample_pages=5, min_chars=40)

    assert text_record["is_valid"] is True
    assert text_record["extraction_status"] == "text_extractable"
    assert text_record["ocr_recommended"] is False
    assert blank_record["is_valid"] is True
    assert blank_record["extraction_status"] == "likely_scanned_or_low_text"
    assert blank_record["ocr_recommended"] is True


def test_audit_one_rejects_missing_pdf_signature(tmp_path: Path) -> None:
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_text("bukan PDF", encoding="utf-8")

    record = audit_one(fake_pdf, tmp_path, sample_pages=5, min_chars=40)

    assert record["is_valid"] is False
    assert record["extraction_status"] == "invalid_signature"
    assert "Header %PDF-" in str(record["error"])


def test_run_audit_detects_duplicate_and_writes_summary(tmp_path: Path) -> None:
    input_dir = tmp_path / "pdf"
    input_dir.mkdir()
    write_pdf(input_dir / "original.pdf", "Dokumen audit dengan teks yang cukup.")
    shutil.copyfile(input_dir / "original.pdf", input_dir / "duplicate.pdf")
    output = tmp_path / "manifests" / "pdf_audit.csv"

    summary = run_audit(input_dir, output, sample_pages=2, min_chars=10)

    with output.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    stored_summary = json.loads(
        output.with_name("pdf_audit_summary.json").read_text(encoding="utf-8")
    )

    assert summary["total_pdf"] == 2
    assert summary["duplicates"] == 1
    assert len(rows) == 2
    assert sum(row["is_duplicate"] == "True" for row in rows) == 1
    assert stored_summary["duplicates"] == 1


@pytest.mark.parametrize(
    ("sample_pages", "min_chars", "limit"),
    [(0, 40, 0), (5, 0, 0), (5, 40, -1)],
)
def test_run_audit_rejects_invalid_configuration(
    tmp_path: Path,
    sample_pages: int,
    min_chars: int,
    limit: int,
) -> None:
    with pytest.raises(ValueError):
        run_audit(
            tmp_path,
            tmp_path / "audit.csv",
            sample_pages=sample_pages,
            min_chars=min_chars,
            limit=limit,
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
