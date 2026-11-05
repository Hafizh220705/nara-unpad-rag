"""CLI audit PDF lokal untuk NARA Unpad.

Contoh:
    python scripts/audit_pdf.py
    python scripts/audit_pdf.py --limit 20
    python scripts/audit_pdf.py --sample-pages 5 --min-chars 40
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from nara.ingestion.pdf_audit import PROJECT_ROOT, run_audit

DEFAULT_INPUT = PROJECT_ROOT / "data" / "raw" / "pdf"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "manifests" / "pdf_audit.csv"
LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit PDF di data/raw/pdf untuk pipeline NARA Unpad"
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--sample-pages",
        type=int,
        default=5,
        help="Banyak halaman sampel per PDF (default: 5)",
    )
    parser.add_argument(
        "--min-chars",
        type=int,
        default=40,
        help="Batas minimal karakter per halaman untuk indikasi teks (default: 40)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Audit N PDF pertama; 0 = semua (default: 0)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    try:
        run_audit(
            args.input_dir,
            args.output,
            sample_pages=args.sample_pages,
            min_chars=args.min_chars,
            limit=args.limit,
        )
    except (OSError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
