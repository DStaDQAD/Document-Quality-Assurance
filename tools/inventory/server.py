"""Local labelling server for the sentence inventory (tools/inventory/export.py writes its data).

usage: python -m tools.inventory.server [--port 8765] [--dir eval/inventory]
then open http://127.0.0.1:8765

Serves app.html and a tiny JSON API. Labels are written to <dir>/labels/<doc>.json on every
change (auto-save), atomically. Bound to 127.0.0.1 only: the data is internal drafts.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
APP = Path(__file__).with_name("app.html")
LABEL_FORMAT = 1
_DOC_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+$")

# Labelling waves (the order the owner labels in).
WAVES = {
    1: {"uang-beredar", "cadangan-devisa", "sulni"},
    2: {"npi", "skdu"},
    3: {"uang-primer-m0", "pii", "sk", "spe", "pmi", "sbank", "shpr", "ppkom"},
}


def wave_of(publication: str) -> int:
    return next((w for w, pubs in WAVES.items() if publication in pubs), 3)


class Store:
    def __init__(self, base: Path):
        self.data = base / "data"
        self.labels = base / "labels"
        self.labels.mkdir(parents=True, exist_ok=True)

    def _doc_path(self, doc: str) -> Path:
        if not _DOC_ID.match(doc):
            raise KeyError(doc)
        path = self.data / f"{doc}.json"
        if not path.exists():
            raise KeyError(doc)
        return path

    def load_labels(self, doc: str) -> dict:
        path = self.labels / f"{doc}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        data = json.loads(self._doc_path(doc).read_text(encoding="utf-8"))
        return {"format_version": LABEL_FORMAT, "case": data["case"], "doc_id": data["doc_id"],
                "publication": data["publication"], "labels": {}}

    def docs(self) -> list:
        out = []
        for path in sorted(self.data.glob("*.json")):
            d = json.loads(path.read_text(encoding="utf-8"))
            labels = self.load_labels(path.stem)["labels"]
            done = sum(1 for l in labels.values() if l.get("status") == "done")
            deferred = sum(1 for l in labels.values() if l.get("status") == "deferred")
            out.append({"id": path.stem, "case": d["case"], "doc_id": d["doc_id"],
                        "publication": d["publication"], "wave": wave_of(d["publication"]),
                        "sentences": len(d["sentences"]), "done": done, "deferred": deferred,
                        "needs_vision": d["needs_vision"]})
        return out

    def doc(self, doc: str) -> dict:
        data = json.loads(self._doc_path(doc).read_text(encoding="utf-8"))
        return {"data": data, "labels": self.load_labels(doc)}

    def save_label(self, doc: str, sid: str, label) -> dict:
        data = json.loads(self._doc_path(doc).read_text(encoding="utf-8"))
        if sid not in {s["sid"] for s in data["sentences"]}:
            raise KeyError(sid)
        current = self.load_labels(doc)
        if label is None:
            current["labels"].pop(sid, None)
        else:
            label["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            current["labels"][sid] = label
        self.labels.mkdir(parents=True, exist_ok=True)
        path = self.labels / f"{doc}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        return current["labels"].get(sid)


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _json(self, code: int, body) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    raw = APP.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                elif url.path == "/api/docs":
                    self._json(200, store.docs())
                elif url.path == "/api/doc":
                    self._json(200, store.doc(parse_qs(url.query).get("id", [""])[0]))
                else:
                    self._json(404, {"error": "not found"})
            except KeyError:
                self._json(404, {"error": "unknown document"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/label":
                return self._json(404, {"error": "not found"})
            # Same-origin only: the page is served from here.
            origin = self.headers.get("Origin")
            if origin and not origin.startswith(f"http://{self.headers.get('Host')}"):
                return self._json(403, {"error": "cross-origin"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                saved = store.save_label(body["doc"], body["sid"], body.get("label"))
                self._json(200, {"ok": True, "label": saved})
            except (KeyError, ValueError, TypeError) as exc:
                self._json(400, {"error": f"bad request: {exc}"})

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dir", default=str(ROOT / "eval" / "inventory"))
    args = ap.parse_args(argv)
    store = Store(Path(args.dir))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    print(f"Inventory labelling: http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
