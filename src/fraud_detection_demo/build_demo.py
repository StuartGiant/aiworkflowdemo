"""Bake data_export.json into index.html to produce a self-contained demo file.

Run from project root:
    source venv/bin/activate
    python src/fraud_detection_demo/build_demo.py

Output: src/fraud_detection_demo/demo/demo_standalone.html
"""

import json
from pathlib import Path

DEMO_DIR = Path("src/fraud_detection_demo/demo")
SRC_HTML = DEMO_DIR / "index.html"
DATA_JSON = DEMO_DIR / "data_export.json"
OUT_HTML  = DEMO_DIR / "demo_standalone.html"


def main() -> None:
    html = SRC_HTML.read_text()
    data = json.loads(DATA_JSON.read_text())

    # Replace the fetch block with an inline assignment
    inline_js = f"DATA = {json.dumps(data, separators=(',', ':'))};\n  init();"

    html = html.replace(
        "fetch('data_export.json')\n"
        "  .then(r => r.json())\n"
        "  .then(d => { DATA = d; init(); })\n"
        "  .catch(() => {\n"
        "    document.body.innerHTML = '<div style=\"padding:2rem;color:#f87171\">data_export.json not found — run scripts 03–05 first.</div>';\n"
        "  });",
        inline_js,
    )

    OUT_HTML.write_text(html)
    size_kb = OUT_HTML.stat().st_size // 1024
    print(f"Built → {OUT_HTML}  ({size_kb} KB)")


if __name__ == "__main__":
    main()
