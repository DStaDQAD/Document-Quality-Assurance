"""The model tidies each PDF page's text layer; a lock keeps its answer only when no letter or digit
changed (spaces and line breaks aside), else the page as it was."""
import asyncio

from langchain_core.messages import AIMessage

import pdf_tidy
from pdf_tidy import lock, tidy_text

# UB Agustus 2026: the text layer splits words and numbers with spaces PDFium makes up.
PAGE = ("Uang Beredar (M2) pada Agustus 2026 tercatat Rp10. 448 ,1 triliun atau tumbuh 8,2% (yoy) ,\n"
        "lebih tinggi dibandingkan pertumbuhan bulan Ju li 2026 sebesar 7,6% (yoy) . Pertumb uhan M2\n"
        "terutama didorong oleh uang kuasi yang tumbuh 6,3% (yoy) dan M1 yang tumbuh 11,0% (yoy) .")
TIDY = ("Uang Beredar (M2) pada Agustus 2026 tercatat Rp10.448,1 triliun atau tumbuh 8,2% (yoy), lebih "
        "tinggi dibandingkan pertumbuhan bulan Juli 2026 sebesar 7,6% (yoy). Pertumbuhan M2 terutama "
        "didorong oleh uang kuasi yang tumbuh 6,3% (yoy) dan M1 yang tumbuh 11,0% (yoy).")


class _Model:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    async def ainvoke(self, messages, **kwargs):
        self.prompts.append(messages[0].content)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return AIMessage(content=reply)


def test_a_tidy_answer_that_only_moves_spaces_is_kept():
    assert lock(PAGE, TIDY) == TIDY


def test_a_paragraph_that_changes_a_digit_is_replaced_by_the_page_text():
    second = "Pertumbuhan M2 terutama didorong oleh uang kuasi yang tumbuh 6,3% (yoy) dan M1 yang tumbuh 11,0% (yoy)."
    first = TIDY[:TIDY.index(second)].strip()
    out = lock(PAGE, first + "\n\n" + second.replace("6,3%", "6,8%"))

    assert out.startswith(first)                          # the kept paragraph, tidy
    assert "6,8%" not in out and "6,3% (yoy) dan" in out  # the refused one: the page's own text
    assert "Pertumb uhan" in out


def test_an_answer_that_drops_or_adds_a_minus_sign_is_not_used():
    page = "Neraca jasa mencatat defisit -1,5 miliar dolar AS pada triwulan III 2026, lebih rendah dari defisit\nsebelumnya."
    out = lock(page, page.replace("-1,5", "1,5").replace("\n", " "))

    assert "-1,5" in out


def test_a_line_end_hyphen_stored_as_a_special_character_reads_as_a_hyphen():
    # UB Agustus 2026 page 5: "Rata\ufffe" + "rata" at a line break; the model writes "Rata-rata".
    page = ("Rata\ufffe\nrata tertimbang suku bunga kredit pada Agustus 2026 sebesar 8,9%, relatif stabil dibanding bulan lalu.")
    answer = "Rata-rata tertimbang suku bunga kredit pada Agustus 2026 sebesar 8,9%, relatif stabil dibanding bulan lalu."

    assert lock(page, answer) == answer


def test_text_the_answer_leaves_out_is_put_back_from_the_page_in_its_place():
    first_sentence_only = TIDY.split(". Pertumbuhan")[0] + "."
    out = lock(PAGE, first_sentence_only)

    assert out.startswith(first_sentence_only)
    assert "terutama didorong oleh uang kuasi yang tumbuh 6,3% (yoy)" in " ".join(out.split())


def test_paragraphs_are_kept_one_per_line():
    answer = "Judul Bagian\n\n" + TIDY
    page = "Judul Bagian\n" + PAGE

    assert lock(page, answer) == "Judul Bagian\n" + TIDY


def test_paragraphs_come_out_in_the_page_order():
    page = "Judul Bagian\n" + PAGE
    out = lock(page, TIDY + "\n\nJudul Bagian")

    assert out == "Judul Bagian\n" + TIDY


def test_a_page_number_the_answer_drops_comes_back_from_the_page():
    out = lock(PAGE + "\nAnalisis Uang Beredar 3", TIDY)   # the footer is not in TIDY

    assert out == TIDY + "\nAnalisis Uang Beredar 3"


def test_every_letter_of_the_page_comes_out_once_whatever_the_answer():
    page = "Judul Bagian\n" + PAGE
    for answer in (TIDY, TIDY[:80], "Lain sama sekali.", TIDY.replace("8,2", "9,9"), TIDY + "\n\n" + TIDY,
                   "Judul Bagian\n\nJudul Bagian\n\n" + TIDY):
        assert pdf_tidy._letters(lock(page, answer)) == pdf_tidy._letters(page), answer


def test_each_page_of_the_text_is_tidied_and_keeps_its_marker():
    text = f"[== Halaman 1 ==]\n{PAGE}\n\n[== Halaman 2 ==]\nTabel 1 2 3"   # page 2: too little to send
    model = _Model([TIDY])

    out = asyncio.run(tidy_text(text, model))

    assert out == f"[== Halaman 1 ==]\n{TIDY}\n\n[== Halaman 2 ==]\nTabel 1 2 3"
    assert len(model.prompts) == 1 and "Ju li" in model.prompts[0]


def test_a_page_whose_answer_fails_or_is_refused_stays_as_it_was():
    text = f"[== Halaman 1 ==]\n{PAGE}\n\n[== Halaman 2 ==]\n{PAGE}"
    model = _Model([RuntimeError("timeout"), TIDY.replace("7,6%", "7,9%")])

    assert asyncio.run(tidy_text(text, model)) == text


def test_a_model_answer_in_parts_is_joined():
    class _Parts(_Model):
        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content=[{"type": "text", "text": TIDY[:40]}, {"type": "text", "text": TIDY[40:]}])

    assert asyncio.run(tidy_text(f"[== Halaman 1 ==]\n{PAGE}", _Parts([]))) == f"[== Halaman 1 ==]\n{TIDY}"


def test_at_most_four_pages_are_with_the_model_at_once():
    running = {"now": 0, "most": 0}

    class _Slow:
        async def ainvoke(self, messages, **kwargs):
            running["now"] += 1
            running["most"] = max(running["most"], running["now"])
            await asyncio.sleep(0.01)
            running["now"] -= 1
            return AIMessage(content=TIDY)

    text = "\n\n".join(f"[== Halaman {i} ==]\n{PAGE}" for i in range(1, 11))

    asyncio.run(tidy_text(text, _Slow()))

    assert running["most"] == pdf_tidy.MAX_AT_ONCE == 4
