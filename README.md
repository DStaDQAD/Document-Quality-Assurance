# Fact-Checker PoC (Text-to-SQL + LLM-as-a-Judge)

Verifies natural-language claims about macroeconomic indicators against a local SQLite
database using a single-shot Text-to-SQL LLM call (schema + claim -> one SQL query, with one
retry on failure), then judges the claim against the retrieved data with a second LLM call.

Three entry points:
- `/api/verify-claim` - verify a single claim.
- `/api/verify-document` - feed in a long document; it extracts every checkable claim about
  inflasi/suku bunga/penyaluran kredit, verifies each one independently, and returns a
  per-claim report plus an aggregate summary.
- `/api/verify-document-pdf` - same as above, but the document is uploaded as a PDF file.
  Only digital PDFs with a real text layer are supported (no OCR for scans).

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

1. Open `.env` and replace `your_google_ai_studio_api_key_here` with your real
   Google AI Studio API key.
2. Build the database:
   ```bash
   python setup_db.py
   ```
3. Start the API:
   ```bash
   uvicorn main:app --reload
   ```

## Try it

```bash
curl -X POST http://127.0.0.1:8000/api/verify-claim \
  -H "Content-Type: application/json" \
  -d "{\"claim\": \"Inflasi pada Q1 2023 lebih tinggi dibandingkan Q4 2023\"}"
```

Response shape:

```json
{
  "status": "Entailed",
  "sql_query_used": "SELECT ...",
  "reasoning": "..."
}
```

For a whole document:

```bash
curl -X POST http://127.0.0.1:8000/api/verify-document \
  -H "Content-Type: application/json" \
  -d "{\"document\": \"Inflasi pada Q1 2023 lebih tinggi dibandingkan Q4 2023. Suku bunga acuan naik sepanjang 2023 dan 2024.\"}"
```

Response shape:

```json
{
  "total_claims": 2,
  "entailed_count": 1,
  "refuted_count": 1,
  "error_count": 0,
  "summary": "2 claim(s) extracted: 1 entailed, 1 refuted, 0 could not be verified.",
  "results": [
    {"claim": "...", "status": "Entailed", "sql_query_used": "SELECT ...", "reasoning": "..."},
    {"claim": "...", "status": "Refuted", "sql_query_used": "SELECT ...", "reasoning": "..."}
  ]
}
```

For a PDF document:

```bash
curl -X POST http://127.0.0.1:8000/api/verify-document-pdf \
  -F "file=@laporan.pdf;type=application/pdf"
```

Response shape is identical to `/api/verify-document`. A PDF with no extractable text layer
(e.g. a scan) returns a `400` explaining that OCR isn't supported.

Interactive docs: http://127.0.0.1:8000/docs
