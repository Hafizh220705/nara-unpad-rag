"""Import permitted, locally acquired JDIH PDFs into NARA's raw corpus."""

from __future__ import annotations

import argparse
from pathlib import Path

from nara.ingestion.local_import import import_documents, read_metadata

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Impor PDF JDIH yang diunduh manual atau diekspor resmi"
    )
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--pdf-dir", type=Path, default=ROOT / "data" / "raw" / "pdf")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "manifests" / "local_ingest.jsonl",
    )
    parser.add_argument("--limit", type=int, default=0, help="0 = semua baris")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit harus >= 0")
    if not args.metadata.is_file():
        parser.error(f"Metadata tidak ditemukan: {args.metadata}")
    if not args.input_dir.is_dir():
        parser.error(f"Direktori input tidak ditemukan: {args.input_dir}")

    summary = import_documents(
        read_metadata(args.metadata),
        input_dir=args.input_dir,
        pdf_dir=args.pdf_dir,
        manifest=args.manifest,
        limit=args.limit,
    )
    print(
        f"Imported={summary.imported} | Skipped={summary.skipped} "
        f"| Failed={summary.failed}"
    )
    print(f"Manifest: {args.manifest}")
    if summary.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
