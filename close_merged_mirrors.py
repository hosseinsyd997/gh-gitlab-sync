#!/usr/bin/env python3
"""
close_merged_mirrors.py — one-off cleanup for mirrored GitLab MRs that got stuck open
because their GitHub PR was already merged/closed long ago (so the normal incremental
sync, which only re-checks recently-updated PRs, will never revisit them on its own).

For every OPEN GitLab MR carrying our "Synced from GitHub PR #N" marker, this looks up
PR #N's CURRENT real state directly (one API call per MR, not incremental), and closes
the GitLab MR if GitHub says it's closed/merged. Idempotent — already-closed MRs and
already-posted merge notes are skipped. Does not touch GitHub, does not touch anything
without our marker.

Usage:
    python3 close_merged_mirrors.py [--dry-run]
"""
import argparse
import re
import sys
from pathlib import Path

import requests
import yaml

MARKER_RE_GH = re.compile(r"<!--\s*gh-gitlab-sync:github:(issue|mr):(\d+)\s*-->")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    gl_api = cfg["gitlab"]["url"].rstrip("/") + "/api/v4"
    gl = requests.Session()
    gl.headers.update({"PRIVATE-TOKEN": cfg["gitlab"]["token"]})

    gh = requests.Session()
    gh.headers.update({"Authorization": "Bearer " + cfg["github"]["token"],
                        "Accept": "application/vnd.github+json"})
    gh_api = cfg["github"].get("api", "https://api.github.com")

    def get_all(session, url, params=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out = []
        while url:
            r = session.get(url, params=params)
            r.raise_for_status()
            out.extend(r.json())
            url = r.links.get("next", {}).get("url")
            params = None
        return out

    pairs = cfg.get("repo_pairs", [])
    total_checked = 0
    total_closed = 0

    for pair in pairs:
        gh_repo = pair["github"]
        gl_project = pair["gitlab"]
        pid = requests.utils.quote(gl_project, safe="")

        mrs = get_all(gl, f"{gl_api}/projects/{pid}/merge_requests", {"state": "opened"})
        for mr in mrs:
            m = MARKER_RE_GH.search(mr.get("description") or "")
            if not m or m.group(1) != "mr":
                continue
            pr_number = int(m.group(2))
            total_checked += 1

            r = gh.get(f"{gh_api}/repos/{gh_repo}/pulls/{pr_number}")
            if r.status_code == 404:
                print(f"[{gl_project}] MR !{mr['iid']} -> GitHub PR #{pr_number} not found (deleted?) — skipping")
                continue
            r.raise_for_status()
            pr = r.json()

            if pr["state"] != "closed":
                continue  # genuinely still open on GitHub, leave it alone

            merged = bool(pr.get("merged_at"))
            print(f"[{gl_project}] MR !{mr['iid']} <- GitHub PR #{pr_number} is "
                  f"{'merged' if merged else 'closed (not merged)'} -> closing MR  {mr.get('web_url','')}")
            total_closed += 1
            if args.dry_run:
                continue

            if merged:
                existing_notes = get_all(gl, f"{gl_api}/projects/{pid}/merge_requests/{mr['iid']}/notes")
                already_noted = any("merged on GitHub" in (n.get("body") or "") for n in existing_notes)
                if not already_noted:
                    nr = gl.post(f"{gl_api}/projects/{pid}/merge_requests/{mr['iid']}/notes",
                                 data={"body": "*This PR was merged on GitHub.*"})
                    nr.raise_for_status()

            ur = gl.put(f"{gl_api}/projects/{pid}/merge_requests/{mr['iid']}", data={"state_event": "close"})
            ur.raise_for_status()

    print(f"\nChecked {total_checked} mirrored MR(s); "
          f"{'would close' if args.dry_run else 'closed'} {total_closed}.")


if __name__ == "__main__":
    sys.exit(main())
