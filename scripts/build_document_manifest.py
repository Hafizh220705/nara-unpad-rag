"""CLI for building the NARA document manifest and metadata review queue."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from nara.ingestion.manifest import ManifestValidationError, build_document_manifest
from nara.ingestion.pdf_audit import PROJECT_ROOT

DEFAULT_AUDIT = PROJECT_ROOT / "data" / "manifests" / "pdf_audit.csv"
DEFAULT_PDF_ROOT = PROJECT_ROOT / "data" / "raw" / "pdf"
DEFAULT_MANIFEST = PROJECT_ROOT / "data" / "manifests" / "documents_manifest.csv"
DEFAULT_REVIEW = PROJECT_ROOT / "data" / "manifests" / "metadata_review.csv"
LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bangun document manifest dan metadata review queue NARA"
    )
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--pdf-root", type=Path, default=DEFAULT_PDF_ROOT)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--review-output", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Proses N dokumen pertama untuk pilot; 0 = semua",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        summary = build_document_manifest(
            audit_csv=args.audit,
            pdf_root=args.pdf_root,
            metadata_csv=args.metadata,
            manifest_output=args.output,
            review_output=args.review_output,
            limit=args.limit,
        )
    except (OSError, ValueError, ManifestValidationError) as exc:
        LOGGER.error("%s", exc)
        return 2

    LOGGER.info(
        "Total=%s | Review=%s | Extraction=%s | ActiveIndex=%s | "
        "UnmatchedMetadata=%s | DuplicateMetadataPaths=%s",
        summary.total_documents,
        summary.review_required,
        summary.ready_for_extraction,
        summary.ready_for_active_index,
        summary.unmatched_metadata,
        summary.duplicate_metadata_paths,
    )
    LOGGER.info("Manifest: %s", args.output.resolve())
    LOGGER.info("Review queue: %s", args.review_output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
