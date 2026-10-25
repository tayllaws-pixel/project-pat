"""Regenerate docs/progress.json from the checklist tables (evidence-based progress, spec §28)."""
import json, re, pathlib, collections
root = pathlib.Path(__file__).resolve().parents[1]
rows = []
for line in (root / "docs/CHECKLIST.md").read_text().splitlines():
    m = re.match(r"\|\s*(PAT-\d{3})\s*\|(.*)", line)
    if m:
        cells = [c.strip() for c in m.group(2).split("|")]
        rows.append({"id": m.group(1), "task": cells[0], "status": cells[3]})
# compact phase lines like "PAT-040 ... All `planned`."
text = (root / "docs/CHECKLIST.md").read_text()
for m in re.finditer(r"(PAT-\d{3}) ([^·\n]+)", text):
    if m.group(1) not in {r["id"] for r in rows}:
        rows.append({"id": m.group(1), "task": m.group(2).strip(" ."), "status": "planned"})
order = ["planned", "designed", "implemented", "tested", "deployed", "verified"]
counts = collections.Counter(r["status"] for r in rows)
out = {"total": len(rows), "by_status": {s: counts.get(s, 0) for s in order},
       "percent_verified": round(100 * counts.get("verified", 0) / max(1, len(rows)), 1),
       "percent_tested_or_better": round(100 * sum(counts.get(s, 0) for s in order[3:]) / max(1, len(rows)), 1),
       "items": sorted(rows, key=lambda r: r["id"])}
(root / "docs/progress.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps({k: v for k, v in out.items() if k != "items"}, indent=2))
