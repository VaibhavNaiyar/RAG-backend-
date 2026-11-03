"""Every test runs offline: synthetic corpus, hash embedder, fake LLM, no exporters."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update({
    "CC_SOURCES_FILE": "tests/fixtures/sources.yaml", "CC_INDEX_DIR": "index-test",
    "CC_EMBEDDER": "hash", "CC_RERANKER": "lexical", "CC_LLM": "fake",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "", "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "",
})

import pytest  # noqa: E402

from claimcheck.config import get_settings  # noqa: E402
from claimcheck.ingest import build_index  # noqa: E402
from claimcheck.telemetry import setup_telemetry  # noqa: E402

setup_telemetry("claimcheck-tests")


@pytest.fixture(scope="session")
def settings():
    s = get_settings()
    build_index(s)
    return s


@pytest.fixture(scope="session")
def pipeline(settings):
    from claimcheck.pipeline import Pipeline
    return Pipeline(settings)
