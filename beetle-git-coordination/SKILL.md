---
name: beetle-git-coordination
description: Coordinate git operations across the multiple repositories of one integrated system (status, sync, matching branches, fan-out commands, coordinated-change records) without hardcoding host paths. Repo set comes from a machine-local JSON manifest resolved via $BEETLE_REPOS / XDG, never committed to any repo.
trigger: /beetle
version: 1.1.0
---

# /beetle — cross-repo git courier

Named after the Beetle probes in *Project Hail Mary*: small couriers that carry
messages between distant places. **beetle** carries git operations across the
several repos that make up one integrated system — e.g. `aichat` ↔
`llm-functions` ↔ `astrophage` ↔ harness — so a change that spans repos is one
command instead of N manual `cd`s.

## Core rule: zero hardcoded paths

The repo set lives in a **machine-local JSON manifest** that sits **outside every
repo**. This is the same portability rule aichat's
`docs/architecture/integrated-architecture/SPEC-mcp-json-artifact.md` applies to
`mcp.json`: the spec is shareable, the host-specific values are not.

Manifest resolution (first hit wins):
1. `$BEETLE_REPOS` — **set this in `.claude/settings.local.json`** (gitignored) to relocate per machine.
2. `$XDG_CONFIG_HOME/beetle/repos.json`
3. `~/.config/beetle/repos.json`

The Agent Skills spec has **no native machine-local config layer** — skills are
portable content. So host values go in the manifest (out-of-repo) and the
per-machine override goes in `settings.local.json`'s `env` block. SKILL.md and
the engine stay path-free.

## Setup (once per machine)

```bash
python3.13 scripts/beetle.py init      # scaffolds manifest from repos.example.json
# edit the manifest: name, path (~ and $ENV ok), url (browsable forge URL), role,
#   remote (optional, default origin: the git remote url is checked against)
python3.13 scripts/beetle.py doctor    # validate: exists, is git, remote matches url
```

To relocate the manifest on a given machine, add to `.claude/settings.local.json`:
```json
{ "env": { "BEETLE_REPOS": "/abs/path/to/repos.json" } }
```

## Commands

All commands accept `--only name1,name2` (subset of repos) and `--json`
(machine-readable). `--manifest PATH` overrides resolution entirely.

```bash
beetle list                       # resolved repos + roles + urls
beetle doctor                     # health: path exists, is git, <remote> == manifest url
beetle status                     # branch / dirty / ahead-behind, per repo
beetle sync                       # fetch --all --prune everywhere
beetle sync --pull                # also fast-forward upstream (skips dirty trees)
beetle branch feature/x           # create-or-checkout matching branch in every repo
beetle branch feature/x --base main
beetle run -- log --oneline -1    # fan out ANY git command across repos
beetle courier "wire astrophage into eridian cache path"   # record a coordinated change
```

Invoke via the bundled engine: `python3.13 <skill-dir>/scripts/beetle.py <cmd>`.
(Optionally symlink it onto PATH as `beetle`.)

### courier — the namesake

`beetle courier "<intent>"` snapshots each repo's branch + HEAD, appends a JSONL
record to `$XDG_CONFIG_HOME/beetle/courier.log`, and prints a summary that links
each repo **by its forge URL** (e.g. `<url>/tree/<branch>`; GitHub, or a Gitea
sandbox, which redirects `/tree/` to `/src/`), never by local
path. That portability is deliberate: the summary can be pasted to a teammate
who has only some repos cloned — the same cross-repo-link rule the
integrated-architecture README enforces for docs. Use it to mark the moment a
multi-repo change is staged across all of them.

## When to use this skill

- A change touches more than one repo in the integrated system (the common case
  for integrated-architecture work, or forking `astrophage` out of `eridian`).
- You need a single status read across all repos before starting work.
- You want matching feature branches created everywhere at once.
- You want a portable record of a coordinated cross-repo change.

For single-repo work, plain `git` is the one-tool-per-job answer — don't reach
for beetle.

## Files

| File | Purpose |
|---|---|
| `scripts/beetle.py` | Engine. Stdlib-only (python3.11+), no PyYAML/yq. |
| `scripts/test_beetle.py` | Self-contained tests — temp repos, temp manifest, no network. `python3.13 scripts/test_beetle.py`. |
| `repos.example.json` | Manifest template `init` copies out. |

## Constraints honored

- **No hardcoded paths** in the skill or any repo — manifest is out-of-repo, override is `settings.local.json`.
- **Zero new dependencies** — Python stdlib only; JSON keeps parsing out of PyYAML/yq.
- **One tool per job** — beetle only fans out across repos; it does not wrap single-repo git.
- **Token/cost conscious** — pure local git plumbing, no model calls.
