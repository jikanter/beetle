# Beetle Git Coordination Skill

Named after the Beetle probes in *Project Hail Mary*: small couriers that carry
messages between distant places. **beetle** carries git operations across the
several repos that make up one integrated system

The purpose of this [skill](https://agentskills.io) is to help local agents coordinate commits across multiple
git repositories. It is the sister skill of agent meta-repositories, in which agent meta-repositories are used
to lookup conventions, playbooks, and standards for agents across repositories, this skill is used to coordinate
the local agent's work across checkouts.


## Installation 

Place the `beetle-git-coordination` folder in your agent's `skills` directory and reload your skills

### Claude Code 
```bash
cp -r beetle-git-coordination ~/.claude/skills
```
Then open claude code and type `/reload-skills` 

### Other Agent harnesses (pi, opencode, copilot)
```bash
cp -r beetle-git-coordination ~/.agents/skills
```
Then open your harness and reload skills

### Usage

1. Set up and place your `repos.json` file according to your needs. See the sample file `beetle-git-coordination/repos.example.json` for an example.
2. Open your harness in one repo and type `/beetle-git-coordination courier the last commit`
3. Ensure that you can run the above script from all relevant repo directories and that the repos can see one another's changes.

That's it! You can now use the `beetle-git-coordination` skill to coordinate across repositories.

See [beetle-git-coordination/SKILL.md](beetle-git-coordination/SKILL.md) for more.
