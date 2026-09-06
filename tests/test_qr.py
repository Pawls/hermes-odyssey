"""The ported QR encoder.

The encoder is a port of PawlRemote's ``qr.ts`` and every one of its subtle parts — the mask
choice, the format BCH, the zigzag's skip of the timing column — fails *silently*: the symbol still
draws, and only a phone camera in someone else's hand finds out. So the load-bearing test is not a
property test but a golden one, and the goldens below were not invented here.

**How they were verified, once, outside this suite.** Each payload was encoded, rendered to a
bitmap and decoded with OpenCV's ``QRCodeDetector``; all four round-tripped to the exact input
string. The four versions were chosen to cover what changes between them: version 1 (no alignment
patterns, no version block), version 4, version 13 (16-bit character count, version information
block, two block groups) and version 20 (the largest this encoder claims). The data codeword stream
was separately checked against the ``qrcode`` package's ``create_data`` for versions 1 and 13.

Neither OpenCV nor ``qrcode`` is in the Hermes venv, and adding one to an environment ``hermes
update`` owns is not on the table — so the digests stand in for them. A change here means the
symbol changed, and the only honest response is to re-run that decode before touching the goldens.

(``segno`` was tried as a third opinion and disagrees on exactly one thing: it emits an extra
``0x00`` codeword after the terminator before the ``EC``/``11`` pad run. ``qrcode`` and the
worked example in the spec both agree with this encoder. It is padding either way, so all three
symbols decode identically.)
"""

from __future__ import annotations

import hashlib
import importlib

import pytest

from conftest import PLUGIN_DIR  # noqa: F401 — imported for its path side effect via _plugin_on_path


@pytest.fixture()
def hr_qr(_plugin_on_path):
    return importlib.import_module("hr_qr")


def _digest(matrix) -> str:
    flat = "".join("1" if module else "0" for row in matrix for module in row)
    return hashlib.sha256(flat.encode()).hexdigest()


#: ``(payload, expected size, sha256 of the row-major module bits)``.
GOLDEN = [
    ("a", 21, "2e7add7dfd3288d4ed23295399f43abf38be57f9a3df265ce870eb6fa468237e"),
    (
        "hermes-remote://pair?v=1&h=192.168.1.50&p=9443",
        33,
        "0f5ed8d34d65dc393f55d51704b0e99efee6f2c3c4a6a3473c7e2332bf54d71b",
    ),
    ("x" * 300, 69, "1b0ce35009886d141b2800ee67e997155ea1eb28e586cb0e5ca6fe1176fa9bd4"),
    ("y" * 660, 97, "d1c26342018f7751d83a935a182c2d6968168228bc7aff9cfb5621f968b412de"),
]


@pytest.mark.parametrize("payload,size,digest", GOLDEN, ids=["v1", "v4", "v13", "v20"])
def test_the_symbol_is_byte_for_byte_what_a_decoder_read(hr_qr, payload, size, digest):
    matrix = hr_qr.encode(payload)
    assert len(matrix) == size
    assert all(len(row) == size for row in matrix)
    assert _digest(matrix) == digest


# ---- structure --------------------------------------------------------------


def test_the_three_finder_eyes_are_where_a_scanner_looks(hr_qr):
    matrix = hr_qr.encode("hermes")
    n = len(matrix)
    for row, col in ((0, 0), (0, n - 7), (n - 7, 0)):
        assert all(matrix[row][col + i] for i in range(7)), "top edge of the eye"
        assert all(matrix[row + i][col] for i in range(7)), "left edge of the eye"
        assert not matrix[row + 1][col + 1], "the light ring inside it"
        assert matrix[row + 3][col + 3], "the dark centre"


def test_the_timing_patterns_alternate(hr_qr):
    matrix = hr_qr.encode("hermes")
    n = len(matrix)
    for i in range(8, n - 8):
        assert matrix[6][i] is (i % 2 == 0)
        assert matrix[i][6] is (i % 2 == 0)


def test_the_always_dark_module_survives_the_format_write(hr_qr):
    """It sits in the reserved region and is the cell a hand-written encoder loses."""
    matrix = hr_qr.encode("hermes")
    assert matrix[len(matrix) - 8][8] is True


def test_the_timing_row_and_column_are_never_data(hr_qr):
    """Masking the timing pattern produces a symbol no reader locks onto."""
    for size in (21, 45, 97):
        assert not hr_qr.is_data_module(size, 6, 10)
        assert not hr_qr.is_data_module(size, 10, 6)


def test_an_alignment_centre_over_a_finder_stays_maskable_data(hr_qr):
    """Version 7's table lists (6,22) and (22,6), which straddle the timing line by design.

    Skipping them because ``used`` is set there would drop two real alignment patterns; skipping
    the corner centres is the only legitimate reason to leave one out.
    """
    size = 7 * 4 + 17
    assert not hr_qr.is_data_module(size, 6, 22)  # timing row, not the alignment pattern
    assert not hr_qr.is_data_module(size, 22, 22)  # a real alignment centre
    assert hr_qr.is_data_module(size, 22, 30)  # ordinary data next to it


# ---- version selection ------------------------------------------------------


def test_the_version_grows_with_the_payload(hr_qr):
    sizes = [len(hr_qr.encode("z" * n)) for n in (10, 100, 400, 660)]
    assert sizes == sorted(sizes)
    assert len(set(sizes)) == len(sizes)


def test_the_character_count_widens_at_version_ten(hr_qr):
    """Version 9 counts characters in 8 bits and version 10 in 16, so a payload on the boundary
    needs the search loop rather than one calculation against a fixed overhead."""
    assert len(hr_qr.encode("z" * 180)) == 9 * 4 + 17
    assert len(hr_qr.encode("z" * 181)) == 10 * 4 + 17


def test_a_payload_that_does_not_fit_raises(hr_qr):
    """Silently dropping to level L would produce a QR that scans into a truncated URI."""
    with pytest.raises(hr_qr.QrTooLong):
        hr_qr.encode("z" * 700)


def test_utf8_is_measured_in_bytes_not_characters(hr_qr):
    """Byte mode counts encoded bytes; a version chosen from ``len(str)`` would overflow."""
    assert len(hr_qr.encode("é" * 90)) == len(hr_qr.encode("z" * 180))


# ---- terminal rendering -----------------------------------------------------


def test_the_rendering_is_half_height_and_quiet_zoned(hr_qr):
    matrix = hr_qr.encode("hermes")
    text = hr_qr.to_text(matrix, quiet=2)
    lines = text.split("\n")
    assert all(len(line) == len(matrix) + 4 for line in lines)
    assert len(lines) == (len(matrix) + 4 + 1) // 2


def test_a_dark_terminal_draws_the_light_modules(hr_qr):
    """A block glyph is the bright colour on a dark terminal, so the polarity has to flip — and
    the quiet zone flips with it, or the code is framed in the wrong colour."""
    matrix = hr_qr.encode("hermes")
    dark_terminal = hr_qr.to_text(matrix, dark_background=True).split("\n")
    light_terminal = hr_qr.to_text(matrix, dark_background=False).split("\n")
    assert dark_terminal[0] == "█" * len(dark_terminal[0])
    assert light_terminal[0] == " " * len(light_terminal[0])
