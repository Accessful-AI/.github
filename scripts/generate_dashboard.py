#!/usr/bin/env python3
"""Generate the Accessful-AI org CI/CD dashboard (profile/README.md).

Data is fetched via the `gh` CLI (no third-party packages), so the exact same
script runs locally and in GitHub Actions. The dashboard groups results by
repository (one row each) instead of one row per workflow, which is what caused
repositories to appear multiple times before.

Env vars:
  ORGANIZATION_NAME  org to scan (default: Accessful-AI)
  PINNED_REPOS       comma-separated repos pinned to the top
  OUTPUT             file to write (default: profile/README.md); "-" -> stdout
  GH_TOKEN           consumed by `gh` itself for authentication
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

ORG = os.environ.get("ORGANIZATION_NAME", "Accessful-AI")
PINNED = [r.strip() for r in os.environ.get("PINNED_REPOS", "").split(",") if r.strip()]
OUTPUT = os.environ.get("OUTPUT", "profile/README.md")

# --- conclusion / status -> (emoji, label, rank) -----------------------------
# rank drives the aggregate status of a repo: lower number = higher severity.
FAIL = {"failure", "timed_out", "startup_failure"}
RUNNING = {"in_progress", "queued", "requested", "waiting", "pending"}
WARN = {"action_required"}
SKIP = {"skipped", "stale", "neutral"}
CANCEL = {"cancelled", "canceled"}


def classify(conclusion, status):
    """Return (emoji, label, rank) for a single workflow's latest run."""
    c = (conclusion or "").lower()
    s = (status or "").lower()
    if c in FAIL:
        return "❌", c, 0
    if not c and s in RUNNING:
        return "🟡", s.replace("_", " "), 1
    if c in WARN:
        return "⚠️", c.replace("_", " "), 2
    if c == "success":
        return "✅", "success", 3
    if c in CANCEL:
        return "🚫", "cancelled", 4
    if c in SKIP:
        return "⏭️", c, 5
    return "⬜", "no runs", 6


# repo-level aggregate by best (lowest) rank present among its workflows
AGG = {
    0: ("🔴", "Failing"),
    1: ("🟡", "Running"),
    2: ("⚠️", "Attention"),
    3: ("🟢", "Passing"),
    4: ("🚫", "Cancelled"),
    5: ("⏭️", "Skipped"),
    6: ("⬜", "No runs"),
}


def gh_json(path):
    """Call `gh api <path>` and return parsed JSON (single page)."""
    res = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {res.stderr.strip()}")
    return json.loads(res.stdout or "null")


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ago(dt, now):
    """Human friendly 'time ago' for a naive-UTC datetime."""
    if not dt:
        return "—"
    secs = (now - dt).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 90:
        return "just now"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m ago"
    hours = mins / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 14:
        return f"{int(days)}d ago"
    weeks = days / 7
    if weeks < 9:
        return f"{int(weeks)}w ago"
    months = days / 30
    if months < 18:
        return f"{int(months)}mo ago"
    return f"{int(days / 365)}y ago"


def collect_repo(repo):
    """Return a dict describing one repo's workflow status, or None to skip."""
    name = repo["name"]
    full = f"{ORG}/{name}"

    try:
        wf_data = gh_json(f"repos/{full}/actions/workflows?per_page=100")
        workflows = wf_data.get("workflows", [])
    except RuntimeError:
        workflows = []

    # latest run per workflow id, from the repo's recent run history (1 call)
    latest_by_wf = {}
    try:
        runs = gh_json(f"repos/{full}/actions/runs?per_page=100").get("workflow_runs", [])
        for run in runs:
            wid = run.get("workflow_id")
            if wid is None:
                continue
            cur = latest_by_wf.get(wid)
            rdt = parse_dt(run.get("updated_at") or run.get("created_at"))
            if cur is None or (rdt and (cur["dt"] is None or rdt > cur["dt"])):
                latest_by_wf[wid] = {
                    "conclusion": run.get("conclusion"),
                    "status": run.get("status"),
                    "dt": rdt,
                    "url": run.get("html_url"),
                }
    except RuntimeError:
        pass

    items = []
    for wf in workflows:
        wname = wf.get("name") or ""
        if "dependabot" in wname.lower():
            continue
        if (wf.get("state") or "").startswith("disabled"):
            continue
        wid = wf.get("id")
        run = latest_by_wf.get(wid)
        if run is None and wid is not None:
            # workflow ran outside the recent-runs window: targeted lookup so we
            # don't mislabel an old-but-real run as "no runs"
            try:
                one = gh_json(f"repos/{full}/actions/workflows/{wid}/runs?per_page=1")
                hits = one.get("workflow_runs", [])
                if hits:
                    h = hits[0]
                    run = {
                        "conclusion": h.get("conclusion"),
                        "status": h.get("status"),
                        "dt": parse_dt(h.get("updated_at") or h.get("created_at")),
                        "url": h.get("html_url"),
                    }
            except RuntimeError:
                pass
        if run:
            emoji, label, rank = classify(run["conclusion"], run["status"])
            items.append({
                "name": wname,
                "emoji": emoji, "label": label, "rank": rank,
                "dt": run["dt"], "url": run["url"],
            })
        else:
            items.append({
                "name": wname,
                "emoji": "⬜", "label": "no runs", "rank": 6,
                "dt": None, "url": wf.get("html_url"),
            })

    items.sort(key=lambda i: (i["rank"], -(i["dt"].timestamp() if i["dt"] else 0)))

    if items:
        best_rank = min(i["rank"] for i in items)
        last_dt = max((i["dt"] for i in items if i["dt"]), default=None)
    else:
        best_rank = 6
        last_dt = None

    return {
        "name": name,
        "url": repo["html_url"],
        "pushed": parse_dt(repo.get("pushed_at")),
        "agg_rank": best_rank,
        "last_dt": last_dt,
        "items": items,
        "pinned": name in PINNED,
    }


