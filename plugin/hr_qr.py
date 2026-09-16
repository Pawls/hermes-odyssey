"""A byte-mode QR encoder, in enough of ISO/IEC 18004 to draw a pairing code in a terminal.

A line-for-line port of PawlRemote's ``vscode-pawl/src/remote/qr.ts``, for the same reason it
existed there: this is the only thing in the feature that would introduce a dependency, and the
Hermes venv is not ours to add packages to — ``hermes update`` owns it. What it encodes is a
``hermes-talaria://pair?...`` URI of roughly 150-250 characters, which is why the tables stop where
they do: **error-correction level M, versions 1-20**. Version 20 holds 669 data codewords, several
times the longest payload :mod:`hr_pairing` can build, so the bound is headroom rather than a limit
anyone will meet. A payload that does not fit raises instead of silently dropping to a lower
correction level — a QR that scans into a truncated URI is worse than one that was never drawn.

Level M (~15% recovery) rather than L is deliberate: this code is read off a terminal, at an angle,
by a phone camera, and the cost of the next version up is a few characters of width.

Everything here is pure — a string in, a boolean matrix out — so it is testable against a reference
encoder without a screen, which is what ``tests/test_qr.py`` does.
"""

from __future__ import annotations

from typing import Callable, List, Sequence, Tuple

# ---- GF(256) ---------------------------------------------------------------
#
# The Reed-Solomon field, built once at import. Primitive polynomial 0x11D, which is the one QR
# specifies; the exp table is doubled out to 512 entries so a product of two logs never needs a
# modulo.

_EXP = [0] * 512
_LOG = [0] * 256

_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]
del _x, _i


