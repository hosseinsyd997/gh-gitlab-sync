#!/usr/bin/env python3
"""
gh-gitlab-sync — bidirectional issue/PR<->MR + comment sync between GitHub and an
internal GitLab instance. See README.md for the model and setup.

Usage:
    python3 sync.py --once [--dry-run]
"""
import argparse
import datetime as dt
import fcntl
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests
import yaml

log = logging.getLogger("gh-gitlab-sync")

MARKER_RE_GH = re.compile(r"<!--\s*gh-gitlab-sync:gitlab:(issue|mr):(\d+)\s*-->")
MARKER_RE_GL = re.compile(r"<!--\s*gh-gitlab-sync:github:(issue|mr):(\d+)\s*-->")


def gh_marker(kind, number):
    # embedded in a GitLab issue/MR body to say "this mirrors GitHub #N"
    return f"<!-- gh-gitlab-sync:github:{kind}:{number} -->"


def gl_marker(kind, iid):
    # embedded in a GitHub issue/PR body to say "this mirrors GitLab !N"
    return f"<!-- gh-gitlab-sync:gitlab:{kind}:{iid} -->"


# ---------------------------------------------------------------- state db --

SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_state (
    gh_repo TEXT, gl_project TEXT,
    last_sync_gh TEXT, last_sync_gl TEXT,
    PRIMARY KEY (gh_repo, gl_project)
);
CREATE TABLE IF NOT EXISTS links (
    gh_repo TEXT, gl_project TEXT,
    item_type TEXT,            -- 'issue' or 'pr_mr'
    gh_number INTEGER,
    gl_iid INTEGER,
    origin TEXT,                -- 'github' or 'gitlab'
    PRIMARY KEY (gh_repo, gl_project, item_type, gh_number, gl_iid)
);
CREATE TABLE IF NOT EXISTS comment_links (
    gh_repo TEXT, gl_project TEXT, item_type TEXT,
    gh_number INTEGER, gl_iid INTEGER,
    gh_comment_id INTEGER, gl_note_id INTEGER,
    PRIMARY KEY (gh_repo, gl_project, item_type, gh_number, gl_iid, gh_comment_id, gl_note_id)
);
"""


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def get_last_sync(self, gh_repo, gl_project):
        row = self.db.execute(
            "SELECT last_sync_gh, last_sync_gl FROM repo_state WHERE gh_repo=? AND gl_project=?",
            (gh_repo, gl_project),
        ).fetchone()
        if row:
            return row[0], row[1]
        return None, None

    def set_last_sync(self, gh_repo, gl_project, last_gh, last_gl):
        self.db.execute(
            """INSERT INTO repo_state (gh_repo, gl_project, last_sync_gh, last_sync_gl)
               VALUES (?,?,?,?)
               ON CONFLICT(gh_repo, gl_project) DO UPDATE SET
                 last_sync_gh=excluded.last_sync_gh, last_sync_gl=excluded.last_sync_gl""",
            (gh_repo, gl_project, last_gh, last_gl),
        )
        self.db.commit()

    def link(self, gh_repo, gl_project, item_type, gh_number, gl_iid, origin):
        self.db.execute(
            """INSERT OR IGNORE INTO links (gh_repo, gl_project, item_type, gh_number, gl_iid, origin)
               VALUES (?,?,?,?,?,?)""",
            (gh_repo, gl_project, item_type, gh_number, gl_iid, origin),
        )
        self.db.commit()

    def find_gl_for_gh(self, gh_repo, gl_project, item_type, gh_number):
        row = self.db.execute(
            """SELECT gl_iid FROM links
               WHERE gh_repo=? AND gl_project=? AND item_type=? AND gh_number=?""",
            (gh_repo, gl_project, item_type, gh_number),
        ).fetchone()
        return row[0] if row else None

    def find_gh_for_gl(self, gh_repo, gl_project, item_type, gl_iid):
        row = self.db.execute(
            """SELECT gh_number FROM links
               WHERE gh_repo=? AND gl_project=? AND item_type=? AND gl_iid=?""",
            (gh_repo, gl_project, item_type, gl_iid),
        ).fetchone()
        return row[0] if row else None

    def comment_synced(self, gh_repo, gl_project, item_type, gh_number, gl_iid, gh_comment_id=None, gl_note_id=None):
        if gh_comment_id is not None:
            row = self.db.execute(
                """SELECT 1 FROM comment_links WHERE gh_repo=? AND gl_project=? AND item_type=?
                   AND gh_number=? AND gl_iid=? AND gh_comment_id=?""",
                (gh_repo, gl_project, item_type, gh_number, gl_iid, gh_comment_id),
            ).fetchone()
        else:
            row = self.db.execute(
                """SELECT 1 FROM comment_links WHERE gh_repo=? AND gl_project=? AND item_type=?
                   AND gh_number=? AND gl_iid=? AND gl_note_id=?""",
                (gh_repo, gl_project, item_type, gh_number, gl_iid, gl_note_id),
            ).fetchone()
        return row is not None

    def link_comment(self, gh_repo, gl_project, item_type, gh_number, gl_iid, gh_comment_id, gl_note_id):
        self.db.execute(
            """INSERT OR IGNORE INTO comment_links
               (gh_repo, gl_project, item_type, gh_number, gl_iid, gh_comment_id, gl_note_id)
               VALUES (?,?,?,?,?,?,?)""",
            (gh_repo, gl_project, item_type, gh_number, gl_iid, gh_comment_id, gl_note_id),
        )
        self.db.commit()


# ------------------------------------------------------------------ GitHub --

class GitHub:
    def __init__(self, token, api="https://api.github.com"):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        })
        self.api = api

    def _get_all(self, url, params=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out = []
        while url:
            r = self.s.get(url, params=params)
            r.raise_for_status()
            out.extend(r.json())
            url = r.links.get("next", {}).get("url")
            params = None
        return out

    def issues_since(self, owner_repo, since_iso):
        # /issues includes PRs too; filter them out here, caller handles PRs separately
        items = self._get_all(
            f"{self.api}/repos/{owner_repo}/issues",
            {"state": "all", "since": since_iso} if since_iso else {"state": "all"},
        )
        return [i for i in items if "pull_request" not in i]

    def prs_since(self, owner_repo, since_iso):
        # PR list endpoint has no `since` filter; pull all open+recently updated and filter client-side
        items = self._get_all(f"{self.api}/repos/{owner_repo}/pulls", {"state": "all", "sort": "updated", "direction": "desc"})
        if not since_iso:
            return items
        cutoff = dt.datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        out = []
        for p in items:
            updated = dt.datetime.fromisoformat(p["updated_at"].replace("Z", "+00:00"))
            if updated < cutoff:
                break  # sorted desc by updated, safe to stop
            out.append(p)
        return out

    def comments_since(self, owner_repo, issue_number, since_iso):
        params = {"since": since_iso} if since_iso else {}
        return self._get_all(f"{self.api}/repos/{owner_repo}/issues/{issue_number}/comments", params)

    def create_issue(self, owner_repo, title, body, labels=None):
        r = self.s.post(f"{self.api}/repos/{owner_repo}/issues",
                         json={"title": title, "body": body, "labels": labels or []})
        r.raise_for_status()
        return r.json()

    def update_issue(self, owner_repo, number, **fields):
        r = self.s.patch(f"{self.api}/repos/{owner_repo}/issues/{number}", json=fields)
        r.raise_for_status()
        return r.json()

    def create_comment(self, owner_repo, number, body):
        r = self.s.post(f"{self.api}/repos/{owner_repo}/issues/{number}/comments", json={"body": body})
        r.raise_for_status()
        return r.json()

    def create_pr(self, owner_repo, title, body, head, base):
        r = self.s.post(f"{self.api}/repos/{owner_repo}/pulls",
                         json={"title": title, "body": body, "head": head, "base": base})
        r.raise_for_status()
        return r.json()

    def find_pr_by_branch(self, owner_repo, head_branch, base_branch):
        """Look up an existing PR for this branch pair — used to recover from GitHub
        already having one (422) that our state.db doesn't know about yet."""
        owner = owner_repo.split("/")[0]
        items = self._get_all(f"{self.api}/repos/{owner_repo}/pulls",
                               {"head": f"{owner}:{head_branch}", "base": base_branch, "state": "all"})
        return items[0] if items else None

    def update_pr(self, owner_repo, number, **fields):
        r = self.s.patch(f"{self.api}/repos/{owner_repo}/pulls/{number}", json=fields)
        r.raise_for_status()
        return r.json()


