import re
import tempfile
from pathlib import Path

import pymupdf as fitz

from pdf_tools import extract_trace_details, extract_trace_id, watermark_pdf


def _make_pdf(directory, name, pages):
    source = Path(directory) / name
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(source)
    doc.close()
    return str(source)


def _watermarked(directory, pages, uid=123456789, code="789", trace="LX-ABC12345"):
    source = _make_pdf(directory, "source.pdf", pages)
    output = str(Path(directory) / "output.pdf")
    watermark_pdf(
        source, output,
        telegram_user_id=uid, visible_code=code,
        trace_id=trace, original_filename="test.pdf",
    )
    return source, output


def test_visible_code_only_on_page_two():
    with tempfile.TemporaryDirectory() as directory:
        _, output = _watermarked(directory, pages=5)

        marked = fitz.open(output)
        def visible_on(page):
            return bool(re.search(r"(?<!\d)789(?!\d)", page.get_text()))

        assert visible_on(marked[1])
        assert not visible_on(marked[0])
        assert not visible_on(marked[4])
        marked.close()


def test_no_readable_trace_in_text_layer():
    """Select-all / copy-paste / Ctrl+F must not reveal the trace."""
    with tempfile.TemporaryDirectory() as directory:
        source, output = _watermarked(directory, pages=5)

        before, after = fitz.open(source), fitz.open(output)
        for i in range(len(after)):
            copied = after[i].get_text()
            assert "LXTRACE" not in copied
            assert "LX-" not in copied
            assert "ABC12345" not in copied
            assert "123456789" not in copied
            assert "UID" not in copied
            # text layer is identical to the original page
            # (page 2 legitimately carries the visible code)
            if i != 1:
                assert copied == before[i].get_text()
        after.close()
        before.close()


def test_no_giveaway_metadata():
    with tempfile.TemporaryDirectory() as directory:
        _, output = _watermarked(directory, pages=2)

        doc = fitz.open(output)
        values = " ".join(str(v) for v in (doc.metadata or {}).values())
        for word in ("LXTRACE", "LX-", "Learn", "trace", "TGUSER", "CODE:", "DELIVERED:"):
            assert word.lower() not in values.lower()
        # the disguised tokens are present and parse back
        tokens = doc.metadata["keywords"].split(",")
        assert len(tokens) == 3
        assert tokens[1] == "ABC12345"
        assert int(tokens[2], 16) == 123456789
        doc.close()


def test_vector_marker_on_every_page():
    with tempfile.TemporaryDirectory() as directory:
        _, output = _watermarked(directory, pages=5)

        doc = fitz.open(output)
        from pdf_tools import _decode_vector_marker
        for page in doc:
            decoded = _decode_vector_marker(page)
            assert decoded is not None
            assert decoded["uid"] == 123456789
            assert decoded["trace_hex"] == "ABC12345"
        doc.close()

        details = extract_trace_details(output)
        assert details["trace_id"] == "LX-ABC12345"
        assert details["uid"] == 123456789
        assert "hidden vector layer" in details["sources"]
        assert extract_trace_id(output) == "LX-ABC12345"


def test_non_target_pages_are_pixel_identical():
    """No visible change anywhere except the page-2 code."""
    import numpy as np

    with tempfile.TemporaryDirectory() as directory:
        source, output = _watermarked(directory, pages=5)

        before, after = fitz.open(source), fitz.open(output)
        matrix = fitz.Matrix(4, 4)
        for i in range(5):
            b = np.frombuffer(before[i].get_pixmap(matrix=matrix).samples, dtype=np.uint8)
            a = np.frombuffer(after[i].get_pixmap(matrix=matrix).samples, dtype=np.uint8)
            if i == 1:
                assert np.count_nonzero(a != b) > 0  # the code
            else:
                # micro-dot antialiasing may shift a few channels by
                # 1-2/255 — far below human perception; anything stronger
                # would mean a visible mark
                diff = np.abs(a.astype(int) - b.astype(int))
                assert int(diff.max()) <= 8
                assert np.count_nonzero(diff) <= 500
        after.close()
        before.close()


def test_short_pdf_uses_last_page():
    with tempfile.TemporaryDirectory() as directory:
        source = _make_pdf(directory, "source.pdf", 1)
        output = str(Path(directory) / "output.pdf")

        watermark_pdf(
            source, output,
            telegram_user_id=111222333, visible_code="333",
            trace_id="LX-FFFF0000", original_filename="short.pdf",
        )

        marked = fitz.open(output)
        assert re.search(r"(?<!\d)333(?!\d)", marked[0].get_text())
        from pdf_tools import _decode_vector_marker
        decoded = _decode_vector_marker(marked[0])
        assert decoded and decoded["uid"] == 111222333
        assert decoded["trace_hex"] == "FFFF0000"
        marked.close()
