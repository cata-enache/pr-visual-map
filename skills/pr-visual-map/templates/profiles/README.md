# Rule packs

All stack knowledge of `make_map.py` lives in these JSON files. The generator code knows nothing about .NET,
Angular or Terraform; it applies whatever the active packs say.

## Which packs load

1. `generic.json`: always. Languages (symbol patterns), generic file kinds, generated-file detection, secrets.
2. Every other `*.json` here whose `detect` regexes match at least one file of the head tree (or the PR).
   No `detect` = always.
3. Overlays, always: `repos/<owner>.json`, then `repos/<owner>__<repo>.json`.
4. `.pr-visual-map.json` in the reviewed repo, read at the **base** commit (a PR cannot relax its own rules).

Steps 2 and 3 also read a private folder, `$PR_VISUAL_MAP_PROFILES` (default `~/.config/pr-visual-map/profiles`),
for packs and overlays you keep out of git (internal knowledge bases, ticket systems). They show as `user:...`.
`examples/org-overlay.example.json` shows a complete overlay; files in `examples/` never load.

Later packs add to earlier ones. The summary line `rule packs: ...` and the page's Checks panel show what loaded.

## Format (every key optional)

| Key | Meaning |
|---|---|
| `name`, `description` | Shown in the summary, README and brief. |
| `detect` | Regexes on repo-relative paths. The pack loads if any file matches. |
| `extensions.code` / `.ui` / `.doc` | Extension sets used by `classify` (`"code": true`, `"ext": "@ui"`). |
| `generated` | `path` regexes, `suffix` list, lock-file `names`, `content` regex on the first added lines. |
| `projectMarkers` | Regexes for files that mark a project root (`.csproj`, `package.json`, ...). |
| `stripDirs` | Folder names skipped when naming features (`src`, `app`, ...). |
| `families` | Per language: `ext`, `types` (regex with named groups `kw`, `name`; multiline), `members` (regex with `name`, matched at line start), `skip`, `skipPrivate`, `callsLookLikeMembers`. |
| `classify` | Ordered rules; the first match wins. See below. |
| `flags` | Line rules that raise risk flags. See below. |
| `routes` | Server routes for client→server links: `families`, `method` regex (first non-empty group = route template), optional `classPrefix` (groups: prefix, class name), `controllerToken`, `controllerSuffix`. |
| `clientUrls.families` | Languages whose string literals are matched against server routes. |
| `config.setters` | `{files, re}`: lines that set a config section (group 1 = section), e.g. Terraform `Section__Key =`. |
| `config.readers` | `{families, re}`: code that reads a section (any group = section name). |
| `context.docs` | Regexes for repo docs listed in the agent brief. `context.projectDocs`: doc names next to touched projects. |
| `context.sources` | External sources for the agent, e.g. a knowledge base: `name`, `path`, `readFirst`, `repoFolders`, `ticketFolders`, `search`, `rules`. The generator resolves which folders match this repo and ticket. |
| `context.output` | Suggested output folder when a ticket folder is found (`whenTicketFolder`, `path` with `{source}`, `{folder}`, `{repoName}`, `{pr}`). |
| `tickets` | `{re, url}`: ticket id pattern (group 1) and optional URL template with `{id}`. Overlay patterns win over generic ones. |

### `classify` rules

`{"order": 60, "kind": "endpoint", "path": "Controller\\.cs$"}`

- `kind`: `endpoint`, `ui`, `handler`, `dto`, `entity`, `migration`, `config`, `infra`, `test`, `unmapped`.
- Conditions (all must hold): `path` regex on the repo path, `name` regex on the file name, `ext` list (or `"@ui"`),
  `code` true/false (extension in the code set), `ui: false`, `generated` true/false.
- `markGenerated: true` marks the file as generated.
- Order bands used by `generic.json`: 10 lock files · 20 tests · 30 migrations · 40 infra · 50 config ·
  55 scripts · 60 endpoints · 70 UI · 80 generated code · 82 entities · 84 DTOs · 90 other code.
  Put stack rules in the same band; equal orders keep load order.

### `flags` rules

```json
{"type": "auth", "re": "\\[Authorize\\b", "on": "+-",
 "severity": {"+": "medium", "-": "high"},
 "reason": {"+": "Auth rule added: {line}", "-": "Auth rule removed or changed: {line}"}}
```

- `on`: which diff lines to scan, `+`, `-` or `+-`. Test files are never scanned.
- `severity`: `high`, `medium`, `low`, or per side. `escalate`: `{group, re, to}` raises it when a captured group matches.
- `reason`: text or per side; placeholders `{line}`, `{0}`..`{n}` (regex groups), `{verb}` (added/removed).
- Filters: `files` (path regex), `kinds`, `notKinds`, `codeOnly`, `skipGenerated`, `safe` (`{group, prefixes}`:
  skip when the captured value starts with a safe prefix), `ignore` (`{group, values}`).
- Never print secret values in a reason; refer to the key name only.

## After a change

Re-run a few existing maps and compare `graph.json` blocks, flags and must-read before and after
(see SKILL.md, "When a map is wrong"). Every difference should be intended.