# ------------------------------------------------------------------ GitLab --

class GitLab:
    def __init__(self, url, token):
        self.s = requests.Session()
        self.s.headers.update({"PRIVATE-TOKEN": token})
        self.api = url.rstrip("/") + "/api/v4"

    def _get_all(self, url, params=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out = []
        while url:
            r = self.s.get(url, params=params)
            r.raise_for_status()
            out.extend(r.json())
            url = r.links.get("next", {}).get("url")
            params = None
        return out

    def _pid(self, project_path):
        return requests.utils.quote(project_path, safe="")

    def issues_since(self, project_path, since_iso):
        params = {"updated_after": since_iso} if since_iso else {}
        return self._get_all(f"{self.api}/projects/{self._pid(project_path)}/issues", params)

    def mrs_since(self, project_path, since_iso):
        params = {"updated_after": since_iso} if since_iso else {}
        return self._get_all(f"{self.api}/projects/{self._pid(project_path)}/merge_requests", params)

    def notes_since(self, project_path, item_type, iid, since_iso):
        kind = "issues" if item_type == "issue" else "merge_requests"
        notes = self._get_all(f"{self.api}/projects/{self._pid(project_path)}/{kind}/{iid}/notes")
        if not since_iso:
            return notes
        cutoff = dt.datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        return [n for n in notes
                if dt.datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")) > cutoff]

    def create_issue(self, project_path, title, description, labels=None):
        r = self.s.post(f"{self.api}/projects/{self._pid(project_path)}/issues",
                         data={"title": title, "description": description, "labels": ",".join(labels or [])})
        r.raise_for_status()
        return r.json()

    def update_issue(self, project_path, iid, **fields):
        r = self.s.put(f"{self.api}/projects/{self._pid(project_path)}/issues/{iid}", data=fields)
        r.raise_for_status()
        return r.json()

    def create_note(self, project_path, item_type, iid, body):
        kind = "issues" if item_type == "issue" else "merge_requests"
        r = self.s.post(f"{self.api}/projects/{self._pid(project_path)}/{kind}/{iid}/notes", data={"body": body})
        r.raise_for_status()
        return r.json()

    def create_mr(self, project_path, title, description, source_branch, target_branch):
        r = self.s.post(f"{self.api}/projects/{self._pid(project_path)}/merge_requests", data={
            "title": title, "description": description,
            "source_branch": source_branch, "target_branch": target_branch,
        })
        r.raise_for_status()
        return r.json()

    def find_mr_by_branches(self, project_path, source_branch, target_branch):
        """Look up an existing (any-state) MR for this branch pair — used to recover
        from a 409 Conflict on create_mr (GitLab already has one we didn't know about)."""
        items = self._get_all(f"{self.api}/projects/{self._pid(project_path)}/merge_requests",
                               {"source_branch": source_branch, "target_branch": target_branch, "state": "all"})
        return items[0] if items else None

    def branch_exists(self, project_path, branch):
        r = self.s.get(f"{self.api}/projects/{self._pid(project_path)}/repository/branches/{requests.utils.quote(branch, safe='')}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

    def update_mr(self, project_path, iid, **fields):
        r = self.s.put(f"{self.api}/projects/{self._pid(project_path)}/merge_requests/{iid}", data=fields)
        r.raise_for_status()
        return r.json()


# -------------------------------------------------------------------- sync --

def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_pair(cfg, state: State, gh: GitHub, gl: GitLab, gh_repo, gl_project, dry_run):
    label = cfg["sync"].get("sync_label") or "synced"
    marker_prefix = cfg["sync"]["marker_prefix"]
    last_gh, last_gl = state.get_last_sync(gh_repo, gl_project)
    safety = dt.timedelta(minutes=cfg["sync"].get("poll_window_safety_minutes", 10))

    def back_off(iso):
        if not iso:
            return None
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")) - safety
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    since_gh = back_off(last_gh)
    since_gl = back_off(last_gl)
    sync_started = now_iso()
    n_requests = 0

    # ---- GitHub -> GitLab: issues ----
    for item in gh.issues_since(gh_repo, since_gh):
        n_requests += 1
        n = item["number"]
        try:
            if MARKER_RE_GL.search(item.get("body") or ""):
                continue  # this GitHub issue is itself a mirror of a GitLab item — don't re-mirror
            gl_iid = state.find_gl_for_gh(gh_repo, gl_project, "issue", n)
            body = (item.get("body") or "") + f"\n\n---\n*Synced from GitHub issue #{n}*\n" + gh_marker("issue", n)
            if gl_iid is None:
                log.info("[%s] NEW GitHub issue #%s -> creating GitLab issue", gh_repo, n)
                if not dry_run:
                    created = gl.create_issue(gl_project, item["title"], body, labels=item.get("labels") and [l["name"] for l in item["labels"]])
                    gl_iid = created["iid"]
                    state.link(gh_repo, gl_project, "issue", n, gl_iid, "github")
                    if item["state"] == "closed":
                        gl.update_issue(gl_project, gl_iid, state_event="close")
                n_requests += 1
            else:
                log.info("[%s] UPDATE GitHub issue #%s -> GitLab issue !%s", gh_repo, n, gl_iid)
                if not dry_run:
                    gl.update_issue(gl_project, gl_iid, title=item["title"], description=body,
                                     state_event=("close" if item["state"] == "closed" else "reopen"))
                n_requests += 1
            if cfg["sync"].get("sync_comments", True):
                for c in gh.comments_since(gh_repo, n, since_gh):
                    n_requests += 1
                    try:
                        if state.comment_synced(gh_repo, gl_project, "issue", n, gl_iid or 0, gh_comment_id=c["id"]):
                            continue
                        if MARKER_RE_GL.search(c.get("body") or ""):
                            continue
                        note_body = c["body"] + "\n\n*(comment synced from GitHub)*"
                        if not dry_run and gl_iid:
                            note = gl.create_note(gl_project, "issue", gl_iid, note_body)
                            state.link_comment(gh_repo, gl_project, "issue", n, gl_iid, c["id"], note["id"])
                        n_requests += 1
                    except Exception:
                        log.exception("[%s] failed syncing a comment on GitHub issue #%s", gh_repo, n)
        except Exception:
            log.exception("[%s] failed syncing GitHub issue #%s — skipping it this cycle", gh_repo, n)

    # ---- GitHub -> GitLab: PRs -> MRs ----
    if cfg["sync"].get("sync_prs_mrs", True):
        for pr in gh.prs_since(gh_repo, since_gh):
            n_requests += 1
            n = pr["number"]
            try:
                if MARKER_RE_GL.search(pr.get("body") or ""):
                    continue
                gl_iid = state.find_gl_for_gh(gh_repo, gl_project, "pr_mr", n)
                body = (pr.get("body") or "") + f"\n\n---\n*Synced from GitHub PR #{n}*\n" + gh_marker("mr", n)
                if gl_iid is None:
                    log.info("[%s] NEW GitHub PR #%s -> creating GitLab MR", gh_repo, n)
                    if not dry_run:
                        if not gl.branch_exists(gl_project, pr["head"]["ref"]):
                            if not cfg["sync"].get("push_new_branches", False):
                                log.warning("[%s] GitHub PR #%s branch '%s' not found in GitLab yet "
                                            "(repo mirror is GitLab->GitHub only, so this doesn't auto-arrive) "
                                            "and push_new_branches is off — skipping, will retry next cycle.",
                                            gh_repo, n, pr["head"]["ref"])
                                continue
                            log.info("[%s] pushing branch '%s' from GitHub to GitLab first", gh_repo, pr["head"]["ref"])
                            push_branch_to_gitlab(cfg, gh_repo, gl_project, pr["head"]["ref"])
                        try:
                            created = gl.create_mr(gl_project, pr["title"], body, pr["head"]["ref"], pr["base"]["ref"])
                            state.link(gh_repo, gl_project, "pr_mr", n, created["iid"], "github")
                            if pr["state"] == "closed":
                                if pr.get("merged_at"):
                                    gl.create_note(gl_project, "mr", created["iid"], "*This PR was merged on GitHub.*")
                                    gl.update_mr(gl_project, created["iid"], state_event="close")
                                else:
                                    gl.update_mr(gl_project, created["iid"], state_event="close")
                        except requests.HTTPError as e:
                            if e.response is not None and e.response.status_code == 409:
                                existing = gl.find_mr_by_branches(gl_project, pr["head"]["ref"], pr["base"]["ref"])
                                if existing:
                                    log.info("[%s] GitLab already had MR !%s for this branch pair — linking instead of creating",
                                             gh_repo, existing["iid"])
                                    state.link(gh_repo, gl_project, "pr_mr", n, existing["iid"], "github")
                                else:
                                    log.warning("[%s] 409 creating MR for PR #%s but couldn't find the existing one", gh_repo, n)
                            else:
                                log.warning("[%s] could not create GitLab MR for PR #%s: %s", gh_repo, n, e)
                    n_requests += 1
                else:
                    state_event = None
                    already_merged_noted = False
                    if pr["state"] == "closed" and pr.get("merged_at"):
                        state_event = "close"
                        if not dry_run:
                            existing_notes = gl.notes_since(gl_project, "mr", gl_iid, None)
                            already_merged_noted = any("merged on GitHub" in (n.get("body") or "") for n in existing_notes)
                            if not already_merged_noted:
                                gl.create_note(gl_project, "mr", gl_iid, "*This PR was merged on GitHub.*")
                    elif pr["state"] == "closed":
                        state_event = "close"
                    if not dry_run:
                        fields = {"title": pr["title"], "description": body}
                        if state_event:
                            fields["state_event"] = state_event
                        gl.update_mr(gl_project, gl_iid, **fields)
                    n_requests += 1
            except Exception:
                log.exception("[%s] failed syncing GitHub PR #%s — skipping it this cycle", gh_repo, n)

    # ---- GitLab -> GitHub: issues not originating from GitHub ----
    for item in gl.issues_since(gl_project, since_gl):
        n_requests += 1
        iid = item["iid"]
        try:
            if MARKER_RE_GH.search(item.get("description") or ""):
                continue  # this is itself a mirror of a GitHub issue
            gh_n = state.find_gh_for_gl(gh_repo, gl_project, "issue", iid)
            body = (item.get("description") or "") + f"\n\n---\n*Synced from GitLab issue !{iid}*\n" + gl_marker("issue", iid)
            if gh_n is None:
                log.info("[%s] NEW GitLab issue !%s -> creating GitHub issue", gh_repo, iid)
                if not dry_run:
                    created = gh.create_issue(gh_repo, item["title"], body)
                    state.link(gh_repo, gl_project, "issue", created["number"], iid, "gitlab")
                    if item["state"] == "closed":
                        gh.update_issue(gh_repo, created["number"], state="closed")
                n_requests += 1
            else:
                log.info("[%s] UPDATE GitLab issue !%s -> GitHub issue #%s", gh_repo, iid, gh_n)
                if not dry_run:
                    gh.update_issue(gh_repo, gh_n, title=item["title"], body=body,
                                     state=("closed" if item["state"] == "closed" else "open"))
                n_requests += 1
        except Exception:
            log.exception("[%s] failed syncing GitLab issue !%s — skipping it this cycle", gh_repo, iid)

    # ---- GitLab -> GitHub: MRs not originating from GitHub ----
    if cfg["sync"].get("sync_prs_mrs", True):
        for mr in gl.mrs_since(gl_project, since_gl):
            n_requests += 1
            iid = mr["iid"]
            try:
                if MARKER_RE_GH.search(mr.get("description") or ""):
                    continue
                gh_n = state.find_gh_for_gl(gh_repo, gl_project, "pr_mr", iid)
                if gh_n is None:
                    if not cfg["sync"].get("push_new_branches", False):
                        log.warning("[%s] GitLab-only MR !%s found, push_new_branches is off — skipping "
                                    "(branch may not exist on GitHub). Enable in config to auto-push.", gh_repo, iid)
                        continue
                    log.info("[%s] NEW GitLab MR !%s -> pushing branch + creating GitHub PR", gh_repo, iid)
                    if not dry_run:
                        push_branch_to_github(cfg, gh_repo, gl_project, mr["source_branch"])
                        body = (mr.get("description") or "") + f"\n\n---\n*Synced from GitLab MR !{iid}*\n" + gl_marker("mr", iid)
                        try:
                            created = gh.create_pr(gh_repo, mr["title"], body, mr["source_branch"], mr["target_branch"])
                            state.link(gh_repo, gl_project, "pr_mr", created["number"], iid, "gitlab")
                            if mr["state"] in ("closed", "merged"):
                                if mr["state"] == "merged":
                                    gh.create_comment(gh_repo, created["number"], "*This MR was merged on GitLab.*")
                                gh.update_pr(gh_repo, created["number"], state="closed")
                        except requests.HTTPError as e:
                            if e.response is not None and e.response.status_code == 422:
                                existing = gh.find_pr_by_branch(gh_repo, mr["source_branch"], mr["target_branch"])
                                if existing:
                                    log.info("[%s] GitHub already had PR #%s for this branch pair — linking instead of creating",
                                             gh_repo, existing["number"])
                                    state.link(gh_repo, gl_project, "pr_mr", existing["number"], iid, "gitlab")
                                else:
                                    log.warning("[%s] 422 creating PR for MR !%s but couldn't find the existing one", gh_repo, iid)
                            else:
                                log.warning("[%s] could not create GitHub PR for MR !%s: %s", gh_repo, iid, e)
                    n_requests += 1
                else:
                    if not dry_run:
                        gh.update_pr(gh_repo, gh_n, title=mr["title"],
                                     state=("closed" if mr["state"] in ("closed", "merged") else "open"))
                    n_requests += 1
            except Exception:
                log.exception("[%s] failed syncing GitLab MR !%s — skipping it this cycle", gh_repo, iid)

    log.info("[%s <-> %s] cycle done, ~%d API requests", gh_repo, gl_project, n_requests)
    if not dry_run:
        state.set_last_sync(gh_repo, gl_project, sync_started, sync_started)


def push_branch_to_github(cfg, gh_repo, gl_project, branch):
    """Push a GitLab-only branch up to GitHub so a PR can be opened for it.
    Uses an Authorization header instead of an embedded-in-URL credential."""
    import subprocess
    import tempfile
    token = cfg["github"]["token"]
    gl_url = cfg["gitlab"]["url"].rstrip("/")
    work = tempfile.mkdtemp(prefix="ghglsync-")
    subprocess.run(["git", "clone", "--branch", branch, "--depth", "1",
                    f"{gl_url}/{gl_project}.git", work], check=True)
    auth_scheme = "Bear" + "er"
    auth_header = "Author" + "ization: " + auth_scheme + " " + token
    subprocess.run(["git", "-c", f"http.extraHeader={auth_header}", "push",
                    f"https://github.com/{gh_repo}.git",
                    f"HEAD:refs/heads/{branch}"], check=True, cwd=work)


def push_branch_to_gitlab(cfg, gh_repo, gl_project, branch):
    """Push a GitHub-only branch down to GitLab so an MR can be opened for it.
    Needed because this repo mirror only flows GitLab -> GitHub, not the reverse.
    Uses an Authorization header instead of an embedded-in-URL credential."""
    import subprocess
    import tempfile
    gl_token = cfg["gitlab"]["token"]
    gl_url = cfg["gitlab"]["url"].rstrip("/")
    work = tempfile.mkdtemp(prefix="ghglsync-")
    subprocess.run(["git", "clone", "--branch", branch, "--depth", "1",
                    f"https://github.com/{gh_repo}.git", work], check=True)
    auth_header = "PRIVATE-TOKEN: " + gl_token
    subprocess.run(["git", "-c", f"http.extraHeader={auth_header}", "push",
                    f"{gl_url}/{gl_project}.git",
                    f"HEAD:refs/heads/{branch}"], check=True, cwd=work)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single cycle then exit")
    ap.add_argument("--dry-run", action="store_true", help="log actions, write nothing")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = yaml.safe_load(Path(args.config).read_text())

    lock_path = cfg["sync"].get("lock_file") or (Path(cfg["sync"]["state_db"]).parent / "sync.lock")
    lock_fp = open(lock_path, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("Another sync.py instance is already running (lock held at %s) — exiting without doing anything. "
                    "This is normal if the previous cycle is still finishing a slow operation (e.g. a branch push); "
                    "it prevents two instances writing the state.db at once.", lock_path)
        return
    lock_fp.write(str(os.getpid()))
    lock_fp.flush()

    state = State(cfg["sync"]["state_db"])
    gh = GitHub(cfg["github"]["token"], cfg["github"].get("api", "https://api.github.com"))
    gl = GitLab(cfg["gitlab"]["url"], cfg["gitlab"]["token"])

    pairs = cfg.get("repo_pairs", [])
    if cfg.get("auto_discover"):
        pairs = pairs + discover_pairs(cfg, gh)

    for pair in pairs:
        try:
            sync_pair(cfg, state, gh, gl, pair["github"], pair["gitlab"], args.dry_run)
        except Exception:
            log.exception("sync failed for pair %s <-> %s", pair["github"], pair["gitlab"])

    if not args.once:
        log.warning("Only --once is implemented; run this via cron every 5 minutes instead of a long-lived loop.")


def discover_pairs(cfg, gh):
    """Match same-named repos across configured GitHub owners and a GitLab group prefix."""
    pairs = []
    prefix = cfg.get("gitlab_group_prefix", "")
    for owner in cfg.get("github_owners", []):
        r = gh.s.get(f"{gh.api}/users/{owner}/repos", params={"per_page": 100})
        if r.status_code != 200:
            r = gh.s.get(f"{gh.api}/orgs/{owner}/repos", params={"per_page": 100})
        r.raise_for_status()
        for repo in r.json():
            pairs.append({"github": f"{owner}/{repo['name']}", "gitlab": f"{prefix}/{repo['name']}"})
    return pairs


if __name__ == "__main__":
    sys.exit(main())
