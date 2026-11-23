"""Charts of a document as pictures: Word pictures after a "Grafik N" caption (EMF rendered, its
texts kept), PDF regions under their captions, and scanned PDF pages."""
from word_extraction import emf_text_boxes
from word_fixtures import emf_bytes


def test_a_chart_emf_gives_each_text_the_centre_of_its_bounds():
    data = emf_bytes([(0, 0, "12,97"), (0, 0, "Tw.II")], bounds=[(2814, 436, 2984, 526), (2050, 1060, 2150, 1118)])

    assert [(b.x, b.y, b.text) for b in emf_text_boxes(data)] == [(2899, 481, "12,97"), (2100, 1089, "Tw.II")]


def test_without_bounds_the_reference_point_is_kept():
    assert [(b.x, b.y) for b in emf_text_boxes(emf_bytes([(120, 5, "2026")]))] == [(120, 5)]
