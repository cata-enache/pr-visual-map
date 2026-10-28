---
name: pr-visual-map
description: Turn a pull request (or a local branch vs its base) into a small, clickable, offline map of what changed, what it touches and where the real risk is, so the reviewer sees the picture first and reads code only where it matters. Works for any git repository; stack knowledge comes from rule packs, the agent's judgement is checked by the generator. Output is graph.json plus one self-contained index.html. USE FOR "map this PR", "show me what this PR changes visually", "review this PR as a picture", "where should I look in this PR", orienting on a big or unfamiliar PR before reading the diff. DO NOT USE FOR dashboards or posting review comments.
metadata:
  audience: agents
---

# PR visual map

Coding agents produce changes faster than people can read them. Reading a PR line by line, as words, is the most expensive way to understand it. This skill gives the reviewer the **picture first**: a sparse map of the change with drill-down. They read code only where the map says it matters.

**The map is a view and a guide. The code is the truth.** Every block points to files and lines. Anything concluded rather than read is labelled "inferred".

## How it works: three layers

1. **Generator** (`templates/make_map.py`, standard-library Python, no LLM). Reads the diff and the head tree, groups files into blocks, finds links and the blast radius, applies rule flags, runs self-checks, renders the page. Deterministic.
2. **Rule packs** (`templates/profiles/*.json`). All stack knowledge lives here, not in code: which files are endpoints, tests, migrations, config; what counts as an auth change, a public route, a secret; how routes and config keys link across files; where docs and knowledge bases are. Packs are picked by what the repo contains. Private packs (house rules, internal knowledge bases) go in `$PR_VISUAL_MAP_PROFILES` (default `~/.config/pr-visual-map/profiles`), outside git. See `templates/profiles/README.md`.
3. **Agent part** (you). Names, one-line summaries, the why, extra risks, questions and the review path go into `enrich.json`. The generator writes your task file (`agent-brief.md`) and refuses to publish the page until your part passes checks A0–A10.

Other files: `templates/page.html` (page template), `templates/enrich.schema.json` (format of `enrich.json`), `templates/check_page.mjs` (scripted click-through, Node 22+), `templates/regress.py` (re-runs every saved map and lists differences).

## Working agreement

- **Keep every message short and in plain English.** Many reviewers read English as a second language and review many PRs a day. Point to a file or screenshot instead of describing a long result.
- Ask the user **once**, at the start, with recommended defaults.
- **Read-only on the repo.** No commits, no pushes, no PR comments or reviews, however they would be posted.
- **Code never leaves the machine** beyond fetching the PR. No external renderers, CDNs, fonts or analytics. The page makes zero network requests.
- No secrets, tokens or customer identifiers in any output.

## The procedure (the same for every repo and PR)

Let `MAP=<folder of this SKILL.md>/templates/make_map.py` and `OUT=${PR_VISUAL_MAP_OUT:-~/pr-maps}`.

### 1. Agree once

One question with recommended defaults: **which PR** (number/URL, or "current branch vs base"; if none given, list `gh pr list --author @me --limit 10` and recommend one) and **output folder** (default `$OUT/<repo>-pr-<n>/`, never inside a tracked path of the reviewed repo). If the PR is already known, run step 2 first: when `agent-brief.md` shows a "Suggested output folder" (from an overlay, e.g. a ticket's folder in a knowledge base), offer it as the default and re-run there.

### 2. Draft run (facts only)

```
python3 $MAP --repo OWNER/NAME --pr N --out <folder>
python3 $MAP --local <clone> --base <base> [--head HEAD] --out <folder>
```
Always run the skill's own generator (`$MAP`), not the snapshot copied into the output folder. It prints a summary (quote it, don't retype it), the rule packs it used and `agent part: MISSING`. It wrote `graph.json`, a draft `index.html`, `agent-brief.md`, `enrich.skeleton.json` and `README.md`. If it says **self-check FAILED**: stop and report the reason. Never edit numbers or outputs to make a check pass.

### 3. Agent part: follow `agent-brief.md`

