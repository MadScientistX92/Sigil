"""CLI.

  python -m sigil check photo.png --jurisdictions eu,ca --deepfake
  python -m sigil mark in.png out.png --provider "Acme AI" --system acme-img --version 2.1
  python -m sigil rules --modality text
  python -m sigil keys create --org acme --name prod      # prints the key once
  python -m sigil keys list [--org acme] | keys revoke key_ab12cd34ef56 | keys usage --org acme
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import policy
from .core import MarkOptions, Sigil

G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"


def _ctx(a, modality):
    return policy.Context(modality, tuple(a.jurisdictions.split(",")), a.deepfake, a.monthly_users)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sigil")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("check", "mark", "rules"):
        s = sub.add_parser(name)
        s.add_argument("--jurisdictions", default="eu,ca,in,kr")
        s.add_argument("--deepfake", action="store_true")
        s.add_argument("--monthly-users", type=int, default=0)
        s.add_argument("--data", default="data")
        s.add_argument("--json", action="store_true")
        if name == "check":
            s.add_argument("file")
        if name == "mark":
            s.add_argument("src")
            s.add_argument("dst")
            s.add_argument("--provider", required=True)
            s.add_argument("--system", required=True)
            s.add_argument("--version", default="")
            s.add_argument("--label", choices=["auto", "on", "off"], default="auto")
        if name == "rules":
            s.add_argument("--modality", default="image", choices=policy.MODALITIES)
    k = sub.add_parser("keys")
    k.add_argument("action", choices=["create", "list", "revoke", "usage"])
    k.add_argument("key_id", nargs="?")
    k.add_argument("--org")
    k.add_argument("--name", default="")
    k.add_argument("--period")
    k.add_argument("--data", default="data")
    a = p.parse_args(argv)
    sg = Sigil(a.data)

    if a.cmd == "keys":
        return _keys(sg, a)

    if a.cmd == "rules":
        ctx = _ctx(a, a.modality)
        for r in policy.applicable_rules(ctx):
            print(f"{B}{r.law}{X} [{policy.rule_status(r)}]\n  {r.summary}\n  controls: {', '.join(r.controls)}\n")
        return 0

    if a.cmd == "mark":
        opts = MarkOptions(provider=a.provider, system=a.system, system_version=a.version,
                           jurisdictions=tuple(a.jurisdictions.split(",")), deepfake=a.deepfake,
                           monthly_users=a.monthly_users,
                           visible_label=None if a.label == "auto" else a.label == "on",
                           output_format=Path(a.dst).suffix.lstrip(".").upper() or None)
        out, receipt = sg.mark_image(Path(a.src).read_bytes(), opts)
        Path(a.dst).write_bytes(out)
        print(json.dumps(receipt.to_dict(), indent=2) if a.json else
              f"{G}Marked{X} {a.dst}  id={receipt.content_id}  applied={', '.join(receipt.controls_applied)}")
        for k, v in receipt.still_required.items():
            if not a.json:
                print(f"  {Y}still required:{X} {k}: {v}")
        return 0

    path = Path(a.file)
    if path.suffix.lower() in (".txt", ".md"):
        rep = sg.verify_text(path.read_text(), _ctx(a, "text"))
    else:
        rep = sg.verify_image(path.read_bytes(), _ctx(a, "image"))
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
        return 0
    print(f"{B}{rep['verdict']}{X}")
    for k, v in rep["layers"].items():
        print(f"  {k:22} {v}")
    c = rep["compliance"]
    print(f"\n{B}Compliance ({', '.join(a.jurisdictions.split(','))}):{X} " + (f"{G}OK{X}" if c["ok"] else f"{R}GAPS FOUND{X}"))
    for i in c["items"]:
        col = G if i["pass"] else (Y if i["pass"] is None else R)
        print(f"  {col}{i['status']:16}{X} {i['control']:22} <- {', '.join(i['required_by'])}")
    print("\nEngineering guidance, not legal advice.")
    return 0 if c["ok"] else 1


def _keys(sg, a):
    acc = sg.accounts
    if a.action == "create":
        if not a.org:
            print("--org is required", file=sys.stderr)
            return 2
        k = acc.create_key(a.org, a.name)
        sg.registry.audit(k["org"], "key.create", detail={"key_id": k["id"], "name": a.name})
        print(f"{G}Created{X} {k['id']} for org {k['org']}\n{B}{k['key']}{X}\nStore it now; only its hash is kept.")
    elif a.action == "list":
        print(json.dumps(acc.list_keys(a.org), indent=2))
    elif a.action == "revoke":
        if not a.key_id or not acc.revoke_key(a.key_id):
            print("no active key with that id", file=sys.stderr)
            return 1
        sg.registry.audit("admin", "key.revoke", detail={"key_id": a.key_id})
        print(f"Revoked {a.key_id}")
    else:
        if not a.org:
            print("--org is required", file=sys.stderr)
            return 2
        print(json.dumps(acc.usage(a.org, a.period), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
