"""The arithmetic a claim needs, written by the Tinjauan AI over the cells it cites, evaluated here.

A claim is often more than one cell: a component's net liabilities rise ("meningkatnya kewajiban
neto investasi portofolio sebesar 12,1" = (Kewajiban − Aset) at the end minus at the start), a
share of several rows ("instrumen utang dengan pangsa 78,2%"), a count ("12 dari 18 kota"), or a
condition ("kontraksi … yang lebih rendah"). The model names the cells c1..cn and writes the
formula; code reads every cell from the table itself (ai_review.read_cells) and computes it here.

Parsed with Python's ast and walked by hand, never eval'd: only numbers, c1..cn, + - * /, unary
minus, comparisons (1 when they hold, inside arithmetic), and / or / not, and abs / sum / min /
max / round. Anything else is a FormulaError, and the answer is then not used.
"""
import ast
import operator
import re
from typing import List, Union

MAX_FORMULA_CHARS = 400
_CELL = re.compile(r"c([1-9]\d*)")

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_COMPARE = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
            ast.Eq: operator.eq, ast.NotEq: operator.ne}
_FUNCTIONS = {"abs": abs, "sum": lambda *xs: sum(xs), "min": min, "max": max, "round": round}


class FormulaError(ValueError):
    """The formula uses something this grammar does not offer, or cannot be computed."""


def evaluate(formula: str, cells: List[float]) -> Union[float, bool]:
    """The formula's value over the cells (c1 = cells[0]); a condition gives a bool."""
    if not formula or not formula.strip() or len(formula) > MAX_FORMULA_CHARS:
        raise FormulaError("rumus kosong atau terlalu panjang")
    try:
        tree = ast.parse(formula.strip(), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"rumus tidak bisa dibaca: {formula!r}") from exc
    return _walk(tree.body, cells)


def _walk(node: ast.AST, cells: List[float]):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name):
        match = _CELL.fullmatch(node.id)
        if not match or int(match.group(1)) > len(cells):
            raise FormulaError(f"'{node.id}' bukan sel yang dikutip")
        return float(cells[int(match.group(1)) - 1])
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _number(node.left, cells), _number(node.right, cells)
        if isinstance(node.op, ast.Div) and right == 0:
            raise FormulaError("pembagian dengan nol")
        return _BINARY[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _number(node.operand, cells)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _truth(node.operand, cells)
    if isinstance(node, ast.BoolOp):
        values = [_truth(v, cells) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare) and all(type(op) in _COMPARE for op in node.ops):
        left = _number(node.left, cells)
        for op, right_node in zip(node.ops, node.comparators):
            right = _number(right_node, cells)
            if not _COMPARE[type(op)](left, right):
                return False
            left = right
        return True
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS
            and not node.keywords and node.args):
        return float(_FUNCTIONS[node.func.id](*(_number(a, cells) for a in node.args)))
    raise FormulaError(f"bagian rumus tidak diizinkan: {ast.dump(node)[:80]}")


def _number(node: ast.AST, cells: List[float]) -> float:
    """A value inside arithmetic: a condition counts 1 when it holds ("(c2 < c1) + (c4 < c3)")."""
    value = _walk(node, cells)
    return float(value) if isinstance(value, bool) else value


def _truth(node: ast.AST, cells: List[float]) -> bool:
    value = _walk(node, cells)
    if not isinstance(value, bool):
        raise FormulaError("and/or/not hanya untuk perbandingan")
    return value