The brief is your task file. It holds the failing checks with fixes, the rules for what you write, the PR at a glance (commits, linked issues, description), the **context sources** resolved for this repo (repo docs, project docs, knowledge bases from overlays, ticket folders) and every block with its files, flags, links and the hunk ranges to read.
1. Read the context sources it lists before writing any "why". Follow each source's own rules (for example a knowledge base's `AGENTS.md`). When a source and the code disagree, the code wins: say so in `readme`.
2. Read the code of the must-read candidates and every high flag. Decide the real must-read blocks (2–4) yourself.
3. Write `<folder>/enrich.json` (start from `enrich.skeleton.json`; format `enrich.schema.json`). Copying the skeleton's `mustRead`/`reviewOrder` counts as your decision; change them where you disagree. Cite `path:line` at the PR head; for a deleted file, the line in the old file. Risks you add carry `source`: `extracted` if you read it in the code, `inferred` if you concluded it.
4. Re-run the same command. Repeat until it prints `agent part: PASSED`. Use `--final` for the last run: it fails if the agent part is missing.

Never edit `graph.json` or `index.html` by hand, and never change a rule pack just to make your part pass.

### 4. Look, then click

- Screenshots into a temporary folder (deleted afterwards), with headless Chrome or Chromium (`$CHROME`): 1440×900 and 900×900, each with nothing and with a must-read block selected; one with `diff=1` (first file of the selection open); one per theme (`theme=dark` and `theme=light`; without the parameter the page follows the system theme, and headless Chrome is light). Add `open=<groupId>` only when the map has groups (`graph.json` nodes with `"kind": "group"`).
  `"$CHROME" --headless=new --disable-gpu --hide-scrollbars --window-size=1440,900 --screenshot=<tmp>/x.png "file://<folder>/index.html?theme=dark&sel=<id>&diff=1"`
- **Open every screenshot.** Overlaps, clipped or unclear labels: fix labels in `enrich.json`. Layout bugs: fix `page.html` in the skill, then re-run.
- `node <skill>/templates/check_page.mjs <folder>/index.html <blockId> [<groupId>]` must print only ✓ (pass a top-level block id; add a group id only if there are groups).

### 5. Store and report

The folder holds `graph.json`, `index.html`, `README.md` (summary, checks, rule packs, deviations, how to regenerate), `enrich.json`, `agent-brief.md` and snapshots of the generator, page template and packs. Report briefly: the path to open, the 3 places that need real attention, self-check and agent-part status, and anything surprising (for example a deleted file still referenced). Never call the PR safe.

## When a map is wrong: extend the packs

- File in the wrong kind, a missed route or auth pattern, a new stack: edit or add a pack in `templates/profiles/` (format in `profiles/README.md`). Stack packs declare `detect` regexes and load only when the repo matches.
- House rules for an organisation or one repo: `repos/<owner>.json` or `repos/<owner>__<repo>.json`, in `templates/profiles/` if they can be public, otherwise in the private user folder. A repo can also ship `.pr-visual-map.json`; it is read at the **base** commit so a PR cannot relax its own rules.
- Before changing a pack or the generator: `python3 templates/regress.py save`. After: `python3 templates/regress.py check` re-runs every map in `$OUT` and lists what changed per block (kind, files, rule flags, must-read, step) and in the links. Explain every difference; a map whose `enrich.json` no longer fits fails loudly and needs its agent part updated.

## Reference

- **Self-checks** (generator, deterministic): 1 every PR file in exactly one block · 2 line totals match GitHub · 3 every block, edge and flag has evidence with a real line · 4 no file in two blocks · 5 every affected block references a changed symbol · 6 no network references in the page · 7 labels ≤ 3 words · 8 one to four must-read blocks.
- **Agent checks**: A0 `enrich.json` well-formed · A1 headline · A2 PR-level why with a source · A3 summaries for must-read, flagged and top-level-group blocks (all blocks when ≤ 12) · A4 2–4 must-read, each with a why and a risk or question · A5 must-read first in the review path · A6 cited lines exist · A7 labels ≤ 3 words and unique · A8 1–5 PR questions with evidence · A9 summaries cite their own block · A10 no placeholders.
- **Page**: layers Entry → Logic → Data → Infra, tests lane; ≤ 12 blocks per level (nested groups: project, folder, layer, then alphabetical ranges); over 24 links on a level are drawn only for the selected block; the page uses the full window width and shows details below the map in two columns (header button or `layout=side` for a wide side panel; the choice is remembered); URL params `sel`, `open`, `theme`, `layout`, `diff`; "reviewed" marks are stored per PR head commit.
- **Fetching**: PR data cached in `<folder>/.raw/` (`--refresh` re-fetches). The head is read in a worktree under `~/.cache/pr-visual-map/` (never the user's clone). Over 300 files, the diff is computed locally and noted under Deviations.
- **Validated on** public PRs: fastapi/full-stack-fastapi-template #2421 and #2438, go-chi/chi #1214 (a fresh agent with only this skill produced a passing map for #2421).
