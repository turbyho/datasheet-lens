"""Tests of the request model of a bundle of the team server (no server needed)."""

import pytest

pytest.importorskip("fastapi")

from pydantic import ValidationError  # noqa: E402

from datasheet_lens.bundles import BACKEND_RANK, BUNDLE_BACKENDS  # noqa: E402
from datasheet_lens.team.app import Bundle  # noqa: E402
from datasheet_lens.team.sync import BUNDLE_KINDS  # noqa: E402

SHA = "a" * 64


def bundle(**fields) -> Bundle:
    values = {"document_sha": SHA, "kind": BUNDLE_KINDS[0], "format": "json", "backend": "pdfium",
              "blob_sha": SHA}
    return Bundle(**(values | fields))


def test_each_backend_is_valid():
    for backend in BACKEND_RANK:
        assert bundle(backend=backend).backend == backend


def test_an_unknown_backend_is_an_error():
    with pytest.raises(ValidationError, match=f"backend must be one of {', '.join(BACKEND_RANK)}"):
        bundle(backend="ocr")


def test_the_backend_tables_agree():
    """The model checks a backend with BACKEND_RANK, register_bundle with the
    backends of its kind (BUNDLE_BACKENDS): the two tables have the same backends."""
    assert set(BACKEND_RANK) == {b for backends in BUNDLE_BACKENDS.values() for b in backends}