def build(repos, now):
    n = len(repos)
    passing = sum(1 for r in repos if r["agg_rank"] == 3)
    failing = sum(1 for r in repos if r["agg_rank"] == 0)
    running = sum(1 for r in repos if r["agg_rank"] == 1)
    norun = sum(1 for r in repos if r["agg_rank"] == 6)

    out = []
    out.append("# 📊 Accessful-AI · CI/CD Dashboard\n")
    out.append(
        f"![Repos](https://img.shields.io/badge/Repos-{n}-blue?style=flat-square) "
        f"![Passing](https://img.shields.io/badge/Passing-{passing}-brightgreen?style=flat-square) "
        f"![Failing](https://img.shields.io/badge/Failing-{failing}-red?style=flat-square) "
        f"![Running](https://img.shields.io/badge/Running-{running}-yellow?style=flat-square) "
        f"![No Runs](https://img.shields.io/badge/No%20Runs-{norun}-lightgrey?style=flat-square)\n"
    )

    # overview table — one row per repo
    out.append("| Repository | Status | Workflows | Last activity |")
    out.append("| --- | --- | --- | --- |")
    for r in repos:
        emoji, label = AGG[r["agg_rank"]]
        pin = "📌 " if r["pinned"] else ""
        # compact per-status counts
        counts = {}
        for it in r["items"]:
            counts[it["emoji"]] = counts.get(it["emoji"], 0) + 1
        if r["items"]:
            breakdown = " ".join(f"{e} {c}" for e, c in
                                 sorted(counts.items(), key=lambda kv: -kv[1]))
        else:
            breakdown = "—"
        out.append(
            f"| {pin}[{r['name']}]({r['url']}) | {emoji} {label} | "
            f"{breakdown} | {ago(r['last_dt'], now)} |"
        )

    # collapsible per-repo workflow detail
    out.append("\n## 🔎 Workflow details\n")
    for r in repos:
        if not r["items"]:
            continue
        emoji, label = AGG[r["agg_rank"]]
        pin = "📌 " if r["pinned"] else ""
        count = len(r["items"])
        noun = "workflow" if count == 1 else "workflows"
        out.append("<details>")
        out.append(
            f"<summary>{emoji} {pin}<b>{r['name']}</b> · "
            f"{count} {noun} · updated {ago(r['last_dt'], now)}</summary>\n"
        )
        out.append("| Workflow | Status | Last run |")
        out.append("| --- | --- | --- |")
        for it in r["items"]:
            wf_cell = f"[{it['name']}]({it['url']})" if it["url"] else it["name"]
            when = it["dt"].strftime("%Y-%m-%d %H:%M") + " UTC" if it["dt"] else "—"
            out.append(f"| {wf_cell} | {it['emoji']} {it['label']} | {when} |")
        out.append("\n</details>\n")

    out.append(
        "\n> **Legend** — 🟢 Passing · 🔴 Failing · 🟡 Running · "
        "⚠️ Attention · ⏭️ Skipped · 🚫 Cancelled · ⬜ No runs  \n"
        "> Per repo the most severe workflow result determines the status. "
        "Dependabot and disabled workflows are omitted.\n"
    )
    out.append(
        f"\n*Last updated: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC — "
        "auto-generated by the [Update Dashboard]"
        f"(https://github.com/{ORG}/.github/actions/workflows/dashboard.yml) workflow.*\n"
    )
    return "\n".join(out)


def main():
    now = datetime.now(timezone.utc)
    repos_raw = gh_json(f"orgs/{ORG}/repos?per_page=100&type=all")
    repos = []
    for repo in repos_raw:
        if repo.get("archived"):
            continue
        repos.append(collect_repo(repo))
        print(f"  · {repo['name']}", file=sys.stderr)

    # pinned first, then by most recent activity (then pushed_at), then name
    def sort_key(r):
        activity = r["last_dt"] or r["pushed"] or EPOCH
        return (not r["pinned"], -activity.timestamp(), r["name"].lower())

    repos.sort(key=sort_key)
    md = build(repos, now)

    if OUTPUT == "-":
        sys.stdout.write(md)
    else:
        with open(OUTPUT, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"Wrote {OUTPUT} ({len(md)} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
