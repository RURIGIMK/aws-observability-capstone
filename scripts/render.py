"""Render infrastructure/05-observability.yaml: embed the remediation Lambda source inline.

Keeps the Lambda in a real .py file (lintable, testable) while the stack stays a single template
with no S3 packaging step. Inline ZipFile code is limited to 4096 bytes.
"""
from pathlib import Path

root = Path(__file__).resolve().parent.parent
code = (root / "lambda/remediation/handler.py").read_text()
assert len(code.encode()) <= 4096, f"Lambda source is {len(code)} bytes; inline limit is 4096"
indented = "\n".join(("          " + line) if line.strip() else "" for line in code.splitlines())
tmpl = (root / "infrastructure/05-observability.tmpl.yaml").read_text()
out = tmpl.replace("__LAMBDA_CODE__", indented)
(root / "infrastructure/05-observability.yaml").write_text(out)
print(f"rendered 05-observability.yaml ({len(code)} bytes of Lambda code inlined)")
