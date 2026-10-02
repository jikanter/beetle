#!/usr/bin/env python3.13
"""beetle — cross-repository git coordinator.

Named after the Beetle probes in *Project Hail Mary*: small couriers that carry
messages between distant places. This tool carries git operations across the
several repositories that make up one integrated system (e.g. aichat ↔
llm-functions ↔ astrophage ↔ harness) without ever hardcoding host paths.

Repo manifest resolution (first hit wins):
  1. $BEETLE_REPOS                          (set this in settings.local.json)
  2. $XDG_CONFIG_HOME/beetle/repos.json
  3. ~/.config/beetle/repos.json

The manifest lives OUTSIDE every repo on purpose — it is machine-local, never
committed. This mirrors the portable mcp.json artifact pattern in aichat's
docs/architecture/integrated-architecture/SPEC-mcp-json-artifact.md.

Zero non-stdlib dependencies. Requires python3.11+ for nothing in particular;
JSON keeps parsing in the standard library so no PyYAML/yq is needed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Manifest resolution & loading
# ---------------------------------------------------------------------------


def default_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "beetle"


def manifest_path(explicit: str | None = None) -> Path:
    """Resolve the manifest location without requiring it to exist."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("BEETLE_REPOS")
    if env:
        return Path(env).expanduser()
    return default_config_dir() / "repos.json"


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


@dataclass
class Repo:
    name: str
    path: Path
    url: str | None = None
    role: str | None = None
    remote: str = "origin"  # the git remote `url` must match (doctor)

    @property
    def git_dir(self) -> Path:
        return self.path / ".git"

    def exists(self) -> bool:
        return self.path.is_dir()

    def is_git(self) -> bool:
        return self.git_dir.exists()


def load_repos(explicit: str | None = None) -> tuple[Path, list[Repo]]:
    mpath = manifest_path(explicit)
    if not mpath.exists():
        die(
            f"no manifest at {mpath}\n"
            f"  run:  beetle init        # scaffold one from the example\n"
            f"  or set BEETLE_REPOS in settings.local.json to point elsewhere"
        )
    try:
        data = json.loads(mpath.read_text())
    except json.JSONDecodeError as e:
        die(f"manifest {mpath} is not valid JSON: {e}")
    repos = []
    for i, entry in enumerate(data.get("repos", [])):
        if "name" not in entry or "path" not in entry:
            die(f"manifest entry #{i} missing required 'name' or 'path'")
        repos.append(
            Repo(
                name=entry["name"],
                path=_expand(entry["path"]),
                url=entry.get("url"),
                role=entry.get("role"),
                remote=entry.get("remote", "origin"),
            )
        )
    if not repos:
        die(f"manifest {mpath} has no repos")
    return mpath, repos


def select(repos: list[Repo], only: str | None) -> list[Repo]:
    if not only:
        return repos
    wanted = {n.strip() for n in only.split(",") if n.strip()}
    chosen = [r for r in repos if r.name in wanted]
    missing = wanted - {r.name for r in chosen}
    if missing:
        die(f"--only names not in manifest: {', '.join(sorted(missing))}")
    return chosen


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def git(repo: Repo, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo.path), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def current_branch(repo: Repo) -> str:
    r = git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    return r.stdout.strip() or "(detached)"


def head_short(repo: Repo) -> str:
    return git(repo, "rev-parse", "--short", "HEAD").stdout.strip()


def is_dirty(repo: Repo) -> bool:
    return bool(git(repo, "status", "--porcelain").stdout.strip())


def ahead_behind(repo: Repo) -> tuple[int, int] | None:
    r = git(repo, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    behind, ahead = r.stdout.split()
    return int(ahead), int(behind)


def remote_url(repo: Repo) -> str | None:
    r = git(repo, "remote", "get-url", repo.remote)
    return r.stdout.strip() if r.returncode == 0 else None


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"beetle: {msg}", file=sys.stderr)
    sys.exit(code)


def emit(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2))


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_init(args) -> int:
    dest = manifest_path(args.manifest)
    if dest.exists() and not args.force:
        die(f"{dest} already exists (use --force to overwrite)")
    example = Path(__file__).resolve().parent.parent / "repos.example.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(example.read_text())
    print(f"scaffolded manifest at {dest}")
    print("edit it to point at your repos, then run:  beetle doctor")
    return 0


