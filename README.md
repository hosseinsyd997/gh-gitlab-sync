# gh-gitlab-sync

Bidirectional issue/PR↔MR sync between a GitHub org/account (source of truth for code,
mirrored into GitLab) and an internal GitLab instance. Code itself is already synced by
GitLab's repository mirroring (pull mirror from GitHub) — this script only syncs the
*tracker* data (issues, PRs/MRs, comments), which mirroring does not touch.

## Model

- GitHub → GitLab: new/updated GitHub issues and PRs get mirrored into GitLab as
  issues/MRs, tagged with a hidden marker so they're recognized as mirrors.
- GitLab → GitHub: new GitLab issues/MRs that do NOT carry that marker (i.e. created
  directly in GitLab, not originating from GitHub) get mirrored back up to GitHub.
- Comments sync the same way, in both directions.
- State (which GitHub item maps to which GitLab item, and vice versa) lives in a local
  SQLite file (`state.db`) next to the script — back this up, losing it causes duplicates
  on the next run.
- Everything is incremental: each repo pair tracks its own "last synced" timestamp and
  only asks each API for items updated since then.

## MR/PR branch caveat (read before enabling push_new_branches)

GitLab's mirror here is **pull-mirror, one-way GitHub → GitLab**. A branch created
directly in GitLab (e.g. for a GitLab-only MR) does not exist on GitHub automatically.
To open the matching GitHub PR, this script would need to `git push` that branch to
GitHub first — which is the only operation in this tool that writes code, not just
tracker metadata. It's gated behind `push_new_branches: false` (default) in config.yaml.
Leave it off until you've reviewed what it does; GitLab-only issues/MRs will otherwise
just be skipped (logged, not silently dropped) until you turn it on.

## Setup

1. `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
2. Copy `config.example.yaml` to `config.yaml`, fill in:
   - `github.token` — a GitHub token with `repo` scope (push scope needed only if you'll
     enable `push_new_branches`)
   - `gitlab.url` — your GitLab instance base URL
   - `gitlab.token` — a GitLab personal/project access token with `api` scope
   - `repo_pairs` — list of `{github: "owner/repo", gitlab: "group/project"}`. If your
     GitLab project paths exactly mirror the GitHub `owner/repo` path, you can instead set
     `auto_discover: true` + `gitlab_group_prefix` and skip listing every pair by hand.
3. Dry run first: `python3 sync.py --once --dry-run` — logs what it *would* do, writes
   nothing. Review the log before trusting it.
4. Real run: `python3 sync.py --once` (no `--dry-run`).
5. Cron, every 5 minutes:
   ```
   */5 * * * * cd /opt/gh-gitlab-sync && venv/bin/python3 sync.py --once >> /var/log/gh-gitlab-sync.log 2>&1
   ```
6. Watch `/var/log/gh-gitlab-sync.log` for the first several runs.

## Rate limits

GitHub REST: 5000 req/hour authenticated. This script only asks for items updated since
the last run (not a full re-fetch), so a 5-minute cycle across dozens of repos stays
well under that — see the log's per-cycle request count if you ever need to confirm.
