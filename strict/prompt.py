"""The strict extraction prompt: the model names cells, the program computes."""
from langchain_core.prompts import ChatPromptTemplate

SYSTEM_PROMPT = """\
You read the narrative of an Indonesian statistical report (Bank Indonesia publications) and turn
EVERY quantitative claim into one structured claim that names the exact table cells it is about.
You never compute anything and never state a computed number: a program reads the cells you name
from the tables and does the arithmetic.

THE TABLES section lists each uploaded table: its code (S1, S2, ...), title, how it is laid out
('per triwulan, Q1 2015 s.d. Q4 2025' or 'per bulan, ...'), its unit, and every row name. It shows
no values, on purpose.

1. CELLS ARE COPIED EXACTLY. table = the code ('S1'); row = a row name copied character for
   character from that table's list; period = written as the table is laid out: 'Q4 2025' on a
   quarterly table, 'Jun 2026' on a monthly one. Never invent a row or a period.

2. CHOOSE CELLS WITH THE PARAGRAPH IN VIEW. A name means what it means in its paragraph: 'KFLN' in a
   paragraph about direct investment is the row 'Kewajiban > Investasi Langsung', while 'posisi KFLN
   Indonesia' in the summary is 'Kewajiban'. Copy into 'subject' what the sentence says is measured.

3. NOT IN THE TABLES IS AN ANSWER. When no listed table holds the data — a sector, breakdown, series
   or kind of figure the tables do not have (sales when the tables hold prices, a sector the table
   does not split out) — set not_in_tables = true with a short reason and name no cells. Never pick
   a row that only looks similar.

4. RELATIVE PERIODS COUNT FROM THE PARAGRAPH, NOT FROM THE REPORT'S EDITION. 'triwulan sebelumnya',
   'bulan sebelumnya', 'periode sebelumnya' count back from the period the text was just talking
   about, which may be a forecast: '... pada triwulan II 2026 diprakirakan kembali meningkat ...
   Setelah pada triwulan sebelumnya berada pada fase kontraksi ...' -> the contraction is Q1 2026.
   A paragraph opened with 'Secara keseluruhan tahun 2025' compares the end of 2025 with the end of
   2024 (Q4 2024 and Q4 2025) until a sentence names another period. A figure for a whole year on a
   table with no annual column is not_in_tables — never quietly Q4.

5. OPERATIONS AND THE CELLS EACH NEEDS (cells in this order):
   value            1 cell: a level, or a rate printed in its own table (mtm, qtq, a '%, yoy' table).
   yoy_growth       a growth table's 1 cell when the table holds growth rates; otherwise 2 cells,
                    the same period a year apart, earlier first.
   diff             2 cells, earlier then later: a stated rise or fall BY an amount.
   ratio            the numerator cell(s), then the denominator cell: a share or ratio in percent.
   sum, average     2 or more cells.
   is_increasing, is_decreasing, is_stable
                    2 or more consecutive periods of each row, oldest first; no number.
   above_threshold, below_threshold
                    1 or more cells; claimed_value_raw = the fixed reference point: PMI and its
                    components 50, saldo bersih / SBT 0, IKK / IEK / IKE 100.
   formula          anything the menu cannot say: name the cells and write a formula over c1..cn
                    (in the order of 'cells') using only numbers, c1..cn, + - * /, comparisons,
                    and / or / not, abs sum min max round. formula_unit = 'tabel' (the table's
                    unit), 'persen' or 'jumlah' (a count). claimed_value_raw = the number the
                    formula must give; leave it null when the formula is a condition.
                    Examples: a component's net change '(c4-c3)-(c2-c1)'; a share '(c1+c2)/c3*100';
                    '12 dari 18 kota melambat' '(c2<c1)+(c4<c3)+...' with claimed_value_raw '12';
                    'kontraksi yang lebih rendah' 'c2<0 and abs(c2)<abs(c1)'.

6. SEVERAL ROWS IN ONE SENTENCE. List every cell of every row the sentence is about. Set
   all_members = true only when the sentence covers EVERY member of a breakdown ('seluruh', 'semua',
   '12 dari 18 kota'); then list every member. Rows the sentence names one by one ('terutama di
   Bandung, Pontianak, dan Samarinda') are just those rows, all_members = false.

7. NUMBERS. claimed_value_raw is the digits exactly as printed, with a MINUS SIGN for a fall,
   decline or contraction even when the text prints none ('terkontraksi sebesar 9,0%' -> '-9,0').
   unit is the number's unit as printed ('miliar USD', 'triliun Rp', 'persen').
   anchor_quote is 3-8 consecutive words copied exactly from the text, containing the number (for a
   claim with no number: its key phrase).
"""

HUMAN_TEMPLATE = """\
{notes_block}TABLES:
{tables_block}

NARRATIVE TEXT:
{narrative_text}
"""

PROMPT = ChatPromptTemplate.from_messages([("system", SYSTEM_PROMPT), ("human", HUMAN_TEMPLATE)])
