"""cell_formula: the Tinjauan AI's own calculation over the cells it cites, evaluated by code.

The model writes the arithmetic a claim needs ("(c4-c3)-(c2-c1)"); the program reads every cell
itself and computes it. Only cells, numbers, + - * /, comparisons, and/or/not and a few functions
are accepted — never Python eval."""
import pytest

from cell_formula import FormulaError, evaluate


@pytest.mark.parametrize("formula, cells, expected", [
    # PII: a rise in a component's net liabilities, (Kewajiban − Aset) at the end minus at the start.
    ("(c4-c3)-(c2-c1)", [37763.4, 40571.2, 278576.7, 293454.7], 12070.2),
    # A share of three rows over the total, in percent.
    ("(c1+c2+c3)/c4*100", [52286.2, 22790.0, 34569.6, 558496.5], 19.63),
    ("sum(c1, c2, c3)", [1.0, 2.0, 3.5], 6.5),
    ("abs(c1) - abs(c2)", [-5.0, 2.0], 3.0),
    # A count: each comparison is 1 when it holds.
    ("(c2 < c1) + (c4 < c3) + (c6 < c5)", [3.0, 2.0, 1.0, 1.5, 9.0, 8.0], 2),
])
def test_arithmetic_and_counts(formula, cells, expected):
    assert evaluate(formula, cells) == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize("formula, cells, expected", [
    # SULNI: still contracting, by less than before.
    ("c2 < 0 and abs(c2) < abs(c1)", [-1.35, -0.57], True),
    ("c2 < 0 and abs(c2) < abs(c1)", [-0.57, -1.35], False),
    ("not (c1 > 50)", [49.1], True),
    ("c1 > 50 or c2 > 50", [49.0, 51.0], True),
])
def test_conditions(formula, cells, expected):
    assert evaluate(formula, cells) is expected


@pytest.mark.parametrize("formula", [
    "__import__('os')",
    "c1.real",
    "open('x')",
    "[c1, c2]",
    "c1 if c2 else c2",
    "lambda: 1",
    "c1 ** 1000",          # power is not offered: nothing a report states needs it
    "x1 + 1",              # only c1..cn name cells
    "c9",                  # a cell the answer did not cite
    "",
])
def test_anything_else_is_refused(formula):
    with pytest.raises(FormulaError):
        evaluate(formula, [1.0, 2.0])


def test_division_by_zero_is_a_formula_error():
    with pytest.raises(FormulaError):
        evaluate("c1 / c2", [1.0, 0.0])


def test_a_very_long_formula_is_refused():
    with pytest.raises(FormulaError):
        evaluate("+".join(["c1"] * 500), [1.0])
