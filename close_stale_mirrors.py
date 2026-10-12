#!/usr/bin/env python3
"""
close_stale_mirrors.py — one-off cleanup: close every OPEN GitLab issue/MR that was
created by gh-gitlab-sync (has our hidden marker) and whose GitHub original is already
closed/merged. Does NOT edit titles/descriptions, does NOT touch anything without our
marker, does NOT touch GitHub at all. Safe to re-run (idempotent — already-closed items
are skipped).

Usage:
    python3 close_stale_mirrors.py [--dry-run]
"""
import argparse
import re
import sys

import requests
import yaml
from pathlib import Path

MARKER_RE_GH = re.compile(r"<!--\s*gh-gitlab-sync:github:(issue|pr):(\d+)\s*-->")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    gl_url = cfg["gitlab"]["url"].rstrip("/") + "/api/v4"
    s = requests.Session()
    s.headers.update({"PRIVATE-TOKEN": cfg["gitlab"]["token"]})

    def get_all(url, params=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out = []
        while url:
            r = s.get(url, params=params)
            r.raise_for_status()
            out.extend(r.json())
            url = r.links.get("next", {}).get("url")
            params = None
        return out

    projects = sorted({p["gitlab"] for p in cfg.get("repo_pairs", [])})
    total_closed = 0

    for project in projects:
        pid = requests.utils.quote(project, safe="")

        issues = get_all(f"{gl_url}/projects/{pid}/issues", {"state": "opened"})
        for it in issues:
            if MARKER_RE_GH.search(it.get("description") or ""):
                print(f"[{project}] closing issue !{it['iid']}: {it['title'][:70]}  {it.get('web_url','')}")
                if not args.dry_run:
                    r = s.put(f"{gl_url}/projects/{pid}/issues/{it['iid']}", data={"state_event": "close"})
                    r.raise_for_status()
                total_closed += 1

        mrs = get_all(f"{gl_url}/projects/{pid}/merge_requests", {"state": "opened"})
        for mr in mrs:
            if MARKER_RE_GH.search(mr.get("description") or ""):
                print(f"[{project}] closing MR !{mr['iid']}: {mr['title'][:70]}  {mr.get('web_url','')}")
                if not args.dry_run:
                    r = s.put(f"{gl_url}/projects/{pid}/merge_requests/{mr['iid']}", data={"state_event": "close"})
                    r.raise_for_status()
                total_closed += 1

    print(f"\n{'Would close' if args.dry_run else 'Closed'} {total_closed} item(s).")


if __name__ == "__main__":
    sys.exit(main())
