"""patctl command line: plan | dry-run | install | verify | status | resume | repair | rollback | uninstall."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .engine import Engine, State
from .host import Host
from .steps import build_steps, load_guests

REPO_ROOT = str(Path(__file__).resolve().parents[3])
STATE_PATH = os.environ.get("PATCTL_STATE", "/var/lib/project-pat/installer-state.json")


def make_engine(args, dry_run: bool = False) -> Engine:
    host = Host(REPO_ROOT, dry_run=dry_run)
    steps = build_steps(host, REPO_ROOT, nvidia_version=getattr(args, "nvidia_version", None) or os.environ.get("NVIDIA_VERSION"))
    return Engine(steps, State(args.state))


def cmd_plan(args):
    eng = make_engine(args)
    rows = eng.plan(args.phase)
    print(f"{'':2} {'step':20} {'ph':>2}  {'state':10} title")
    for r in rows:
        mark = "✓" if r.satisfied else ("?" if r.satisfied is None else "·")
        flag = " [DESTRUCTIVE]" if r.step.destructive else ""
        blocked = f"  (blocked by {', '.join(r.blocked_by)})" if r.blocked_by and not r.satisfied else ""
        print(f"{mark:2} {r.step.id:20} {r.step.phase:>2}  {r.record.status:10} {r.step.title}{flag}{blocked}")
    return 0


def cmd_install(args, dry_run=False):
    eng = make_engine(args, dry_run=dry_run)
    res = eng.install(args.phase, dry_run=dry_run, understood=tuple(args.i_understand or ()))
    print(json.dumps(res, indent=2))
    return 0 if res.get("status") in ("ok", "reboot_required") else 1


def cmd_verify(args):
    eng = make_engine(args)
    rows = eng.verify(args.phase)
    bad = [r for r in rows if r.satisfied is not True and not r.step.destructive]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} steps satisfied")
    return 1 if bad else 0


def cmd_status(args):
    eng = make_engine(args)
    print(json.dumps(eng.status(), indent=2))
    return 0


def cmd_resume(args):
    eng = make_engine(args)
    eng.clear_reboot()          # the admin says the reboot happened; checks will prove it
    return cmd_install(args)


def cmd_repair(args):
    eng = make_engine(args)
    print(json.dumps(eng.repair(args.step, understood=tuple(args.i_understand or ())), indent=2))
    return 0


def cmd_rollback(args):
    eng = make_engine(args)
    return 0 if eng.rollback(args.step) else 1


def cmd_uninstall(args):
    """Safe uninstall of components *this installer* created: guests (by id) and host changes with rollbacks.
    Never touches anything not in infra/guests or not recorded as applied in the state file."""
    eng = make_engine(args)
    st = eng.state.data["steps"]
    created = [sid for sid, rec in st.items() if sid.startswith("guest-") and rec.get("status") == "applied"]
    if not created and not args.i_understand:
        print("Nothing recorded as created by this installer.")
        return 0
    print("Would remove guests created by patctl: " + ", ".join(created))
    if "uninstall" not in (args.i_understand or ()):
        print("Refusing without --i-understand uninstall (destroys the listed guests and their disks).")
        return 1
    for sid in created:
        eng.rollback(sid)
    for sid in ("40-network", "20-nvidia-host"):
        if st.get(sid, {}).get("status") == "applied":
            eng.rollback(sid)
    return 0


def cmd_guests(args):
    for g in load_guests(REPO_ROOT):
        print(f"{g['kind'].upper():3} {g['id']}  {g['name']:12} phase {g['phase']}  {g['cores']}c {g['memory']}MiB {g['disk_gb']}GB  {g['network']}{'  GPU' if g.get('gpu') else ''}")
    return 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default=STATE_PATH)
    common.add_argument("--phase", type=int, default=None, help="run steps up to and including this phase")
    common.add_argument("--nvidia-version", default=None, help="NVIDIA production driver version for step 20 (e.g. 580.95.05)")
    common.add_argument("--i-understand", action="append", metavar="STEP", help="authorize a specific destructive step")
    ap = argparse.ArgumentParser(prog="patctl", description="Project PAT installer", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("plan", cmd_plan), ("install", cmd_install), ("verify", cmd_verify), ("status", cmd_status),
                     ("resume", cmd_resume), ("uninstall", cmd_uninstall), ("guests", cmd_guests)]:
        sub.add_parser(name, parents=[common]).set_defaults(fn=fn)
    sub.add_parser("dry-run", parents=[common]).set_defaults(fn=lambda a: cmd_install(a, dry_run=True))
    for name, fn in [("repair", cmd_repair), ("rollback", cmd_rollback)]:
        p = sub.add_parser(name, parents=[common]); p.add_argument("--step", required=True); p.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
