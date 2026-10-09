"""PDF personalization for Learn-X.

Every delivered PDF carries:
- a small visible delivery code at the bottom-left of page 2
  (last page is used for single-page PDFs) — the ONLY visible mark;
- an invisible micro-dot pattern on every page. The recipient's
  Telegram ID and the trace ID are encoded in the vertical positions
  of sub-pixel rectangles. They are vector drawings, not text:
  select-all, copy-paste, and Ctrl+F cannot see them;
- innocuous-looking PDF metadata that hides the same IDs as
  comma-separated hex tokens;
- detection of the legacy plain-text marker from earlier versions.

Nothing in the file spells out "Learn-X" or "LXTRACE" outside the
delivery database.
"""
import re
import secrets

import pymupdf as fitz


# Legacy detection (PDFs delivered by earlier versions of the bot).
TRACE_PATTERN = re.compile(r"LXTRACE:([A-Z0-9-]+)")
UID_PATTERN = re.compile(r"UID:([0-9]+)")
CODE_PATTERN = re.compile(r"CODE:([0-9]+)")
DELIVERED_PATTERN = re.compile(r"DELIVERED:([^\s;]+)")

# Micro-dot vector encoding. Each bit is a rectangle smaller than a
# screen pixel, placed at one of two vertical levels. A fixed preamble
# lets the extractor find and align the run on any page.
_RECT_SIZE = 0.01      # pt; invisible even at a viewer's maximum zoom
_X_STEP = 0.6          # pt between consecutive bits
_Y_DELTA = 0.35         # vertical distance between the 0-track and 1-track
_PREAMBLE = "10101010"
_UID_BITS = 64
_TRACE_BITS = 32
_TOTAL_BITS = len(_PREAMBLE) + _UID_BITS + _TRACE_BITS


def _trace_hex(trace_id: str) -> str:
    return trace_id.split("-")[-1].upper()


def _marker_bits(telegram_user_id: int, trace_id: str) -> str:
    uid_bits = format(telegram_user_id, f"0{_UID_BITS}b")
    trace_bits = format(int(_trace_hex(trace_id), 16), f"0{_TRACE_BITS}b")
    return _PREAMBLE + uid_bits + trace_bits


def _insert_vector_marker(page, bits: str):
    """Draw the encoded bit run as sub-pixel dots at a random position."""
    rect = page.rect
    run_width = len(bits) * _X_STEP + 20
    max_x = int(max(20, rect.width - run_width - 10))
    max_y = int(max(20, rect.height - 60))
    x0 = 10 + secrets.randbelow(max(1, max_x))
    y0 = 10 + secrets.randbelow(max(1, max_y))
    for i, bit in enumerate(bits):
        x = x0 + i * _X_STEP
        y = y0 if bit == "0" else y0 + _Y_DELTA
        page.draw_rect(
            fitz.Rect(x, y, x + _RECT_SIZE, y + _RECT_SIZE),
            color=None,
            fill=(0, 0, 0),
            overlay=True,
        )


def _decode_vector_marker(page):
    """Recover {uid, trace_hex} from the micro-dot run on one page."""
    tiny = []
    for drawing in page.get_drawings():
        r = drawing["rect"]
        if r.width <= 0.1 and r.height <= 0.1:
            tiny.append((round(r.x0, 3), round(r.y0, 3)))
    if len(tiny) < _TOTAL_BITS:
        return None
    tiny.sort()

    runs = []
    run = [tiny[0]]
    for point in tiny[1:]:
        if abs(point[0] - run[-1][0] - _X_STEP) < 0.05:
            run.append(point)
        else:
            if len(run) >= _TOTAL_BITS:
                runs.append(run)
            run = [point]
    if len(run) >= _TOTAL_BITS:
        runs.append(run)

    for run in runs:
        levels = sorted({point[1] for point in run})
        if len(levels) != 2:
            continue
        y_low, y_high = levels
        if abs(y_high - y_low - _Y_DELTA) > 0.1:
            continue
        bits = "".join(
            "1" if abs(point[1] - y_high) < 0.05 else "0" for point in run
        )
        if not bits.startswith(_PREAMBLE):
            continue
        payload = bits[len(_PREAMBLE):]
        if len(payload) < _UID_BITS + _TRACE_BITS:
            continue
        uid = int(payload[:_UID_BITS], 2)
        trace_hex = format(int(payload[_UID_BITS:], 2), "08X")
        if uid == 0 or trace_hex == "00000000":
            continue
        return {"uid": uid, "trace_hex": trace_hex}
    return None


