"""Try a Copilot Studio agent over Direct Line before wiring it into the app (no app code touched).

Needs in .env (the owner writes it; never printed):
  COPILOT_DIRECTLINE_SECRET   the agent's Web channel security secret
  COPILOT_DIRECTLINE_URL      optional, Direct Line base URL (default https://directline.botframework.com)

usage: python -m tools.copilot_probe [halo|json|long]
  halo  one greeting, shows the agent answers at all
  json  one report sentence, asks for the figures as JSON: does the answer parse?
  long  a ~20.000-character message: does the agent accept app-sized input?
"""
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
SECRET = (os.getenv("COPILOT_DIRECTLINE_SECRET") or "").strip()
BASE = (os.getenv("COPILOT_DIRECTLINE_URL") or "https://directline.botframework.com").rstrip("/") + "/v3/directline"
USER = {"id": "fact-checker-probe", "name": "probe"}
SENTENCE = ("Pada September 2026, M2 tumbuh sebesar 8,3% (yoy) sehingga mencapai Rp10.587,9 triliun, "
            "didorong oleh M1 sebesar 9,0% (yoy) dan uang kuasi sebesar 7,3% (yoy).")
PROMPTS = {
    "halo": "Halo, balas dengan satu kalimat saja.",
    "json": ("Daftar semua angka dalam kalimat berikut beserta apa yang diukurnya. Jawab HANYA dengan JSON "
             'berbentuk {"items": [{"label": "...", "value": 0.0}]}, tanpa teks lain.\n\n' + SENTENCE),
    "long": ("Berapa banyak kalimat yang diawali kata 'Pada' di teks berikut? Jawab dengan satu angka saja.\n\n"
             + " ".join([SENTENCE] * 110)),
}


def ask(client: httpx.Client, text: str, timeout_s: float = 180.0):
    conv = client.post(f"{BASE}/conversations").raise_for_status().json()
    cid, token = conv["conversationId"], conv.get("token")
    if token:
        client.headers["Authorization"] = f"Bearer {token}"
    sent = client.post(f"{BASE}/conversations/{cid}/activities",
                       json={"type": "message", "from": USER, "text": text, "locale": "id-ID"}).raise_for_status().json()
    t0, watermark, replies, last_new = time.time(), None, [], None
    while time.time() - t0 < timeout_s:
        time.sleep(1.5)
        params = {"watermark": watermark} if watermark else {}
        got = client.get(f"{BASE}/conversations/{cid}/activities", params=params).raise_for_status().json()
        watermark = got.get("watermark", watermark)
        for a in got.get("activities", []):
            if a.get("from", {}).get("id") != USER["id"] and a.get("type") == "message" and a.get("replyToId") == sent["id"]:
                replies.append(a.get("text") or "")
                last_new = time.time()
        if replies and time.time() - last_new > 5:  # the agent may send several messages; stop after 5 s of quiet
            break
    return replies, time.time() - t0


def main(argv) -> int:
    if not SECRET:
        print("COPILOT_DIRECTLINE_SECRET belum ada di .env")
        return 1
    mode = argv[0] if argv else "halo"
    with httpx.Client(timeout=60.0, headers={"Authorization": f"Bearer {SECRET}"}) as client:
        try:
            replies, secs = ask(client, PROMPTS[mode])
        except httpx.HTTPStatusError as e:
            print(f"GAGAL: HTTP {e.response.status_code} {e.response.text[:300]}")
            return 1
    print(f"mode={mode}  kirim={len(PROMPTS[mode])} karakter  waktu={secs:.1f} s  balasan={len(replies)}")
    for r in replies:
        print("---\n" + r[:2000])
    if mode == "json" and replies:
        raw = replies[-1].strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            print("JSON OK:", len(json.loads(raw).get("items", [])), "angka")
        except ValueError as e:
            print("JSON GAGAL dibaca:", e)
    return 0 if replies else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
