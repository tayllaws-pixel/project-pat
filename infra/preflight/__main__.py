"""python3 -m infra.preflight [--inventory-loss] [--out DIR] [--json]"""
import argparse, sys
from .preflight import run_preflight, write_report

ap = argparse.ArgumentParser(description="Project PAT read-only preflight")
ap.add_argument("--inventory-loss", action="store_true", help="also list what a reinstall would destroy (default on)")
ap.add_argument("--no-inventory-loss", action="store_true")
ap.add_argument("--out", default="/var/lib/project-pat")
ap.add_argument("--json", action="store_true", help="print JSON instead of markdown")
a = ap.parse_args()
rep = run_preflight(loss_inventory=not a.no_inventory_loss)
try:
    j, m = write_report(rep, a.out)
    where = f"\nwritten: {j}, {m}\n"
except OSError as e:
    where = f"\n(report not written: {e})\n"
sys.stdout.write(rep.to_json() if a.json else rep.to_markdown())
sys.stdout.write(where)
sys.exit(1 if rep.blocking else 0)
