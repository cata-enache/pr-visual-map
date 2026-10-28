# visual-pr-review

See a pull request as a picture before you read it line by line.

`pr-visual-map` is an agent skill. It turns a pull request (or a local branch) into one offline HTML page: a small,
clickable map of what changed, what it touches and where the real risk is. You read code only where the map says it
matters. Every block points to files and lines; anything an agent concluded rather than read is labelled "inferred".

- **Picture first:** at most 12 blocks per level, laid out Entry → Logic → Data → Infra with a tests lane; groups open on click.
- **Risk where it is:** rule flags (auth, public routes, secrets, IAM, migrations, large edits, untested code), the blast radius
  (unchanged code that calls what changed) and a numbered review path with 2–4 must-read blocks.
- **Drill down:** block → files → diff hunks, with "Mark reviewed" and "Copy question for agent".
- **Offline and honest:** one self-contained file, zero network requests, self-checks that refuse to publish a wrong map.

## How it works

| Layer | What | Where |
|---|---|---|
| Generator | Deterministic, standard-library Python. Diff → blocks, links, blast radius, rule flags, self-checks, page. | `skills/pr-visual-map/templates/make_map.py` |
| Rule packs | All stack knowledge as JSON, picked by what the repo contains: generic, .NET, TypeScript, Python, Go, JVM, Terraform, GitHub Actions. | `skills/pr-visual-map/templates/profiles/` |
| Agent part | The agent reads `agent-brief.md`, writes `enrich.json` (names, summaries, why, risks, questions, review path). The generator checks it (A0–A10) before publishing. | `skills/pr-visual-map/SKILL.md` |

## Install

Requirements: Python 3.10+, git, the [GitHub CLI](https://cli.github.com/) (`gh auth login`) for PR mode, and for the
optional checks Chrome or Chromium plus Node.js 22+.

Link the skill into your agent's skills folder, for example:

```sh
git clone https://github.com/cata-enache/visual-pr-review.git
ln -s "$PWD/visual-pr-review/skills/pr-visual-map" ~/.copilot/skills/pr-visual-map   # GitHub Copilot CLI
ln -s "$PWD/visual-pr-review/skills/pr-visual-map" ~/.claude/skills/pr-visual-map    # Claude Code
```

Then ask your agent: *"Use the pr-visual-map skill on https://github.com/OWNER/REPO/pull/123."*

## Run the generator yourself

```sh
python3 skills/pr-visual-map/templates/make_map.py --repo OWNER/REPO --pr 123 --out ~/pr-maps/REPO-pr-123
open ~/pr-maps/REPO-pr-123/index.html
```

Without an agent you get a draft: files, links, rule flags and a default review path. The agent part
(`enrich.json`, described in the generated `agent-brief.md`) adds the summaries, the why and the questions.

| Setting | Default | Purpose |
|---|---|---|
| `PR_VISUAL_MAP_OUT` | `~/pr-maps` | Where maps go (used by the skill and `regress.py`). |
| `PR_VISUAL_MAP_PROFILES` | `~/.config/pr-visual-map/profiles` | Private rule packs and overlays kept out of git. |
| `CHROME` | auto-detected | Chrome or Chromium for screenshots and `check_page.mjs`. |

## Extend it

Add or fix a rule pack in `templates/profiles/` (format: `templates/profiles/README.md`), or keep house rules
private in `PR_VISUAL_MAP_PROFILES`. `templates/regress.py save` / `check` re-runs your existing maps and lists every
difference, so pack changes stay deliberate.

## Roadmap

1. **Now:** one PR → one map (this skill).
2. **Solution map:** the same `graph.json` for a whole repository or a set of services.
3. **Live canvas:** drill-down, diffs and an agent chat per block.
4. **Visual editing:** change the picture, get a change proposal.

## Licence

MIT, see [LICENSE](LICENSE).