def watermark_pdf(
    input_path: str,
    output_path: str,
    telegram_user_id: int,
    visible_code: str,
    trace_id: str,
    original_filename: str,
):
    """Create the personalized copy of a PDF.

    The visible code lands on page 2; everything else is invisible and
    unfindable by normal inspection (text layer, search, copy-paste).
    """
    doc = fitz.open(input_path)
    if doc.needs_pass:
        doc.close()
        raise ValueError("Password-protected PDFs are not supported")
    if doc.page_count < 1:
        doc.close()
        raise ValueError("The PDF has no pages")

    # Visible code on page 2 (index 1); shorter PDFs fall back to the last page.
    target_index = min(1, doc.page_count - 1)
    page = doc[target_index]
    page_height = page.rect.height
    page.insert_text(
        (28, max(20, page_height - 18)),
        visible_code,
        fontsize=6,
        fontname="helv",
        color=(0, 0, 0),
        overlay=True,
    )

    # Invisible micro-dot marker on every page, at a pseudo-random
    # position, so the trace survives even if single pages are removed.
    bits = _marker_bits(telegram_user_id, trace_id)
    for page in doc:
        _insert_vector_marker(page, bits)

    # Metadata looks like generic PDF tooling output. The comma tokens
    # hide the trace hex and the recipient ID in hex.
    metadata = doc.metadata or {}
    metadata.update(
        {
            "title": original_filename,
            "author": "",
            "subject": "Document",
            "keywords": f"doc,{_trace_hex(trace_id)},{format(telegram_user_id, 'x')}",
            "creator": "PDF Processor 1.7",
            "producer": "PDF Tools",
        }
    )
    doc.set_metadata(metadata)
    doc.save(output_path, garbage=4, deflate=True, clean=True)
    doc.close()


def extract_trace_details(path: str) -> dict:
    """Collect everything embedded in a PDF that can identify a delivery.

    Priority: micro-dot vector marker, then disguised metadata tokens,
    then the legacy plain-text marker. Returns a dict with trace_id,
    uid, visible_code, delivered and the sources values came from.
    Missing values are None.
    """
    doc = fitz.open(path)
    sources = set()

    vector = None
    for page in doc:
        vector = _decode_vector_marker(page)
        if vector:
            sources.add("hidden vector layer")
            break

    metadata = doc.metadata or {}
    keywords = (metadata.get("keywords") or "").strip()
    meta_trace = meta_uid = None
    tokens = [token.strip() for token in keywords.split(",")]
    if len(tokens) == 3 and len(tokens[1]) == 8:
        try:
            int(tokens[1], 16)
            int(tokens[2], 16)
            meta_trace = f"LX-{tokens[1].upper()}"
            meta_uid = int(tokens[2], 16)
            sources.add("metadata")
        except ValueError:
            meta_trace = meta_uid = None

    candidates = [str(value) for value in metadata.values() if value]
    for page in doc:
        candidates.append(page.get_text("text"))
    doc.close()
    joined = "\n".join(candidates)

    legacy_trace = TRACE_PATTERN.search(joined)
    if legacy_trace:
        sources.add("text (legacy)")

    def first(*values):
        for value in values:
            if value not in (None, ""):
                return value
        return None

    trace_id = first(
        f"LX-{vector['trace_hex']}" if vector else None,
        meta_trace,
        legacy_trace.group(1) if legacy_trace else None,
    )
    legacy_uid = UID_PATTERN.search(joined)

    return {
        "trace_id": trace_id,
        "uid": first(
            vector["uid"] if vector else None,
            meta_uid,
            int(legacy_uid.group(1)) if legacy_uid else None,
        ),
        "visible_code": first(
            CODE_PATTERN.search(joined).group(1) if CODE_PATTERN.search(joined) else None,
        ),
        "delivered": first(
            DELIVERED_PATTERN.search(joined).group(1)
            if DELIVERED_PATTERN.search(joined) else None,
        ),
        "sources": sources,
    }


def extract_trace_id(path: str):
    """Backward-compatible helper returning only the trace ID."""
    return extract_trace_details(path)["trace_id"]


def is_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False
