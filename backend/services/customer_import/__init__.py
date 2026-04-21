"""
services.customer_import
────────────────────────
Customer import wizard backend.

Splits the four-step flow into focused, testable units:

    parser      → bytes (CSV/XLSX) → list[dict] rows + headers
    normalizer  → raw row dict → normalized contact dict (E.164 phone,
                  cleaned name/email/city/notes, source tag)
    dedupe      → normalized row → classification decision against the
                  existing tenant customer book (exact / suspect /
                  new / invalid)
    importer    → executes a commit pass that creates new customers,
                  non-destructively merges matched ones, and tracks
                  source_tags / primary_source / import_batch_id.

The router (`routers.customer_import`) glues these together and
persists per-batch state to the `customer_import_batches` table.
"""
from .parser import (  # noqa: F401
    ParsedFile,
    ParseError,
    parse_upload,
)
from .normalizer import (  # noqa: F401
    NormalizedRow,
    REQUIRED_FIELDS,
    OPTIONAL_FIELDS,
    SUPPORTED_FIELDS,
    normalize_row,
    suggest_column_mapping,
)
from .dedupe import (  # noqa: F401
    CLASSIFICATION_EXACT,
    CLASSIFICATION_INVALID,
    CLASSIFICATION_NEW,
    CLASSIFICATION_SUSPECT,
    ClassifiedRow,
    classify_rows,
)
from .importer import (  # noqa: F401
    ImportResult,
    commit_batch,
)
