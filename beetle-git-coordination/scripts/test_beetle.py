#!/usr/bin/env python3.13
"""Self-contained tests for beetle. No network, no real repos.

Run:  python3.13 scripts/test_beetle.py
Creates throwaway git repos in a temp dir, points a temp manifest at them,
exercises resolution + each read/write subcommand, asserts behavior.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import beetle  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def make_repo(root: Path, name: str) -> Path:
    p = root / name
    p.mkdir()
    git(p, "init", "-q")
    git(p, "config", "user.email", "t@t.t")
    git(p, "config", "user.name", "t")
    git(p, "config", "commit.gpgsign", "false")
    (p / "README.md").write_text(f"# {name}\n")
    git(p, "add", ".")
    git(p, "commit", "-q", "-m", "init")
    return p


def run(*argv: str) -> int:
    """Invoke beetle.main, returning its exit code (suppress SystemExit)."""
    try:
        return beetle.main(list(argv))
    except SystemExit as e:
        return int(e.code or 0)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        a = make_repo(root, "alpha")
        b = make_repo(root, "beta")
        # a non-git dir to exercise doctor failure path
        (root / "gamma").mkdir()

        manifest = root / "repos.json"
        manifest.write_text(
            json.dumps(
                {
                    "repos": [
                        {"name": "alpha", "path": str(a), "role": "runtime"},
                        {"name": "beta", "path": str(b), "role": "tools"},
                        {"name": "gamma", "path": str(root / "gamma")},
                    ]
                }
            )
        )

        # --- resolution: BEETLE_REPOS wins ---
        os.environ["BEETLE_REPOS"] = str(manifest)
        mp, repos = beetle.load_repos()
        check("BEETLE_REPOS resolves manifest", mp == manifest)
        check("loads all repos", len(repos) == 3, f"got {len(repos)}")
        check("path ~/$ expansion is a Path", all(isinstance(r.path, Path) for r in repos))

        # --- select / --only ---
        check("select all when no --only", len(beetle.select(repos, None)) == 3)
        check("select subset", [r.name for r in beetle.select(repos, "beta")] == ["beta"])
        bad = run("status", "--only", "nope")
        check("--only unknown name errors", bad == 1)

        # --- doctor: gamma is not a git repo -> nonzero ---
        rc = run("doctor")
        check("doctor flags non-git repo", rc == 1, f"rc={rc}")
        rc_ok = run("doctor", "--only", "alpha,beta")
        check("doctor passes for valid repos", rc_ok == 0, f"rc={rc_ok}")

        # --- status json shape ---
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run("status", "--only", "alpha,beta", "--json")
        data = json.loads(buf.getvalue())
        check("status --json has 2 entries", len(data["status"]) == 2)
        check("status reports clean tree", data["status"][0]["dirty"] is False)

        # dirty detection
        (a / "scratch.txt").write_text("x")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run("status", "--only", "alpha", "--json")
        check("status detects dirty tree", json.loads(buf.getvalue())["status"][0]["dirty"] is True)
        (a / "scratch.txt").unlink()

        # --- branch: create across repos, then checkout existing ---
        rc = run("branch", "feature/x", "--only", "alpha,beta")
        check("branch create rc=0", rc == 0)
        check("alpha on feature/x", beetle.current_branch(beetle.Repo("alpha", a)) == "feature/x")
        check("beta on feature/x", beetle.current_branch(beetle.Repo("beta", b)) == "feature/x")
        rc2 = run("branch", "feature/x", "--only", "alpha")  # already exists -> checkout
        check("branch re-checkout rc=0", rc2 == 0)

        # --- run fan-out ---
        rc = run("run", "--only", "alpha", "--", "rev-parse", "--abbrev-ref", "HEAD")
        check("run fan-out rc=0", rc == 0)

        # --- courier: writes a record, links by url when present ---
        cfg = root / "cfg"
        os.environ["XDG_CONFIG_HOME"] = str(cfg)
        rc = run("courier", "coordinated test change", "--only", "alpha,beta")
        check("courier rc=0", rc == 0)
        log = cfg / "beetle" / "courier.log"
        check("courier writes log", log.exists())
        if log.exists():
            rec = json.loads(log.read_text().splitlines()[-1])
            check("courier record has intent", rec["intent"] == "coordinated test change")
            check("courier record has 2 entries", len(rec["entries"]) == 2)

        # --- init: scaffolds from example ---
        dest = root / "scaffold" / "repos.json"
        os.environ["BEETLE_REPOS"] = str(dest)
        rc = run("init")
        check("init rc=0", rc == 0)
        check("init created manifest", dest.exists())
        if dest.exists():
            check("scaffold is valid json", "repos" in json.loads(dest.read_text()))
        check("init refuses overwrite", run("init") == 1)

        # --- missing manifest path ---
        os.environ["BEETLE_REPOS"] = str(root / "does-not-exist.json")
        check("missing manifest errors", run("status") == 1)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