def _gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> List[int]:
    """The generator polynomial for ``degree`` error-correction codewords, high term first."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coefficient in enumerate(poly):
            nxt[j] ^= coefficient
            nxt[j + 1] ^= _gf_mul(coefficient, _EXP[i])
        poly = nxt
    return poly


def _ec_codewords(data: Sequence[int], count: int) -> List[int]:
    """The EC codewords for one block: polynomial long division, remainder kept."""
    gen = _generator(count)
    rem = list(data) + [0] * count
    for i in range(len(data)):
        factor = rem[i]
        if factor == 0:
            continue
        for j, g in enumerate(gen):
            rem[i + j] ^= _gf_mul(g, factor)
    return rem[len(data) :]


# ---- version tables (level M only) -----------------------------------------

#: ``(ec_per_block, group1_blocks, group1_data, group2_blocks, group2_data)`` indexed by version-1.
_BLOCKS_M: Tuple[Tuple[int, int, int, int, int], ...] = (
    (10, 1, 16, 0, 0),
    (16, 1, 28, 0, 0),
    (26, 1, 44, 0, 0),
    (18, 2, 32, 0, 0),
    (24, 2, 43, 0, 0),
    (16, 4, 27, 0, 0),
    (18, 4, 31, 0, 0),
    (22, 2, 38, 2, 39),
    (22, 3, 36, 2, 37),
    (26, 4, 43, 1, 44),
    (30, 1, 50, 4, 51),
    (22, 6, 36, 2, 37),
    (22, 8, 37, 1, 38),
    (24, 4, 40, 5, 41),
    (24, 5, 41, 5, 42),
    (28, 7, 45, 3, 46),
    (28, 10, 46, 1, 47),
    (26, 9, 43, 4, 44),
    (26, 3, 44, 11, 45),
    (26, 3, 41, 13, 42),
)

#: Alignment-pattern centre coordinates per version; version 1 has none.
_ALIGN: Tuple[Tuple[int, ...], ...] = (
    (),
    (6, 18),
    (6, 22),
    (6, 26),
    (6, 30),
    (6, 34),
    (6, 22, 38),
    (6, 24, 42),
    (6, 26, 46),
    (6, 28, 50),
    (6, 30, 54),
    (6, 32, 58),
    (6, 34, 62),
    (6, 26, 46, 66),
    (6, 26, 48, 70),
    (6, 26, 50, 74),
    (6, 30, 54, 78),
    (6, 30, 56, 82),
    (6, 30, 58, 86),
    (6, 34, 62, 90),
)

MAX_VERSION = len(_BLOCKS_M)


class QrTooLong(ValueError):
    """The payload does not fit in version 20 at level M."""


def _data_capacity(version: int) -> int:
    _, g1n, g1d, g2n, g2d = _BLOCKS_M[version - 1]
    return g1n * g1d + g2n * g2d


def _choose_version(byte_len: int) -> int:
    """The smallest version that holds ``byte_len`` bytes in byte mode.

    The header is 4 mode bits plus a character count that is 8 bits below version 10 and 16 bits at
    and above it — so the search cannot be done once against a fixed overhead, and a payload
    sitting exactly on the version-9 boundary really does need the loop.
    """
    for version in range(1, MAX_VERSION + 1):
        header = 4 + (8 if version < 10 else 16)
        if _data_capacity(version) * 8 >= header + byte_len * 8:
            return version
    raise QrTooLong(
        f"{byte_len} bytes does not fit in version {MAX_VERSION} at correction level M"
    )


# ---- bit stream ------------------------------------------------------------


class _Bits:
    def __init__(self) -> None:
        self._out: List[int] = []
        self._cur = 0
        self._used = 0

    def push(self, value: int, width: int) -> None:
        for i in range(width - 1, -1, -1):
            self._cur = (self._cur << 1) | ((value >> i) & 1)
            self._used += 1
            if self._used == 8:
                self._out.append(self._cur)
                self._cur = 0
                self._used = 0

    @property
    def bit_length(self) -> int:
        return len(self._out) * 8 + self._used

    def finish(self, capacity: int) -> List[int]:
        """Pad to ``capacity`` codewords: terminator, byte alignment, then the two pad bytes."""
        spare = capacity * 8 - self.bit_length
        self.push(0, min(4, spare))
        if self._used != 0:
            self.push(0, 8 - self._used)
        out = list(self._out)
        # 0xEC / 0x11 alternating, which is what the spec names; any other filler still decodes but
        # stops the symbol matching a reference encoder byte for byte, and the tests compare
        # against one.
        i = 0
        while len(out) < capacity:
            out.append(0xEC if i % 2 == 0 else 0x11)
            i += 1
        return out


def _codewords(data: Sequence[int], version: int) -> List[int]:
    """Data and EC codewords, interleaved the way the spec requires.

    The interleave is not cosmetic: it is what makes a contiguous smudge land as one or two lost
    codewords in every block rather than as a whole block gone, which is the difference between a
    scan that corrects and one that fails.
    """
    ec_len, g1n, g1d, g2n, g2d = _BLOCKS_M[version - 1]
    blocks: List[List[int]] = []
    ecs: List[List[int]] = []
    at = 0
    for i in range(g1n + g2n):
        length = g1d if i < g1n else g2d
        block = list(data[at : at + length])
        at += length
        blocks.append(block)
        ecs.append(_ec_codewords(block, ec_len))
    out: List[int] = []
    for i in range(max(g1d, g2d)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_len):
        for ec in ecs:
            out.append(ec[i])
    return out


# ---- BCH -------------------------------------------------------------------


def _bch(value: int, generator: int, count: int) -> int:
    """The remainder of ``value`` over ``generator`` in GF(2), for ``count`` reduction steps.

    The generator's own degree is derived rather than passed: the format field's generator is
    degree 10 and the version field's is degree 12, and hard-coding either as "the bit width"
    produces a checksum that is wrong in a way no test of the payload can see — the data modules
    decode perfectly and the reader still rejects the symbol.
    """
    degree = generator.bit_length() - 1
    rem = value
    for i in range(count - 1, -1, -1):
        if rem & (1 << (degree + i)):
            rem ^= generator << i
    return rem


def _format_bits(mask: int) -> int:
    """The 15-bit format field: level M (0b00) and the mask, BCH-protected and XOR-masked."""
    data = (0b00 << 3) | mask
    return ((data << 10) | _bch(data << 10, 0b10100110111, 5)) ^ 0b101010000010010


def _version_bits(version: int) -> int:
    """The 18-bit version field, present only from version 7."""
    return (version << 12) | _bch(version << 12, 0b1111100100101, 6)


# ---- matrix ----------------------------------------------------------------


class _Grid:
    """A module grid under construction: ``dark`` plus the reservation map data must skip."""

    __slots__ = ("size", "dark", "used")

    def __init__(self, size: int) -> None:
        self.size = size
        self.dark = [[False] * size for _ in range(size)]
        self.used = [[False] * size for _ in range(size)]

    def set(self, r: int, c: int, dark: bool) -> None:
        self.dark[r][c] = dark
        self.used[r][c] = True


def _finder(g: _Grid, row: int, col: int) -> None:
    # The 7x7 eye plus its one-module separator, drawn as a 9x9 window clipped to the grid.
    # Drawing the separator here rather than in its own pass is what guarantees it is RESERVED —
    # an unreserved light separator gets overwritten by data placement and the symbol stops being
    # findable at all.
    for r in range(-1, 8):
        for c in range(-1, 8):
            rr, cc = row + r, col + c
            if rr < 0 or rr >= g.size or cc < 0 or cc >= g.size:
                continue
            ring = max(abs(r - 3), abs(c - 3))
            g.set(rr, cc, 0 <= r <= 6 and 0 <= c <= 6 and ring != 2)


def _over_finder(r: int, c: int, size: int) -> bool:
    """Whether an alignment centre at ``(r, c)`` lands on one of the three finder corners.

    This is the ONLY reason a centre from the table is skipped. Testing ``used`` instead looks
    equivalent and is not: the timing pattern has already claimed row and column 6, so from version
    7 up it would also skip the legitimate centres at (6,22) and (22,6), which straddle the timing
    line by design. The symbol still encodes and no scanner locks onto it.
    """
    return (r <= 8 and c <= 8) or (r <= 8 and c >= size - 9) or (r >= size - 9 and c <= 8)


def _alignment(g: _Grid, version: int) -> None:
    centres = _ALIGN[version - 1]
    for r in centres:
        for c in centres:
            if _over_finder(r, c, g.size):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    g.set(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)


def _reserve_format(g: _Grid, version: int) -> None:
    n = g.size
    for i in range(9):
        if i != 6:
            g.set(8, i, False)
            g.set(i, 8, False)
    for i in range(8):
        g.set(8, n - 1 - i, False)
        g.set(n - 1 - i, 8, False)
    # The always-dark module below the top-left finder. It is not part of any pattern and is the
    # single most commonly missed cell in a hand-written encoder.
    g.set(n - 8, 8, True)
    if version >= 7:
        for i in range(18):
            r, c = divmod(i, 3)
            g.set(r, n - 11 + c, False)
            g.set(n - 11 + c, r, False)


def _skeleton(version: int) -> _Grid:
    size = version * 4 + 17
    g = _Grid(size)
    _finder(g, 0, 0)
    _finder(g, 0, size - 7)
    _finder(g, size - 7, 0)
    # Timing runs the full width; the finders and separators already claimed their ends, so writing
    # it over the whole row and column is harmless and simpler than clipping.
    for i in range(8, size - 8):
        g.set(6, i, i % 2 == 0)
        g.set(i, 6, i % 2 == 0)
    _alignment(g, version)
    _reserve_format(g, version)
    return g


def _place_data(g: _Grid, words: Sequence[int]) -> None:
    """Zigzag placement: two-module columns, right to left, skipping the timing column."""
    n = g.size
    bit = 0
    upward = True
    right = n - 1
    total_bits = len(words) * 8
    while right > 0:
        # Column 6 is the vertical timing pattern. The whole two-wide column shifts left past it,
        # and the shift must move the CURSOR, not just this iteration's reading of it — stepping
        # past 6 to 5 and then decrementing from 6 again revisits columns 4 and 3, overwriting a
        # third of the payload with later bits.
        if right == 6:
            right -= 1
        for step in range(n):
            row = n - 1 - step if upward else step
            for col in (right, right - 1):
                if g.used[row][col]:
                    continue
                # Running off the end of the stream is not an error: the last few modules of some
                # versions are genuinely unused remainder bits, and the spec leaves them light.
                dark = bit < total_bits and ((words[bit >> 3] >> (7 - (bit & 7))) & 1) == 1
                g.dark[row][col] = dark
                g.used[row][col] = True
                bit += 1
        right -= 2
        upward = not upward


_MASKS: Tuple[Callable[[int, int], bool], ...] = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)

_RULE3_A = (True, False, True, True, True, False, True, False, False, False, False)
_RULE3_B = (False, False, False, False, True, False, True, True, True, False, True)


def _penalty(dark: List[List[bool]], size: int) -> int:
    """The four penalty rules, summed. Lower is better.

    This exists to stop a mask producing large blank areas or something that looks like a finder
    pattern in the data region — both of which make a symbol that encodes correctly and scans
    badly, which is the failure mode that would show up only on someone else's phone in a dim room.
    """
    score = 0
    # Rule 1: runs of five or more same-coloured modules, both directions.
    for i in range(size):
        for read in (lambda j, i=i: dark[i][j], lambda j, i=i: dark[j][i]):
            run = 1
            for j in range(1, size):
                if read(j) == read(j - 1):
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2
    # Rule 2: every 2x2 block of one colour.
    for r in range(size - 1):
        row, below = dark[r], dark[r + 1]
        for c in range(size - 1):
            v = row[c]
            if v == row[c + 1] and v == below[c] and v == below[c + 1]:
                score += 3
    # Rule 3: the 1:1:3:1:1 finder-like sequence with four light modules on either side.
    for i in range(size):
        for j in range(size - 10):
            for read in (lambda k, i=i, j=j: dark[i][j + k], lambda k, i=i, j=j: dark[j + k][i]):
                window = tuple(read(k) for k in range(11))
                if window == _RULE3_A or window == _RULE3_B:
                    score += 40
    # Rule 4: deviation from an even split of dark and light.
    dark_count = sum(1 for row in dark for v in row if v)
    percent = (dark_count * 100) / (size * size)
    score += int(abs(percent - 50) / 5) * 10
    return score


def _write_format(dark: List[List[bool]], size: int, mask: int) -> None:
    bits = _format_bits(mask)

    def on(i: int) -> bool:
        return ((bits >> i) & 1) == 1

    # The field is written twice — once down column 8 past the top-left finder, once along row 8
    # split across the other two corners — so a symbol with one corner damaged still says which
    # mask to undo. Both runs skip the timing line, which is why neither is a straight loop.
    for i in range(15):
        if i < 6:
            dark[i][8] = on(i)
        elif i < 8:
            dark[i + 1][8] = on(i)
        else:
            dark[size - 15 + i][8] = on(i)
    for i in range(15):
        if i < 8:
            dark[8][size - 1 - i] = on(i)
        elif i < 9:
            dark[8][15 - i] = on(i)
        else:
            dark[8][14 - i] = on(i)
    # The always-dark module. It sits inside the reserved region, so the mask pass leaves it alone,
    # but the format write above runs over the same rows and must not lose it.
    dark[size - 8][8] = True


def _write_version(dark: List[List[bool]], size: int, version: int) -> None:
    if version < 7:
        return
    bits = _version_bits(version)
    for i in range(18):
        on = ((bits >> i) & 1) == 1
        r, c = divmod(i, 3)
        dark[r][size - 11 + c] = on
        dark[size - 11 + c][r] = on


def is_data_module(size: int, r: int, c: int) -> bool:
    """Whether ``(r, c)`` carries data rather than a function pattern.

    Recomputed from the geometry instead of tracked in a third map: :func:`_place_data` marks every
    module it fills as used, so after it runs ``used`` is true almost everywhere and can no longer
    distinguish the two. Getting this wrong masks the timing pattern, which produces a symbol no
    reader will lock onto.
    """
    n = size
    if r == 6 or c == 6:
        return False

    def in_finder(fr: int, fc: int) -> bool:
        return fr - 1 <= r <= fr + 7 and fc - 1 <= c <= fc + 7

    if in_finder(0, 0) or in_finder(0, n - 7) or in_finder(n - 7, 0):
        return False
    if c == 8 and (r < 9 or r >= n - 8):
        return False
    if r == 8 and (c < 9 or c >= n - 8):
        return False
    version = (n - 17) // 4
    if version >= 7:
        if r < 6 and n - 11 <= c < n - 8:
            return False
        if c < 6 and n - 11 <= r < n - 8:
            return False
    centres = _ALIGN[version - 1]
    for cr in centres:
        for cc in centres:
            # A centre that would sit over a finder is never drawn, so those modules are ordinary
            # data and must stay maskable. Same predicate as ``_alignment`` uses to skip them, so
            # the two cannot disagree about which patterns exist.
            if abs(r - cr) <= 2 and abs(c - cc) <= 2 and not _over_finder(cr, cc, n):
                return False
    return True


def encode(text: str) -> List[List[bool]]:
    """Encode ``text`` as a QR symbol at correction level M; row-major ``dark`` matrix out.

    UTF-8 in byte mode, which is what every phone scanner reads by default. There is no ECI header:
    the payload :mod:`hr_pairing` builds is percent-encoded ASCII, so the question of how a decoder
    guesses the charset never arises.
    """
    data = text.encode("utf-8")
    version = _choose_version(len(data))
    bits = _Bits()
    bits.push(0b0100, 4)
    bits.push(len(data), 8 if version < 10 else 16)
    for byte in data:
        bits.push(byte, 8)
    words = _codewords(bits.finish(_data_capacity(version)), version)

    base = _skeleton(version)
    _place_data(base, words)

    size = base.size
    data_modules = [
        [is_data_module(size, r, c) for c in range(size)] for r in range(size)
    ]

    best: List[List[bool]] = []
    best_score = None
    for mask in range(8):
        rule = _MASKS[mask]
        dark = [row[:] for row in base.dark]
        for r in range(size):
            flags = data_modules[r]
            row = dark[r]
            for c in range(size):
                # Function patterns are never masked. ``used`` cannot answer this: data placement
                # set it for every module it filled, so after that pass it is true nearly
                # everywhere — the geometry has to be re-derived.
                if flags[c] and rule(r, c):
                    row[c] = not row[c]
        _write_format(dark, size, mask)
        _write_version(dark, size, version)
        score = _penalty(dark, size)
        if best_score is None or score < best_score:
            best_score, best = score, dark
    return best


# ---- rendering -------------------------------------------------------------

#: Two vertical modules per character cell using the half-block, so a version-9 symbol
#: (53 modules plus quiet zones) fits in 61 rows of terminal instead of 122.
_HALF_BLOCKS = {
    (False, False): " ",
    (True, False): "▀",  # upper half
    (False, True): "▄",  # lower half
    (True, True): "█",  # full
}


def to_text(dark: List[List[bool]], *, quiet: int = 2, dark_background: bool = True) -> str:
    """The symbol as terminal text, two module rows per line.

    ``quiet`` is 2 rather than the spec's 4 because a terminal's own margin does the rest of the
    job and eight blank lines above the code pushes it off a short window. It is not zero: scanners
    genuinely fail on a code drawn edge to edge.

    ``dark_background`` says what the terminal looks like, and it decides which modules become
    glyphs. A scanner needs the code's *dark* modules to be genuinely darker than its light ones.
    On a dark terminal a block glyph is the bright colour, so the LIGHT modules are drawn as blocks
    and the dark ones as spaces — including the quiet zone, which is light and so must be blocks
    too. Getting that inversion wrong produces a photographic negative that most phone cameras
    refuse. This module cannot know the theme, so the caller says.
    """
    size = len(dark)
    width = size + quiet * 2
    blank = [False] * width
    padded: List[List[bool]] = [blank[:] for _ in range(quiet)]
    for row in dark:
        padded.append([False] * quiet + list(row) + [False] * quiet)
    padded.extend(blank[:] for _ in range(quiet))
    if len(padded) % 2:
        padded.append(blank[:])
    if dark_background:
        padded = [[not v for v in row] for row in padded]

    lines = []
    for i in range(0, len(padded), 2):
        top, bottom = padded[i], padded[i + 1]
        lines.append("".join(_HALF_BLOCKS[(top[c], bottom[c])] for c in range(width)))
    return "\n".join(lines)
