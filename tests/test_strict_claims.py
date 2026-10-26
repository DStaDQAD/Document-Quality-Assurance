"""The strict claim: what the extractor fills in, and how a finished claim is saved for replays."""
from strict.claims import Cell, CellRef, Claim, StrictClaim, claim_from_dict, claim_to_dict


def test_a_claim_round_trips_through_dict():
    claim = Claim(
        operation="formula", subject="KFLN investasi lainnya",
        cells=[Cell(code="S1", row_text="kewajiban > investasi lainnya", period_text="Triwulan III 2025",
                    row="Kewajiban > Investasi Lainnya", key=(2025, "Q3")),
               Cell(code="S9", row_text="tidak ada", period_text="kemarin", row=None, key=None)],
        context_quote="Hal tersebut didorong oleh … 178 juta dolar AS.", page_number=7,
        claimed_value=178.0, unit="juta USD", formula="c2-c1", formula_unit="tabel",
        all_members=False, not_in_tables=False, reason=None,
    )

    assert claim_from_dict(claim_to_dict(claim)) == claim


def test_formula_is_an_operation():
    raw = StrictClaim(anchor_quote="12 kota", subject="kota yang melambat", operation="formula",
                      cells=[CellRef(table="S1", row="Medan", period="Q4 2025")],
                      formula="(c2<c1)", formula_unit="jumlah", claimed_value_raw="12")

    assert raw.operation == "formula" and raw.all_members is False and raw.not_in_tables is False


def test_field_order_puts_not_in_tables_before_cells():
    # A structured answer is written top to bottom: the model decides "not in the tables" before
    # it starts naming cells.
    assert list(StrictClaim.model_fields)[:4] == ["anchor_quote", "subject", "not_in_tables", "reason"]
