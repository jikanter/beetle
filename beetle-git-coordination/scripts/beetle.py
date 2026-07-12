#!/usr/bin/env python3.13
"""beetle — cross-repository git coordinator.

Named after the Beetle probes in *Project Hail Mary*: small couriers that carry
messages between distant places. This tool carries git operations across the
several repositories that make up one integrated system (e.g. `aichat` ↔
`llm-functions` ↔ `brief` ↔ harness) without ever hardcoding host paths.

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
import yaml

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
    meta_url: str | None = None

    @property
    def git_dir(self) -> Path:
        return self.path / ".git"

    def exists(self) -> bool:
        return self.path.is_dir()

    def is_git(self) -> bool:
        return self.git_dir.exists()

def load_repos(explicit: str | None = None) -> tuple[Path, list[Repo]]:
    data = None
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
        repo_data = {
            "name": entry["name"],
            "path": _expand(entry["path"]),
            "url": entry.get("url"),
            "role": entry.get("role"),
            "meta_url": entry.get("meta_url"),
        }
        repos.append(Repo(**repo_data))
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
    r = git(repo, "remote", "get-url", "origin")
    return r.stdout.strip() if r.returncode == 0 else None

def fetch_meta_data(repo: Repo) -> dict | None:
    """Fetches data from a configured meta repository URL."""
    if not repo.meta_url:
        return None
    print(f"  > Fetching meta data for '{repo.name}' from {repo.meta_url}...")
    try:
        import httpx
        response = httpx.get(repo.meta_url, timeout=10)
        response.raise_for_status()
        # Assuming JSON response from meta endpoint
        return response.json()
    except Exception as e:
        print(f"  > Warning: Failed to fetch meta data for {repo.name}: {e}", file=sys.stderr)
        return None

def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"beetle: {msg}", file=sys.stderr)
    sys.exit(code)

def emit(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2))

# --- New Context Scanning Functions ---

def scan_repo_for_context(repo: Repo) -> dict:
    """Scans a single repo for context files (AGENTS.md, README.md, repos.yaml)."""
    context = {}
    files_to_find = ["AGENTS.md", "README.md", "repos.yaml"]

    for file_name in files_to_find:
        path = repo.path / file_name
        if path.exists():
            try:
                content = path.read_text()
                context[file_name] = content
                if file_name == "repos.yaml":
                    try:
                        # Attempt to parse YAML
                        context[f"{file_name}_parsed"] = yaml.safe_load(content)
                    except yaml.YAMLError as e:
                        context[f"{file_name}_parsed_error"] = str(e)
            except Exception as e:
                context[f"{file_name}_error"] = str(e)
        else:
            context[file_name] = f"File not found."
    return context

# --- Subcommands ---

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
                    issues.append(f"origin={actual} != manifest url")
                elif actual is None:
                    issues.append("no origin remote")
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
    """Loose compare: ignore .git suffix and scp-vs-https shape."""
    def norm(u: str) -> str:
        u = u.strip().removesuffix(".git")
        u = u.replace("git@github.com:", "github.com/")
        u = u.replace("https://", "").replace("http://", "")
        return u.rstrip("/")
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
                "head": head_short(
                    r
                )
            }
        )
    return 0

def cmd_scan(args) -> int:
    mpath, repos = load_repos(args.manifest)
    if args.json:
        # Logic for JSON output of all contexts
        results = []
        for r in repos:
            scan_results = scan_repo_for_context(r)
            results.append({"name": r.name, "context": scan_results})
        emit(results, True)
    else:
        print("--- Starting Local Context Scan ---")
        for r in repos:
            print(f"\n[--- {r.name} ---]")
            context = scan_repo_for_context(r)
            for file_name, value in context.items():
                print(f"\n--- {file_name} ---")
                if isinstance(value, dict):
                    print(json.dumps(value, indent=2))
                else:
                    print(value)
    return 0

def main():
    parser = argparse.ArgumentParser(description="Beetle Git Coordinator.")
    parser.add_argument("-m", "--manifest", default=None, help="Path to the repo manifest.")
    parser.add_argument("--only", type=str, default=None, help="Only target specific repositories.")
    parser.add_argument("-j", "--json", action="store_true", help="Output in JSON format.")
    parser.add_argument("-f", "--force", action="store_true", help="Force initialization even if manifest exists.")
    subparsers = parser.add_subparsers(dest="command")

    # Init command
    parser_init = subparsers.add_parser("init", help="Scaffold a new manifest.")
    parser_init.add_argument("-f", "--force", action="store_true")

    # List command
    parser_list = subparsers.add_parser("list", help="List all repositories.")

    # Doctor command
    parser_doctor = subparsers.add_parser("doctor", help="Check the health of the repositories.")

    # Status command
    parser_status = subparsers.add_parser("status", help="Show branch status of repositories.")

    # Scan command (New)
    parser_scan = subparsers.add_parser("scan", help="Scan repositories for context files.")

    args = parser.parse_args()

    if args.command == "init":
        return cmd_init(args)
    elif args.command == "list":
        return cmd_list(args)
    elif args.command == "doctor":
        return cmd_doctor(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "scan":
        return cmd_scan(args)

    return 0

if __name__ == "__main__":
    main()
