"""A page block must never be sent whole when it is larger than the chunk budget.

Measured on sample_data/M2-April-2026 (1).pdf (a scanned report, internal mode, 2026-09-14):
the vision narrative pass returned 248.339 chars — 6,5x the 37.944 chars its digital twin
actually contains — and one batch came back with a single page marker for three pages, so all
of it landed in ONE page block. _split_into_page_chunks keeps a page block together, so that
block went to the extraction model as a single 240.867-char call: in=175.790 out=168.769 tokens
and 575s for a document whose digital twin costs 62.877/16.741 and 44s.

Whatever makes a page block run away upstream, a chunk larger than the budget must not be sent:
the budget is the whole point of chunking. Page attribution has to survive the split, since
every extracted fact carries the page it came from.
"""

from structured_extractor import _split_into_page_chunks

MAX = 6000


def _page(n: int, body: str) -> str:
    return f"[== Halaman {n} ==]\n{body}\n"


def test_an_oversized_page_block_is_split_to_the_budget():
    """The runaway case: one page block many times the budget."""
    runaway = _page(7, "\n".join(f"kalimat narasi nomor {i} pada halaman tujuh." for i in range(2000)))

    chunks = _split_into_page_chunks(runaway, MAX)

    assert len(chunks) > 1
    assert all(len(c) <= MAX for c in chunks), [len(c) for c in chunks]


def test_every_piece_of_a_split_page_still_says_which_page_it_is():
    """page_number comes from the marker, so a piece without one would file its facts
    under the previous page — a wrong citation is worse than a missing one."""
    runaway = _page(7, "\n".join(f"kalimat narasi nomor {i}." for i in range(2000)))

    chunks = _split_into_page_chunks(runaway, MAX)

    assert all("[== Halaman 7 ==]" in c for c in chunks)


def test_splitting_loses_no_narrative():
    """Every source line has to survive somewhere, or a claim silently stops being checkable."""
    lines = [f"kalimat narasi nomor {i} yang cukup panjang untuk mengisi ruang." for i in range(2000)]
    runaway = _page(7, "\n".join(lines))

    chunks = _split_into_page_chunks(runaway, MAX)
    joined = "\n".join(chunks)

    assert all(line in joined for line in lines)


def test_ordinary_pages_still_group_together():
    """The regression guard: small pages must keep sharing a chunk, which is what keeps the
    system prompt from being re-sent once per page."""
    text = "".join(_page(n, f"Paragraf pendek halaman {n}.") for n in range(1, 6))

    chunks = _split_into_page_chunks(text, MAX)

    assert len(chunks) == 1
    assert all(f"[== Halaman {n} ==]" in chunks[0] for n in range(1, 6))


def test_a_single_unsplittable_line_is_not_dropped():
    """One line longer than the budget cannot be split on a line boundary; it must still be
    emitted (truncating it would delete a claim) even though it exceeds the budget."""
    monster = _page(3, "x" * (MAX * 2))

    chunks = _split_into_page_chunks(monster, MAX)

    assert len(chunks) >= 1
    assert "x" * (MAX * 2) in "".join(chunks)