def cmd_list(args) -> int:
    mpath, repos = load_repos(args.manifest)
    if args.json:
        emit(
            {
                "manifest": str(mpath),
                "repos": [
                    {"name": r.name, "path": str(r.path), "url": r.url, "role": r.role}
                    for r in repos
                ],
            },
            True,
        )
        return 0
    print(f"manifest: {mpath}")
    for r in select(repos, args.only):
        tag = f" [{r.role}]" if r.role else ""
        print(f"  {r.name}{tag}\n    {r.path}")
        if r.url:
            print(f"    {r.url}")
    return 0


def cmd_doctor(args) -> int:
    mpath, repos = load_repos(args.manifest)
    repos = select(repos, args.only)
    problems = 0
    rows = []
    for r in repos:
        issues = []
        if not r.exists():
            issues.append("path missing")
        elif not r.is_git():
            issues.append("not a git repo")
        else:
            if r.url:
                actual = remote_url(r)
                if actual and not _urls_match(actual, r.url):
                    issues.append(f"{r.remote}={actual} != manifest url")
                elif actual is None:
                    issues.append(f"no {r.remote} remote")
        problems += len(issues)
        rows.append((r.name, "OK" if not issues else "; ".join(issues)))
    if args.json:
        emit({"manifest": str(mpath), "checks": dict(rows)}, True)
    else:
        for name, status in rows:
            mark = "✓" if status == "OK" else "✗"
            print(f"  {mark} {name}: {status}")
    return 1 if problems else 0


def _urls_match(a: str, b: str) -> bool:
    """Loose compare: host + path only. Ignores scheme, user, port and a .git
    suffix, so scp/ssh/https forms match, and a self-hosted forge whose ssh
    and http ports differ (Gitea `ssh://git@host:2221/o/r` vs
    `http://host:3006/o/r`) matches too."""
    def norm(u: str) -> str:
        u = u.strip().removesuffix("/").removesuffix(".git")
        u = u.split("://", 1)[1] if "://" in u else u.replace(":", "/", 1)  # scp form
        u = u.rsplit("@", 1)[-1]  # drop user
        host, _, path = u.partition("/")
        return f"{host.split(':', 1)[0]}/{path}".rstrip("/")
    return norm(a) == norm(b)


def cmd_status(args) -> int:
    mpath, repos = load_repos(args.manifest)
    repos = select(repos, args.only)
    out = []
    for r in repos:
        if not r.is_git():
            out.append({"repo": r.name, "error": "not a git repo" if r.exists() else "path missing"})
            continue
        ab = ahead_behind(r)
        out.append(
            {
                "repo": r.name,
                "branch": current_branch(r),
                "head": head_short(r),
                "dirty": is_dirty(r),
                "ahead": ab[0] if ab else None,
                "behind": ab[1] if ab else None,
            }
        )
    if args.json:
        emit({"manifest": str(mpath), "status": out}, True)
        return 0
    w = max((len(o["repo"]) for o in out), default=4)
    print(f"  {'REPO'.ljust(w)}  BRANCH                 STATE")
    for o in out:
        if "error" in o:
            print(f"  {o['repo'].ljust(w)}  -- {o['error']}")
            continue
        flags = []
        if o["dirty"]:
            flags.append("dirty")
        if o["ahead"]:
            flags.append(f"↑{o['ahead']}")
        if o["behind"]:
            flags.append(f"↓{o['behind']}")
        if o["ahead"] is None:
            flags.append("no-upstream")
        state = " ".join(flags) or "clean"
        print(f"  {o['repo'].ljust(w)}  {o['branch'][:20].ljust(20)}  {state}")
    return 0


def cmd_sync(args) -> int:
    mpath, repos = load_repos(args.manifest)
    repos = select(repos, args.only)
    rc = 0
    for r in repos:
        if not r.is_git():
            print(f"  {r.name}: skip (not a git repo)")
            rc = 1
            continue
        fetch = git(r, "fetch", "--all", "--prune")
        if fetch.returncode != 0:
            print(f"  {r.name}: fetch failed\n{fetch.stderr.strip()}")
            rc = 1
            continue
        if args.pull:
            if is_dirty(r):
                print(f"  {r.name}: fetched; skip pull (working tree dirty)")
                continue
            pull = git(r, "merge", "--ff-only", "@{upstream}")
            if pull.returncode != 0:
                print(f"  {r.name}: fetched; ff-only merge failed (diverged?)")
                rc = 1
            else:
                print(f"  {r.name}: synced (ff)")
        else:
            print(f"  {r.name}: fetched")
    return rc


def cmd_branch(args) -> int:
    mpath, repos = load_repos(args.manifest)
    repos = select(repos, args.only)
    rc = 0
    for r in repos:
        if not r.is_git():
            print(f"  {r.name}: skip (not a git repo)")
            rc = 1
            continue
        exists = git(r, "rev-parse", "--verify", "-q", f"refs/heads/{args.name}").returncode == 0
        if exists:
            res = git(r, "checkout", args.name)
            verb = "checked out"
        else:
            cmd = ["checkout", "-b", args.name]
            if args.base:
                cmd.append(args.base)
            res = git(r, *cmd)
            verb = "created"
        if res.returncode != 0:
            print(f"  {r.name}: branch failed\n{res.stderr.strip()}")
            rc = 1
        else:
            print(f"  {r.name}: {verb} {args.name}")
    return rc


def cmd_run(args) -> int:
    mpath, repos = load_repos(args.manifest)
    repos = select(repos, args.only)
    if not args.gitargs:
        die("nothing to run; pass git args after --, e.g. beetle run -- log --oneline -1")
    rc = 0
    for r in repos:
        if not r.is_git():
            print(f"  {r.name}: skip (not a git repo)")
            rc = 1
            continue
        res = git(r, *args.gitargs)
        rc = rc or res.returncode
        print(f"── {r.name} " + "─" * max(0, 40 - len(r.name)))
        if res.stdout:
            print(res.stdout.rstrip())
        if res.stderr:
            print(res.stderr.rstrip(), file=sys.stderr)
    return rc


def cmd_courier(args) -> int:
    """Record a coordinated change spanning repos; emit a GitHub-URL summary.

    The summary links repos by GitHub URL (never local path) so it is portable
    to a teammate who has only some repos cloned — the same rule the
    integrated-architecture README enforces for cross-repo doc links.
    """
    mpath, repos = load_repos(args.manifest)
    repos = select(repos, args.only)
    entries = []
    for r in repos:
        if not r.is_git():
            continue
        entries.append(
            {
                "repo": r.name,
                "url": r.url,
                "branch": current_branch(r),
                "head": head_short(r),
                "dirty": is_dirty(r),
            }
        )
    record = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "intent": args.intent,
        "entries": entries,
    }
    log = default_config_dir() / "courier.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps(record) + "\n")
    if args.json:
        emit(record, True)
        return 0
    print(f"📡 courier: {args.intent}")
    for e in entries:
        loc = e["url"] or e["repo"]
        flag = " (dirty)" if e["dirty"] else ""
        print(f"  {e['repo']}: {e['branch']} @ {e['head']}{flag}")
        if e["url"]:
            br = e["branch"]
            if br not in ("(detached)",):
                print(f"    {e['url'].removesuffix('.git')}/tree/{br}")
    print(f"\nrecorded → {log}")
    return 0


# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="beetle", description=__doc__.splitlines()[0])
    p.add_argument("--manifest", help="explicit manifest path (overrides BEETLE_REPOS)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp, *, only=True, js=True):
        if only:
            sp.add_argument("--only", help="comma-separated repo names to act on")
        if js:
            sp.add_argument("--json", action="store_true", help="machine-readable output")

    sp = sub.add_parser("init", help="scaffold a manifest from the bundled example")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("list", help="show resolved repos")
    add_common(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("doctor", help="validate each repo (exists, git, its remote (default origin) matches url)")
    add_common(sp)
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("status", help="branch/dirty/ahead-behind across repos")
    add_common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("sync", help="fetch --all --prune (optionally --pull ff-only)")
    add_common(sp)
    sp.add_argument("--pull", action="store_true", help="also fast-forward merge upstream")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("branch", help="create/checkout a matching branch across repos")
    sp.add_argument("name")
    sp.add_argument("--base", help="base ref for new branches")
    add_common(sp)
    sp.set_defaults(func=cmd_branch)

    sp = sub.add_parser("run", help="fan out an arbitrary git command: beetle run -- log --oneline -1")
    add_common(sp)
    sp.add_argument("gitargs", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("courier", help="record a coordinated cross-repo change; emit GitHub-URL summary")
    sp.add_argument("intent", help="one-line description of the coordinated change")
    add_common(sp)
    sp.set_defaults(func=cmd_courier)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # strip a leading "--" left in REMAINDER
    if getattr(args, "gitargs", None) and args.gitargs and args.gitargs[0] == "--":
        args.gitargs = args.gitargs[1:]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
