#!/usr/bin/env python3
"""pr-visual-map generator (Phase 0 of visual-review).

Turns a pull request (or a local branch vs its base) into:
  graph.json   facts + blocks + edges + risk flags + review path (schema visual-review/graph@1)
  index.html   one self-contained page (vanilla JS + SVG, graph embedded, zero network requests)
  README.md    what it shows, how it was checked, deviations, how to regenerate

Usage:
  make_map.py --repo OWNER/NAME --pr 268 --out DIR [--enrich DIR/enrich.json] [--refresh]
  make_map.py --local PATH --base develop [--head HEAD] --out DIR [--enrich FILE]

Deterministic facts come first (files, hunks, blocks, edges, blast radius, rule-based risk flags).
The optional enrich.json adds the LLM part (labels, one-line summaries, why, review path,
questions); every enrich statement carries evidence and a source. A skeleton is written to
DIR/enrich.skeleton.json on every run so the ids are known.

Standard library only. Read-only on the reviewed repository: PR mode works in a cache clone
under ~/.cache/pr-visual-map/, local mode only reads git objects.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

GENERATOR = "pr-visual-map@0.1"
SCHEMA = "visual-review/graph@1"
CACHE_ROOT = Path.home() / ".cache" / "pr-visual-map"
HERE = Path(__file__).resolve().parent

MAX_TOP_LEVEL = 12
MAX_BLOCK_FILES = 8
LARGE_HUNK = 100
LARGE_NEW_FILE = 250
MAX_LINES_PER_FILE = 600
MAX_LINES_TOTAL = 30000
MAX_AFFECTED_FILES = 16
MAX_FILES_PER_SYMBOL = 6
MAX_CALLER_SYMBOLS = 8
TYPE_KWS = {"class", "interface", "record", "struct", "enum", "type"}
TOO_COMMON_HITS = 60

LAYER_OF = {"endpoint": "entry", "ui": "entry", "handler": "logic", "dto": "logic",
            "entity": "data", "migration": "data", "config": "infra", "infra": "infra",
            "test": "tests", "unmapped": "infra"}
KIND_ORDER = ["endpoint", "migration", "infra", "entity", "handler", "dto", "ui", "config", "test", "unmapped"]
KIND_NOUN = {"endpoint": "endpoint", "ui": "UI", "handler": "logic", "dto": "contracts", "entity": "domain",
             "migration": "migrations", "config": "config", "infra": "infra", "test": "tests",
             "unmapped": "other files"}
LAYER_ORDER = ["entry", "logic", "data", "infra", "tests"]
SEVERITY_WEIGHT = {"high": 5, "medium": 2, "low": 1}


def die(msg: str) -> None:
    raise SystemExit(f"pr-visual-map: {msg}")


def run(cmd, cwd=None, check=True) -> str:
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        die(f"command failed ({p.returncode}): {' '.join(map(str, cmd))}\n{p.stderr.strip()[:2000]}")
    return p.stdout


def tickets_of(*texts) -> list[dict]:
    """Ticket ids from title, branch and body, using the packs' ticket patterns (most specific first)."""
    found = []
    for t in R.tickets:
        for text in texts:
            for m in t["rx"].finditer(text or ""):
                tid = m.group(1)
                if tid not in [f["id"] for f in found]:
                    found.append({"id": tid, "url": t["url"].format(id=tid) if t.get("url") else ""})
        if found:
            break
    return found


# ----------------------------------------------------------------------------- fetching

def fetch_pr(repo: str, pr: int, raw: Path, refresh: bool) -> dict:
    """PR metadata, per-file numbers (REST, paginated) and the unified diff. Cached in raw/."""
    raw.mkdir(parents=True, exist_ok=True)
    meta_f, files_f, diff_f = raw / "meta.json", raw / "files.json", raw / "pr.diff"
    stale = meta_f.exists() and "commits" not in json.loads(meta_f.read_text(encoding="utf-8"))
    if refresh or stale or not meta_f.exists():
        fields = ("number,title,body,url,state,baseRefName,headRefName,baseRefOid,headRefOid,"
                  "additions,deletions,changedFiles,author,mergedAt,commits,closingIssuesReferences")
        meta_f.write_text(run(["gh", "pr", "view", pr, "-R", repo, "--json", fields]), encoding="utf-8")
    if refresh or not files_f.exists():
        out = run(["gh", "api", "--paginate", f"repos/{repo}/pulls/{pr}/files?per_page=100",
                   "--jq", ".[] | {filename,status,additions,deletions,previous_filename}"])
        files = [json.loads(line) for line in out.splitlines() if line.strip()]
        files_f.write_text(json.dumps(files, indent=1), encoding="utf-8")
    if refresh or not diff_f.exists():
        p = subprocess.run(["gh", "pr", "diff", str(pr), "-R", repo], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0:
            diff_f.write_text(p.stdout, encoding="utf-8")
        else:
            (raw / "diff-fallback.txt").write_text(p.stderr, encoding="utf-8")
            if diff_f.exists():
                diff_f.unlink()
    meta = json.loads(meta_f.read_text(encoding="utf-8"))
    files = json.loads(files_f.read_text(encoding="utf-8"))
    diff = diff_f.read_text(encoding="utf-8") if diff_f.exists() else None
    return {"meta": meta, "files": files, "diff": diff}


def ensure_pr_checkout(repo: str, pr: int, head_oid: str, base_oid: str) -> Path:
    """Cache clone checked out at the PR head (never the user's own clone)."""
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    d = CACHE_ROOT / repo.replace("/", "__")
    if not (d / ".git").exists():
        run(["gh", "repo", "clone", repo, d, "--", "--filter=blob:none", "--no-checkout", "-q"])
    have = subprocess.run(["git", "-C", d, "cat-file", "-e", f"{head_oid}^{{commit}}"],
                          capture_output=True).returncode == 0
    if not have:
        run(["git", "-C", d, "fetch", "-q", "origin", f"pull/{pr}/head"])
    if base_oid and subprocess.run(["git", "-C", d, "cat-file", "-e", f"{base_oid}^{{commit}}"],
                                   capture_output=True).returncode != 0:
        subprocess.run(["git", "-C", d, "fetch", "-q", "origin", base_oid], capture_output=True)
    wt = CACHE_ROOT / f"{repo.replace('/', '__')}@pr{pr}"  # one worktree per PR: parallel runs never collide
    if not (wt / ".git").exists():
        run(["git", "-C", d, "worktree", "prune"])
        run(["git", "-C", d, "worktree", "add", "-q", "--detach", "--force", wt, head_oid])
    else:
        run(["git", "-C", wt, "checkout", "-q", "--detach", "--force", head_oid])
    return wt


def local_files(path: Path, base: str, head: str) -> list[dict]:
    out = run(["git", "-C", path, "diff", "--numstat", "-z", "-M", f"{base}...{head}"])
    parts, files, i = out.split("\0"), [], 0
    while i < len(parts):
        rec = parts[i]
        if not rec.strip():
            i += 1
            continue
        a, d, rest = rec.split("\t", 2)
        if rest == "":  # rename: old and new follow as separate fields
            old, new = parts[i + 1], parts[i + 2]
            i += 3
            files.append({"filename": new, "previous_filename": old, "status": "renamed",
                          "additions": int(a) if a.isdigit() else 0, "deletions": int(d) if d.isdigit() else 0})
            continue
        i += 1
        files.append({"filename": rest, "status": "modified",
                      "additions": int(a) if a.isdigit() else 0, "deletions": int(d) if d.isdigit() else 0})
    st = run(["git", "-C", path, "diff", "--name-status", "-z", "--no-renames", f"{base}...{head}"]).split("\0")
    status = {}
    for k in range(0, len(st) - 1, 2):
        status[st[k + 1]] = {"A": "added", "D": "removed"}.get(st[k][:1], "modified")
    for f in files:
        if f["status"] != "renamed":
            f["status"] = status.get(f["filename"], "modified")
    return files


# ----------------------------------------------------------------------------- diff parsing

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def _unquote(p: str) -> str:
    p = p.rstrip("\t")
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        body = p[1:-1]
        try:
            p = body.encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            p = body
    return p


def _strip_ab(p: str) -> str:
    p = _unquote(p)
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_diff(text: str) -> dict:
    """Unified diff -> {path: {path, oldPath, status, binary, additions, deletions, hunks}}."""
    files, cur, hunk = [], None, None
    rem_old = rem_new = 0
    for raw in text.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if hunk is not None and (rem_old > 0 or rem_new > 0):
            if line.startswith("\\"):
                hunk["lines"].append(line)
                continue
            tag = line[:1]
            if tag == "+":
                rem_new -= 1
                cur["additions"] += 1
            elif tag == "-":
                rem_old -= 1
                cur["deletions"] += 1
            else:
                rem_old -= 1
                rem_new -= 1
                if tag != " ":
                    line = " " + line
            hunk["lines"].append(line)
            continue
        if line.startswith("\\") and hunk is not None:
            hunk["lines"].append(line)
            continue
        if line.startswith("diff --git "):
            rest = line[11:]
            path = old = ""
            n = (len(rest) - 5) // 2
            if n > 0 and rest == f"a/{rest[2:2 + n]} b/{rest[2:2 + n]}":
                path = old = rest[2:2 + n]
            elif " b/" in rest:
                a, b = rest.rsplit(" b/", 1)
                old, path = _strip_ab(a), b
            cur = {"path": path, "oldPath": old, "status": "modified", "binary": False,
                   "additions": 0, "deletions": 0, "hunks": []}
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        m = HUNK_RE.match(line)
        if m:
            hunk = {"header": line, "oldStart": int(m.group(1)), "startLine": int(m.group(3)), "lines": []}
            cur["hunks"].append(hunk)
            rem_old = int(m.group(2)) if m.group(2) is not None else 1
            rem_new = int(m.group(4)) if m.group(4) is not None else 1
            continue
        if line.startswith("new file mode"):
            cur["status"] = "added"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("rename from "):
            cur["oldPath"], cur["status"] = _unquote(line[12:]), "renamed"
        elif line.startswith("rename to "):
            cur["path"] = _unquote(line[10:])
        elif line.startswith("copy to "):
            cur["path"], cur["status"] = _unquote(line[8:]), "added"
        elif line.startswith("Binary files ") or line == "GIT binary patch":
            cur["binary"] = True
        elif line.startswith("--- ") and line[4:] != "/dev/null":
            cur["oldPath"] = _strip_ab(line[4:])
        elif line.startswith("+++ ") and line[4:] != "/dev/null":
            cur["path"] = _strip_ab(line[4:])
    return {f["path"]: f for f in files}


def hunk_line_numbers(h: dict, old: bool = False):
    """Yield (tag, line, text) for one hunk. Line numbers are the new file's; with old=True (deleted files,
    which have no new side) they are the old file's."""
    ln = h["oldStart"] if old else h["startLine"]
    for line in h["lines"]:
        tag = line[:1]
        if tag == "\\":
            continue
        yield tag, max(ln, 1), line[1:]  # a hunk that empties a file starts at new line 0
        if (tag != "+") if old else (tag != "-"):
            ln += 1


# ----------------------------------------------------------------------------- head tree access

class Tree:
    """Read-only view of the head commit: file list, file text, word grep."""

    def __init__(self, git_dir: Path, rev: str, worktree: bool):
        self.git_dir, self.rev, self.worktree = git_dir, rev, worktree
        out = run(["git", "-C", git_dir, "ls-tree", "-r", "--name-only", "-z", rev])
        self.files = [f for f in out.split("\0") if f]
        self.fileset = set(self.files)
        self._cache: dict[str, str | None] = {}

    def read(self, path: str) -> str | None:
        if path in self._cache:
            return self._cache[path]
        txt = None
        if path in self.fileset:
            if self.worktree:
                fp = self.git_dir / path
                if fp.is_file() and fp.stat().st_size < 1_500_000:
                    txt = fp.read_text(encoding="utf-8", errors="replace")
            else:
                p = subprocess.run(["git", "-C", str(self.git_dir), "show", f"{self.rev}:{path}"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace")
                txt = p.stdout if p.returncode == 0 and len(p.stdout) < 1_500_000 else None
        if txt is not None:
            txt = txt.lstrip("\ufeff")
        self._cache[path] = txt
        return txt

    def grep(self, word: str, limit: int = 400) -> list[tuple[str, int, str]]:
        cmd = ["git", "-C", self.git_dir, "grep", "-n", "-I", "-w", "-F", "-e", word, self.rev, "--", ".",
               ":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/dist/**",
               ":(exclude,glob)**/bin/**", ":(exclude,glob)**/obj/**", ":(exclude,glob)**/*.min.js"]
        out = run(cmd, check=False)
        hits, prefix = [], f"{self.rev}:"
        for line in out.splitlines():
            if line.startswith(prefix):
                line = line[len(prefix):]
            path, _, rest = line.partition(":")
            num, _, text = rest.partition(":")
            if num.isdigit():
                hits.append((path, int(num), text.strip()[:240]))
                if len(hits) >= limit:
                    break
        return hits


# ----------------------------------------------------------------------------- rule packs

PROFILE_DIR = HERE / "profiles"
USER_PROFILE_DIR = Path(os.environ.get("PR_VISUAL_MAP_PROFILES", "~/.config/pr-visual-map/profiles")).expanduser()
IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
STRING_LIT_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`")
R = None  # the merged Rules, set in main()
REPO_SHORT = "root"  # the repository's name, used for files at the repository root


def ext_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


class Rules:
    """All stack knowledge, merged from rule packs: generic + detected stacks + org/repo overlays."""

    def __init__(self):
        self.packs = []
        self.code_ext, self.ui_ext, self.doc_ext = set(), set(), set()
        self.gen_path, self.gen_suffix, self.gen_names, self.gen_content = [], [], set(), []
        self.markers, self.strip_dirs, self.families = [], set(), {}
        self.classify_rules, self.flags, self.routes = [], [], []
        self.client_families, self.config_setters, self.config_readers = set(), [], []
        self.package_names, self.follow_links = [], False
        self.docs, self.project_docs, self.sources, self.outputs, self.tickets = [], [], [], [], []
        self.ext_family = {}

    def add(self, d: dict, source: str) -> None:
        def rx(p, flags=0):
            try:
                return re.compile(p, flags)
            except re.error as e:
                die(f"rule pack {source}: bad regex {p!r}: {e}")
        name = d.get("name", source)
        self.packs.append({"name": name, "description": d.get("description", ""), "source": source})
        ex = d.get("extensions", {})
        self.code_ext |= set(ex.get("code", []))
        self.ui_ext |= set(ex.get("ui", []))
        self.doc_ext |= set(ex.get("doc", []))
        g = d.get("generated", {})
        self.gen_path += [rx(p) for p in g.get("path", [])]
        self.gen_suffix += g.get("suffix", [])
        self.gen_names |= set(g.get("names", []))
        if g.get("content"):
            self.gen_content.append(rx(g["content"], re.I))
        self.markers += [rx(p) for p in d.get("projectMarkers", [])]
        self.strip_dirs |= set(d.get("stripDirs", []))
        for fam, s in d.get("families", {}).items():
            self.families[fam] = {"ext": set(s["ext"]), "types": rx(s["types"], re.M),
                                  "members": rx(s["members"]) if s.get("members") else None,
                                  "decl": rx(s["decl"]) if s.get("decl") else None,
                                  "indentScoped": s.get("indentScoped", False),
                                  "skip": set(s.get("skip", [])), "skipPrivate": s.get("skipPrivate", False),
                                  "callsLookLikeMembers": s.get("callsLookLikeMembers", False)}
        for r in d.get("classify", []):
            self.classify_rules.append({
                "order": r["order"], "seq": len(self.classify_rules), "kind": r["kind"], "source": source,
                "path": rx(r["path"]) if r.get("path") else None, "name": rx(r["name"]) if r.get("name") else None,
                "ext": r.get("ext"), "code": r.get("code"), "ui": r.get("ui"), "generated": r.get("generated"),
                "markGenerated": r.get("markGenerated", False)})
        for r in d.get("flags", []):
            self.flags.append({**r, "rx": rx(r["re"]), "filesRx": rx(r["files"]) if r.get("files") else None,
                               "escRx": rx(r["escalate"]["re"]) if r.get("escalate") else None, "source": source})
        for r in d.get("routes", []):
            self.routes.append({"families": set(r.get("families", [])),
                                "classPrefix": rx(r["classPrefix"]) if r.get("classPrefix") else None,
                                "method": rx(r["method"]), "token": r.get("controllerToken"),
                                "suffix": r.get("controllerSuffix", "")})
        self.client_families |= set(d.get("clientUrls", {}).get("families", []))
        self.package_names += [{"files": rx(x["files"]), "re": rx(x["re"], re.M)} for x in d.get("packageNames", [])]
        c = d.get("config", {})
        self.config_setters += [{"files": rx(s["files"]), "re": rx(s["re"])} for s in c.get("setters", [])]
        self.config_readers += [{"families": set(s.get("families", [])), "re": rx(s["re"])} for s in c.get("readers", [])]
        ctx = d.get("context", {})
        self.docs += [rx(p) for p in ctx.get("docs", [])]
        self.project_docs += [rx(p) for p in ctx.get("projectDocs", [])]
        self.follow_links = self.follow_links or bool(ctx.get("followLinks"))
        self.sources += [{**s, "pack": name} for s in ctx.get("sources", [])]
        if ctx.get("output") and ctx.get("sources"):
            self.outputs.append({**ctx["output"], "sourcePath": ctx["sources"][0]["path"]})
        self.tickets = [{"rx": rx(t["re"]), "url": t.get("url")} for t in d.get("tickets", [])] + self.tickets

    def finish(self) -> "Rules":
        self.classify_rules.sort(key=lambda r: (r["order"], r["seq"]))
        self.ext_family = {e: fam for fam, f in self.families.items() for e in f["ext"]}
        return self

    def family_of(self, path: str) -> str | None:
        return self.ext_family.get(ext_of(path))


def load_rules(paths: list[str], repo: str, git_dir: Path, base_rev: str | None) -> Rules:
    """generic.json, every stack pack whose detect regex matches a file, then overlays:
    repos/<owner>.json, repos/<owner>__<repo>.json and .pr-visual-map.json read at the BASE commit (so a PR
    cannot relax the rules it is reviewed with). Packs come from the skill's profiles/ folder and from a private
    user folder ($PR_VISUAL_MAP_PROFILES, default ~/.config/pr-visual-map/profiles) for rules you keep out of git."""
    rules = Rules()

    def read(p: Path) -> dict:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            die(f"rule pack {p.name} is not valid JSON: {e}")

    rules.add(read(PROFILE_DIR / "generic.json"), "generic.json")
    dirs = [(PROFILE_DIR, "")] + ([(USER_PROFILE_DIR, "user:")] if USER_PROFILE_DIR.is_dir() else [])
    for folder, tag in dirs:
        for p in sorted(folder.glob("*.json")):
            if p.name == "generic.json" and not tag:
                continue
            d = read(p)
            detect = [re.compile(x) for x in d.get("detect", [])]
            if not detect or any(rx.search(f) for rx in detect for f in paths):
                rules.add(d, f"{tag}{p.name}")
    owner, _, name = repo.partition("/")
    for folder, tag in dirs:
        for cand in (folder / "repos" / f"{owner}.json", folder / "repos" / f"{owner}__{name}.json"):
            if cand.exists():
                rules.add(read(cand), f"{tag}repos/{cand.name}")
    if base_rev:
        p = subprocess.run(["git", "-C", str(git_dir), "show", f"{base_rev}:.pr-visual-map.json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode == 0:
            try:
                rules.add(json.loads(p.stdout), ".pr-visual-map.json (base)")
            except json.JSONDecodeError as e:
                die(f".pr-visual-map.json at the base commit is not valid JSON: {e}")
    return rules.finish()


def classify(path: str, first_added: list[str]) -> tuple[str, bool]:
    """Return (kind, generated): the first matching classify rule, in order."""
    low, name, ext = path.lower(), path.rsplit("/", 1)[-1], ext_of(path)
    lname = name.lower()
    generated = (any(rx.search(low) for rx in R.gen_path) or lname.endswith(tuple(R.gen_suffix))
                 or lname in R.gen_names)
    if not generated and first_added:
        head = "\n".join(first_added[:25]).lower()
        generated = any(rx.search(head) for rx in R.gen_content)
    for r in R.classify_rules:
        if r["ext"] is not None and ext not in (R.ui_ext if r["ext"] == "@ui" else r["ext"]):
            continue
        if (r["code"] is True and ext not in R.code_ext) or (r["code"] is False and ext in R.code_ext):
            continue
        if r["ui"] is False and ext in R.ui_ext:
            continue
        if r["generated"] is not None and bool(r["generated"]) != generated:
            continue
        if (r["path"] and not r["path"].search(path)) or (r["name"] and not r["name"].search(name)):
            continue
        return r["kind"], generated or r["markGenerated"]
    return "unmapped", generated


# ----------------------------------------------------------------------------- symbols

def declared_types(path: str, text: str) -> list[tuple[str, int, str]]:
    """Top-level declared symbols: (name, line, keyword)."""
    fam = R.families.get(R.family_of(path))
    if not fam:
        return []
    out, seen = [], set()
    for m in fam["types"].finditer(text):
        name = m.group("name")
        if name and len(name) >= 3 and name not in seen:
            seen.add(name)
            kw = m.group("kw") if "kw" in fam["types"].groupindex else ""
            out.append((name, text.count("\n", 0, m.start("name")) + 1, kw))
    return out


def member_names(path: str, line: str) -> list[str]:
    fam = R.families.get(R.family_of(path))
    if not fam:
        return []
    names = [m.group("name") for m in fam["types"].finditer(line)]
    if fam["members"]:
        m = fam["members"].match(line)
        if m and m.group("name") not in fam["skip"]:
            names.append(m.group("name"))
    if fam["skipPrivate"]:
        names = [n for n in names if not n.startswith("_")]
    return [n for n in dict.fromkeys(names) if len(n) >= 4]



def first_line_of(text: str, name: str) -> int:
    m = re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", text)
    return text.count("\n", 0, m.start()) + 1 if m else 1


# ----------------------------------------------------------------------------- naming helpers

def slug(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s)
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "root"


def humanize(s: str) -> list[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    words = [w for w in re.split(r"[\s._\-/]+", s) if w]
    return [w[:1].upper() + w[1:] for w in words]


def label_of(words: list[str], noun: str) -> str:
    words = [w for w in words if w.lower() not in ("test", "tests", noun.lower())]
    if not words:
        return noun[:1].upper() + noun[1:]
    keep = 3 - len(noun.split())
    return " ".join((words[-keep:] if len(words) > keep else words) + [noun]).strip()


def dedupe_labels(nodes: list[dict]) -> None:
    """Same label twice: prefix the project's short name, else number them (still max 3 words)."""
    seen = defaultdict(list)
    for n in nodes:
        seen[n["label"]].append(n)
    for label, ns in seen.items():
        if len(ns) < 2:
            continue
        for i, n in enumerate(sorted(ns, key=lambda n: n["id"])):
            proj = humanize((n.get("project") or "").rsplit("/", 1)[-1].rsplit(".", 1)[-1])[-1:]
            words = label.split()
            cand = " ".join(proj + words[-(3 - len(proj)):]) if proj else label
            if i and (cand in {m["label"] for m in nodes} or cand == label):
                cand = " ".join(words[-2:] + [str(i + 1)])
            n["label"] = cand


def short_names(names: list[str]) -> dict[str, str]:
    """Strip the dotted prefix shared by the project names (Acme.Shop.Application -> Application)."""
    base = {n: n.rsplit("/", 1)[-1] for n in names}
    dotted = [b.split(".") for b in base.values() if "." in b]
    prefix = []
    if len(dotted) >= 2:
        for parts in zip(*dotted):
            if len(set(parts)) == 1:
                prefix.append(parts[0])
            else:
                break
    res = {}
    for n, b in base.items():
        parts = b.split(".")
        if prefix and parts[:len(prefix)] == prefix and len(parts) > len(prefix):
            parts = parts[len(prefix):]
        res[n] = ".".join(parts) or b or "root"
    return res


# ----------------------------------------------------------------------------- per-file facts

def project_roots(tree: Tree, pr_paths: list[str]) -> set[str]:
    roots = set()
    for f in list(tree.files) + pr_paths:
        if "node_modules/" not in f and any(rx.search(f) for rx in R.markers):
            roots.add(f.rsplit("/", 1)[0] if "/" in f else "")
    return roots


def project_of(path: str, roots: set[str]) -> str:
    parts = path.split("/")[:-1]
    for i in range(len(parts), -1, -1):
        d = "/".join(parts[:i])
        if d in roots:
            return d
    if path.endswith((".tf", ".tfvars")):
        return "/".join(parts)
    return parts[0] if parts else ""


def build_file_facts(diff_files: dict, rest_files: list[dict], tree: Tree) -> tuple[list[dict], list[str]]:
    """Merge REST numbers (authoritative) with parsed hunks. Returns (files, deviations)."""
    deviations, files = [], []
    by_path = dict(diff_files)
    for rf in rest_files:
        path = rf["filename"]
        df = by_path.get(path)
        rest_status = {"removed": "deleted", "copied": "added", "changed": "modified",
                       "unchanged": "modified"}.get(rf["status"], rf["status"])
        if df is None:
            deviations.append(f"{path}: listed by GitHub but missing from the diff (status {rf['status']}).")
            df = {"path": path, "oldPath": rf.get("previous_filename") or path, "status": rest_status,
                  "binary": True, "additions": 0, "deletions": 0, "hunks": [], "missing": True}
        elif df["status"] != rest_status:
            deviations.append(f"{path}: status from diff headers '{df['status']}' vs GitHub '{rest_status}'"
                              f" (diff headers used).")
        if not df.get("missing") and not df["binary"] and (df["additions"], df["deletions"]) != (
                rf["additions"], rf["deletions"]):
            deviations.append(f"{path}: parsed +{df['additions']}/-{df['deletions']} but GitHub reports "
                              f"+{rf['additions']}/-{rf['deletions']}.")
            df["parseMismatch"] = True
        first_added = [ln[1:] for h in df["hunks"][:1] for ln in h["lines"] if ln.startswith("+")]
        kind, generated = classify(path, first_added if df["status"] == "added" else [])
        files.append({**df, "additions": rf["additions"], "deletions": rf["deletions"],
                      "parsedAdditions": df["additions"], "parsedDeletions": df["deletions"],
                      "kind": kind, "generated": generated, "family": R.family_of(path)})
    extra = set(by_path) - {rf["filename"] for rf in rest_files}
    for p in sorted(extra):
        deviations.append(f"{p}: in the diff but not in GitHub's file list.")
    return files, deviations


def assign_projects(files: list[dict], tree: Tree) -> None:
    roots = project_roots(tree, [f["path"] for f in files])
    projects = sorted({project_of(f["path"], roots) for f in files})
    tops = sorted({f["path"].split("/")[0] if "/" in f["path"] else "" for f in files})
    shorts = short_names([p for p in projects if p] + [t for t in tops if t])
    by_proj = defaultdict(list)
    for f in files:
        f["project"] = project_of(f["path"], roots)
        f["top"] = f["path"].split("/")[0] if "/" in f["path"] else ""
        f["projectShort"] = shorts.get(f["project"], REPO_SHORT)
        f["topShort"] = shorts.get(f["top"], REPO_SHORT)
        rel = f["path"][len(f["project"]) + 1:] if f["project"] else f["path"]
        parts = rel.split("/")[:-1]
        while parts and parts[0].lower() in R.strip_dirs:
            parts.pop(0)
        f["_parts"] = parts
        by_proj[f["project"]].append(f)
    for proj, fs in by_proj.items():  # drop deep shared prefixes (Java-style packages)
        code = [f["_parts"] for f in fs if f["kind"] not in ("test", "config", "infra") and f["_parts"]]
        common = []
        if len(code) >= 2:
            for parts in zip(*code):
                if len(set(parts)) == 1:
                    common.append(parts[0])
                else:
                    break
        for f in fs:
            parts = f["_parts"]
            if len(common) >= 2 and parts[:len(common)] == common and len(parts) > len(common):
                parts = parts[len(common):]
            f["feature"] = parts[0] if parts else ""
            f["feature2"] = parts[1] if len(parts) > 1 else ""


# ----------------------------------------------------------------------------- file-level edges

URL_LIT_RE = re.compile(r"[`'\"]([^`'\"\s]*?/[^`'\"\s]*?)[`'\"]")


def changed_lines(f: dict, tag: str = "+"):
    for h in f["hunks"]:
        for t, ln, text in hunk_line_numbers(h, old=f.get("status") == "deleted"):
            if t == tag:
                yield ln, text


def literal_segments(url: str) -> list[str]:
    url = re.sub(r"\$\{[^}]*\}", "", url)
    return [s.lower() for s in url.split("/") if s and not s.startswith(("{", ":")) and "." not in s and "~" not in s]


def file_edges(files: list[dict], tree: Tree) -> list[dict]:
    """Static type references among changed files + inferred HTTP and config-binding links."""
    texts = {f["path"]: tree.read(f["path"]) for f in files if f["status"] != "deleted"}
    symbols = defaultdict(list)  # name -> [(path, line, family)]
    for f in files:
        t = texts.get(f["path"])
        f["declared"] = declared_types(f["path"], t) if t else []
        for name, line, _kw in f["declared"]:
            symbols[name].append((f["path"], line, f["family"]))
    edges = []
    for f in files:
        t = texts.get(f["path"])
        if not t or not f["family"]:
            continue
        idents = set(IDENT_RE.findall(t))
        for name, owners in symbols.items():
            if name not in idents:
                continue
            for owner, _line, fam in owners:
                if owner != f["path"] and fam == f["family"]:
                    edges.append({"from": f["path"], "to": owner, "kind": "uses", "source": "static",
                                  "evidence": [f"{f['path']}:{first_line_of(t, name)}"], "symbol": name})
    # inferred: client URL literal -> server route (both on changed lines)
    routes = []
    for f in files:
        t = texts.get(f["path"])
        if f["kind"] != "endpoint" or not t:
            continue
        for rr in R.routes:
            if f["family"] not in rr["families"]:
                continue
            prefix = ""
            cm = rr["classPrefix"].search(t) if rr["classPrefix"] else None
            if cm:
                prefix = cm.group(1)
                if rr["token"]:
                    ctrl = re.sub(re.escape(rr["suffix"]) + "$", "", cm.group(2)) if rr["suffix"] else cm.group(2)
                    prefix = prefix.replace(rr["token"], ctrl.lower())
            for ln, text in changed_lines(f):
                for m in rr["method"].finditer(text):
                    tmpl = next((g for g in m.groups() if g is not None), "")
                    full = tmpl if tmpl.startswith(("/", "~/")) or not prefix else f"{prefix}/{tmpl}"
                    segs = literal_segments(full)
                    if segs:
                        routes.append((f["path"], ln, segs))
    if routes:
        for f in files:
            if f["family"] not in R.client_families:
                continue
            for ln, text in changed_lines(f):
                for url in URL_LIT_RE.findall(text):
                    u = literal_segments(url)
                    for rpath, rln, r in routes:
                        k = min(len(u), len(r))
                        if k >= 2 and u[-k:] == r[-k:]:
                            edges.append({"from": f["path"], "to": rpath, "kind": "calls", "source": "inferred",
                                          "evidence": [f"{f['path']}:{ln}", f"{rpath}:{rln}"],
                                          "note": "client URL matches server route"})
    # inferred: Terraform app settings Section__Key -> code reading that section
    sections = defaultdict(list)
    for f in files:
        for st in R.config_setters:
            if st["files"].search(f["path"]):
                for ln, text in changed_lines(f):
                    m = st["re"].match(text)
                    if m:
                        sections[m.group(1)].append(f"{f['path']}:{ln}")
    if sections:
        for f in files:
            readers = [rd["re"] for rd in R.config_readers if f["family"] in rd["families"]]
            for ln, text in changed_lines(f) if readers else []:
                for g in (g for rd in readers for g in rd.findall(text)):
                    g = (g,) if isinstance(g, str) else g
                    name = next((x for x in g if x), "")
                    if name in sections:
                        tf_path = sections[name][0].rsplit(":", 1)[0]
                        edges.append({"from": f["path"], "to": tf_path, "kind": "reads", "source": "inferred",
                                      "evidence": [f"{f['path']}:{ln}", sections[name][0]],
                                      "note": f"config section {name} is set by infrastructure"})
    edges += name_edges(files, tree)
    return edges


FILE_TOKEN_RE = re.compile(r"[\w.@-]+\.[A-Za-z0-9]{1,8}\b")
PATH_TOKEN_RE = re.compile(r"[\w.@-]+(?:/[\w.@-]+)+")


def name_edges(files: list[dict], tree: Tree) -> list[dict]:
    """Inferred links by name: a changed line names another changed file (file name, or its last two folders),
    or a package declared by a manifest (the packs' packageNames rules)."""
    by_base, by_dir, edges = defaultdict(list), defaultdict(list), []
    for f in files:
        base = f["path"].rsplit("/", 1)[-1]
        if len(base) >= 6:
            by_base[base].append(f["path"])
        parts = f["path"].split("/")[:-1]
        if len(parts) >= 2:
            by_dir["/".join(parts[-2:])].append(f["path"])
    packages = {}
    manifests = {p for p in tree.files if any(r["files"].search(p) for r in R.package_names)}
    roots = {f["project"] for f in files}
    for mp in sorted(manifests):
        root = mp.rsplit("/", 1)[0] if "/" in mp else ""
        if root not in roots:
            continue
        for r in R.package_names:
            m = r["files"].search(mp) and r["re"].search(tree.read(mp) or "")
            if m and len(m.group(1)) >= 4:
                target = mp if mp in by_base.get(mp.rsplit("/", 1)[-1], []) else sorted(
                    f["path"] for f in files if f["project"] == root)[0]
                packages[m.group(1)] = (root, target)
    pkg_res = {name: re.compile(r"(?<![\w@/.-])" + re.escape(name) + r"(?![\w/.-])") for name in packages}
    for f in files:
        if f["status"] == "deleted" or ext_of(f["path"]) in R.doc_ext:
            continue
        for ln, text in changed_lines(f):
            hits = set()
            for tok in FILE_TOKEN_RE.findall(text):
                for t in by_base.get(tok.rsplit("/", 1)[-1], []):
                    hits.add((t, f"names the file {tok.rsplit('/', 1)[-1]}"))
            for tok in PATH_TOKEN_RE.findall(text):
                segs = [x for x in tok.split("/") if x not in (".", "..", "")]
                for k in (segs[-2:], segs[-3:-1]):
                    for t in by_dir.get("/".join(k), [])[:3] if len(k) == 2 else []:
                        hits.add((t, f"names the folder {'/'.join(k)}"))
            for name, rx in pkg_res.items():
                root, target = packages[name]
                if f["project"] != root and rx.search(text):
                    hits.add((target, f"refers to the package {name}"))
            for target, note in sorted(hits):
                if target != f["path"]:
                    edges.append({"from": f["path"], "to": target, "kind": "uses", "source": "inferred",
                                  "evidence": [f"{f['path']}:{ln}"], "note": note})
    return edges


# ----------------------------------------------------------------------------- blocks

def block_key(f: dict) -> tuple:
    k = f["kind"]
    if k == "unmapped":
        return ("docs",) if ext_of(f["path"]) in R.doc_ext else ("unmapped",)
    if k == "test":
        return ("test", f["project"])
    if k in ("config", "infra"):
        return (k, f["top"])
    if k == "migration":
        return ("migration", f["project"])
    if f["generated"]:
        return ("generated", f["project"])
    return ("code", f["project"], f["feature"])


def group_files(files: list[dict], fedges: list[dict]) -> dict:
    groups = defaultdict(list)
    for f in files:
        groups[block_key(f)].append(f)
    for key in sorted(k for k in groups if k[0] == "code" and k[2] == ""):
        layer = LAYER_OF[dominant_kind(groups[key])]  # merge only into a block of the same layer
        siblings = sorted(k for k in groups if k[0] == "code" and k[1] == key[1] and k[2] != ""
                          and LAYER_OF[dominant_kind(groups[k])] == layer)
        if not siblings:
            continue
        paths = {f["path"] for f in groups[key]}
        score = Counter()
        for e in fedges:
            if e["from"] in paths or e["to"] in paths:
                other = e["to"] if e["from"] in paths else e["from"]
                for k in siblings:
                    if any(f["path"] == other for f in groups[k]):
                        score[k] += 1
        target = max(siblings, key=lambda k: (score[k], len(groups[k]), k[2]))
        groups[target].extend(groups.pop(key))
    final = {}
    for key, fs in sorted(groups.items()):
        if len(fs) > MAX_BLOCK_FILES and key[0] in ("code", "test", "config", "infra"):
            sub = defaultdict(list)
            for f in fs:
                sk = f["feature2"] if key[0] == "code" else f["feature"] if key[0] == "test" else f["project"]
                sub[sk].append(f)
            if len(sub) > 1:
                for sk, sfs in sorted(sub.items()):
                    final[key + (sk,)] = sfs
                continue
        final[key] = fs
    return final


def dominant_kind(fs: list[dict]) -> str:
    kinds = [f["kind"] for f in fs]
    for k in ("endpoint", "migration"):
        if k in kinds:
            return k
    c = Counter(kinds)
    return max(c, key=lambda k: (c[k], -KIND_ORDER.index(k)))


def block_id_label(key: tuple, fs: list[dict], kind: str) -> tuple[str, str]:
    f0 = fs[0]
    base_len = 3 if key[0] == "code" else 2
    sub = key[base_len] if len(key) > base_len else ""
    if key[0] == "unmapped":
        return "unmapped", "Other files"
    if key[0] == "docs":
        return "docs", "Docs"
    if key[0] == "test":
        return slug(f"{f0['projectShort']}-tests-{sub}"), label_of(humanize(sub or f0["projectShort"]), "tests")
    if key[0] == "config":
        return slug(f"{f0['topShort']}-config-{sub}"), label_of(humanize(f0["topShort"]), "config")
    if key[0] == "infra":
        if all(f["path"].endswith((".tf", ".tfvars")) for f in fs):
            words = humanize(f0["project"].rsplit("/", 1)[-1])[-2:]
            return slug(f"{f0['topShort']}-terraform-{sub}"), " ".join(["Terraform"] + words)
        if all(".github/workflows/" in f["path"] for f in fs):
            return slug(f"{f0['topShort']}-ci"), "CI workflows"
        return slug(f"{f0['topShort']}-infra-{sub}"), label_of(humanize(f0["topShort"]), "infra")
    if key[0] == "migration":
        return slug(f"{f0['projectShort']}-migrations"), "DB migrations"
    if key[0] == "generated":
        return slug(f"{f0['projectShort']}-generated"), "Generated client" if f0["family"] == "js" else "Generated code"
    name = sub or f0["feature"] or f0["projectShort"]
    return slug(f"{f0['projectShort']}-{f0['feature']}-{sub}"), label_of(humanize(name), KIND_NOUN[kind])


def common_parts(fs: list[dict]) -> list[str]:
    parts = [f.get("_parts") or [] for f in fs]
    out = []
    for col in zip(*parts):
        if len(set(col)) != 1:
            break
        out.append(col[0])
    return out


def graph_file(f: dict, budget: list[int]) -> dict:
    hunks, stored, truncated = [], 0, False
    for h in f["hunks"]:
        lines = h["lines"]
        room = min(MAX_LINES_PER_FILE - stored, budget[0])
        if room <= 0:
            truncated = True
            break
        if len(lines) > room:
            lines, truncated = lines[:room], True
        stored += len(lines)
        budget[0] -= len(lines)
        hunks.append({"header": h["header"], "startLine": h["startLine"], "oldStart": h["oldStart"], "lines": lines})
    return {"path": f["path"], "oldPath": f["oldPath"] if f["oldPath"] != f["path"] else None,
            "status": f["status"], "additions": f["additions"], "deletions": f["deletions"],
            "generated": f["generated"], "binary": f["binary"], "hunks": hunks, "truncated": truncated}


def file_evidence(f: dict) -> str:
    if f["hunks"]:
        for t, ln, _ in hunk_line_numbers(f["hunks"][0], old=f["status"] == "deleted"):
            if t in "+-":
                return f"{f['path']}:{max(ln, 1)}"
        return f"{f['path']}:{max(f['hunks'][0]['startLine'], 1)}"
    return f"{f['path']}:1"


def default_summary(fs: list[dict]) -> dict:
    by = defaultdict(list)
    for f in fs:
        by[f["status"]].append(f["path"].rsplit("/", 1)[-1])
    parts = []
    for st in ("added", "modified", "renamed", "deleted"):
        names = by.get(st)
        if names:
            more = f" +{len(names) - 3} more" if len(names) > 3 else ""
            parts.append(f"{st}: {', '.join(names[:3])}{more}")
    return {"text": "; ".join(parts) + ".", "source": "extracted", "evidence": [file_evidence(f) for f in fs[:6]]}


def status_of(statuses) -> str:
    s = set(statuses)
    if s == {"added"}:
        return "added"
    if s == {"deleted"}:
        return "deleted"
    return "modified"


def make_nodes(blocks: dict, budget: list[int]) -> tuple[list[dict], dict]:
    nodes, file_block, used = [], {}, set()
    for key, fs in blocks.items():
        fs = sorted(fs, key=lambda f: f["path"])
        kind = "dto" if key[0] == "generated" else dominant_kind(fs)
        nid, label = block_id_label(key, fs, kind)
        base, i = nid, 2
        while nid in used:
            nid, i = f"{base}-{i}", i + 1
        used.add(nid)
        nodes.append({
            "id": nid, "label": label, "kind": kind, "level": 1, "parent": None, "layer": LAYER_OF[kind],
            "status": status_of(f["status"] for f in fs),
            "stats": {"files": len(fs), "additions": sum(f["additions"] for f in fs),
                      "deletions": sum(f["deletions"] for f in fs)},
            "risk": [], "summary": default_summary(fs), "why": None,
            "files": [graph_file(f, budget) for f in fs], "mustRead": False, "reviewOrder": None,
            "questions": [], "generated": all(f["generated"] for f in fs),
            "project": fs[0]["project"], "top": fs[0]["top"], "dirParts": common_parts(fs)})
        for f in fs:
            file_block[f["path"]] = nid
    return nodes, file_block


def block_edges(fedges: list[dict], file_block: dict) -> list[dict]:
    agg = {}
    for e in fedges:
        a, b = file_block.get(e["from"]), file_block.get(e["to"])
        if not a or not b or a == b:
            continue
        k = (a, b, e["kind"], e["source"])
        cur = agg.setdefault(k, {"from": a, "to": b, "kind": e["kind"], "source": e["source"], "evidence": [],
                                 "symbols": [], "note": e.get("note")})
        for ev in e["evidence"]:
            if ev not in cur["evidence"] and len(cur["evidence"]) < 4:
                cur["evidence"].append(ev)
        if e.get("symbol") and e["symbol"] not in cur["symbols"]:
            cur["symbols"].append(e["symbol"])
    return sorted(agg.values(), key=lambda e: (e["from"], e["to"], e["kind"]))


# ----------------------------------------------------------------------------- blast radius

def is_decl_of(path: str, line: str, name: str) -> bool:
    fam = R.families.get(R.family_of(path) or "")
    m = fam["decl"].match(line) if fam and fam["decl"] else None
    return bool(m and m.group("name") == name)


def changed_functions(files: list[dict], tree: Tree) -> list[dict]:
    """Public functions whose body changed (signature may be unchanged): their callers are affected too.
    A changed line belongs to the nearest declaration above it; a type declaration or a private function stops
    the search. Capped at MAX_CALLER_SYMBOLS, the most-changed first."""
    counts, where = Counter(), {}
    for f in files:
        fam = R.families.get(f["family"] or "")
        if f["status"] not in ("modified", "renamed") or f["generated"] or f["kind"] == "test" or not fam or not fam["decl"]:
            continue
        lines = (tree.read(f["path"]) or "").split("\n")
        for tag in "+-":
            for ln, text in changed_lines(f, tag):
                indent = len(text) - len(text.lstrip())
                for i in range(min(ln, len(lines)) - 1, max(-1, ln - 400), -1):
                    line = lines[i]
                    tm = fam["types"].search(line)
                    if tm and "kw" in fam["types"].groupindex and tm.group("kw") in TYPE_KWS:
                        break
                    m = fam["decl"].match(line)
                    if not m or m.group("name") in fam["skip"]:
                        continue
                    if fam["indentScoped"] and i + 1 != ln and indent <= len(line) - len(line.lstrip()):
                        break
                    name = m.group("name")
                    if name in member_names(f["path"], line):  # public
                        owner_type = ""
                        for j in range(i - 1, -1, -1):  # the type the method belongs to, if any
                            tm2 = fam["types"].search(lines[j])
                            if tm2 and "kw" in fam["types"].groupindex and tm2.group("kw") in TYPE_KWS:
                                owner_type = tm2.group("name")
                                break
                        counts[(f["path"], name, owner_type)] += 1
                        where.setdefault((f["path"], name, owner_type), i + 1)
                    break
    return [{"name": name, "mode": "callers", "owner": path, "ownerType": owner_type, "deletedFile": False,
             "evidence": f"{path}:{where[(path, name, owner_type)]}"}
            for (path, name, owner_type), _ in counts.most_common(MAX_CALLER_SYMBOLS)]


def blast_radius(files: list[dict], tree: Tree, file_block: dict, budget: list[int]) -> dict:
    pr_paths = {f["path"] for f in files}
    symbols = []
    for f in files:
        if f["status"] in ("deleted", "renamed"):
            if f["status"] == "deleted":
                removed = "\n".join(t for h in f["hunks"] for tag, _, t in hunk_line_numbers(h) if tag == "-")
                for name, line, _kw in declared_types(f["path"], removed):
                    symbols.append({"name": name, "mode": "refs", "owner": f["path"], "deletedFile": True,
                                    "evidence": f"{f['path']}:1"})
            stem = f["oldPath"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if f["family"] == "js" and len(stem) >= 4:
                symbols.append({"name": stem, "mode": "imports", "owner": f["path"],  # old path is gone either way
                                "deletedFile": True, "evidence": f"{f['path']}:1"})
        if f["status"] in ("modified", "renamed"):
            for ln, text in changed_lines(f, "-"):
                if (R.families.get(f["family"] or "", {}).get("callsLookLikeMembers")
                        and text.rstrip().endswith(";") and "):" not in text):
                    continue
                for name in member_names(f["path"], text):
                    symbols.append({"name": name, "mode": "refs", "owner": f["path"], "deletedFile": False,
                                    "evidence": f"{f['path']}:{ln}"})
            decls = sorted((line, kw, name) for name, line, kw in f.get("declared", []))
            for ln, text in changed_lines(f, "+"):
                if not member_names(f["path"], text):
                    continue
                enclosing = [d for d in decls if d[0] <= ln]
                if enclosing and enclosing[-1][1] == "interface":
                    symbols.append({"name": enclosing[-1][2], "mode": "implementers", "owner": f["path"],
                                    "deletedFile": False, "evidence": f"{f['path']}:{ln}"})
    symbols += changed_functions(files, tree)
    seen, uniq = set(), []
    for s in symbols:
        k = (s["name"], s["mode"], s.get("ownerType", ""))
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    affected = defaultdict(list)
    deleted_refs = defaultdict(list)
    skipped, capped = [], False
    for s in uniq:
        name = s["name"]
        hits = tree.grep(name)
        decl_re = re.compile(r"\b(class|interface|record|struct|enum|type|function|def|func)\s+" + re.escape(name) + r"\b")
        refs = [h for h in hits if not decl_re.search(h[2])]
        if s["mode"] in ("refs", "callers"):
            word = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
            refs = [h for h in refs if word.search(STRING_LIT_RE.sub("", h[2])) and not is_decl_of(h[0], h[2], name)]
        if s["mode"] == "callers":  # call syntax, and for a method the caller must name its type
            call = re.compile(r"(?<![\w$])" + re.escape(name) + r"\s*[(<]")
            owner_rx = re.compile(r"(?<![\w$])" + re.escape(s["ownerType"]) + r"(?![\w$])") if s["ownerType"] else None
            refs = [h for h in refs if call.search(h[2]) and (not owner_rx or owner_rx.search(tree.read(h[0]) or ""))]
        if s["mode"] == "implementers":
            refs = [h for h in refs if re.search(r"(:|implements|extends)[^{;]*\b" + re.escape(name) + r"\b", h[2])]
        if s["mode"] == "imports":
            spec = re.compile(r"[/'\"]" + re.escape(name) + r"(\.\w+)?['\"]")
            refs = [h for h in refs if re.search(r"\b(import|require|from)\b", h[2]) and spec.search(h[2])]
        if s["deletedFile"]:
            still_declared = any(decl_re.search(h[2]) for h in hits)
            stem_exists = s["mode"] == "imports" and any(
                p.rsplit("/", 1)[-1].rsplit(".", 1)[0] == name for p in tree.files)
            if refs and not still_declared and not stem_exists:
                deleted_refs[s["owner"]].extend((f"{p}:{l}", name) for p, l, _ in refs[:6])
        outside = [h for h in refs if h[0] not in pr_paths and ext_of(h[0]) not in R.doc_ext
                   and classify(h[0], [])[0] != "unmapped"]
        if len(outside) > TOO_COMMON_HITS:
            skipped.append(name)
            continue
        per_file = defaultdict(list)
        for p, l, t in outside:
            per_file[p].append((l, t))
        if len(per_file) > MAX_FILES_PER_SYMBOL:
            capped = True
        for p in sorted(per_file)[:MAX_FILES_PER_SYMBOL]:
            for l, t in per_file[p][:5]:
                affected[p].append({"line": l, "text": t, "symbol": name, "owner": s["owner"], "mode": s["mode"]})
    if len(affected) > MAX_AFFECTED_FILES:
        capped = True
        affected = dict(sorted(affected.items())[:MAX_AFFECTED_FILES])
    # affected files -> affected blocks
    pseudo = []
    for p, refs in sorted(affected.items()):
        kind, gen = classify(p, [])
        pseudo.append({"path": p, "kind": kind, "generated": gen, "family": R.family_of(p),
                       "refs": refs, "status": "affected"})
    nodes, edges = [], []
    if pseudo:
        assign_projects(pseudo, tree)
        by_key = defaultdict(list)
        for f in pseudo:
            by_key[(f["project"], f["feature"])].append(f)
        for (proj, feat), fs in sorted(by_key.items()):
            f0 = fs[0]
            kind = dominant_kind(fs)
            nid = "affected-" + slug(f"{f0['projectShort']}-{feat}")
            ev = [f"{r_f['path']}:{r['line']}" for r_f in fs for r in r_f["refs"]][:6]
            syms = sorted({r["symbol"] for r_f in fs for r in r_f["refs"]})
            gfiles = []
            for r_f in fs:
                lines = [" " + r["text"] for r in r_f["refs"]]
                budget[0] -= len(lines)
                gfiles.append({"path": r_f["path"], "oldPath": None, "status": "affected", "additions": 0,
                               "deletions": 0, "generated": r_f["generated"], "binary": False, "truncated": False,
                               "hunks": [{"header": f"line {r['line']}: references {r['symbol']}",
                                          "startLine": r["line"], "lines": [" " + r["text"]]} for r in r_f["refs"]]})
            nodes.append({
                "id": nid, "label": label_of(humanize(feat or f0["projectShort"]),
                                             "test callers" if kind == "test" else "callers"), "kind": kind,
                "level": 1, "parent": None, "layer": LAYER_OF[kind], "status": "affected",
                "stats": {"files": len(fs), "additions": 0, "deletions": 0,
                          "refs": sum(len(r_f["refs"]) for r_f in fs)},
                "risk": [], "summary": {"text": f"Unchanged code that references {', '.join(syms[:4])}"
                                                f"{' …' if len(syms) > 4 else ''}.", "source": "extracted",
                                        "evidence": ev},
                "why": None, "files": gfiles, "mustRead": False, "reviewOrder": None, "questions": [],
                "generated": False, "project": proj, "top": f0["top"], "dirParts": common_parts(fs)})
            targets = defaultdict(list)
            for r_f in fs:
                for r in r_f["refs"]:
                    tb = file_block.get(r["owner"])
                    if tb:
                        targets[tb].append((f"{r_f['path']}:{r['line']}", r["symbol"]))
            for tb, evs in sorted(targets.items()):
                edges.append({"from": nid, "to": tb, "kind": "uses", "source": "affected",
                              "evidence": [e for e, _ in evs[:4]], "symbols": sorted({s for _, s in evs}),
                              "note": None})
    return {"nodes": nodes, "edges": edges, "deletedRefs": deleted_refs,
            "summary": {"symbolsSearched": [s["name"] for s in uniq], "affectedFiles": len(affected),
                        "capped": capped, "skippedTooCommon": skipped,
                        "caps": {"filesPerSymbol": MAX_FILES_PER_SYMBOL, "totalFiles": MAX_AFFECTED_FILES}}}


# ----------------------------------------------------------------------------- risk flags (rules)

def fill_reason(tmpl: str, m: re.Match, text: str, tag: str) -> str:
    def group(k):
        try:
            return m.group(k) or ""
        except IndexError:
            return ""
    out = re.sub(r"\{(\d+)\}", lambda g: group(int(g.group(1))), tmpl)
    out = out.replace("{line}", text.strip()[:90]).replace("{verb}", "added" if tag == "+" else "removed")
    return " ".join(out.split())


def file_findings(f: dict) -> list[tuple]:
    """(type, severity, reason, evidence) by rule."""
    out, path = [], f["path"]
    base = path.rsplit("/", 1)[-1]
    if f["kind"] == "migration":
        out.append(("migration", "high", f"Database migration: {base}", file_evidence(f)))
    if f["kind"] == "infra":
        out.append(("infra", "medium", f"Infrastructure change: {base} (+{f['additions']} −{f['deletions']})",
                    file_evidence(f)))
    if f["kind"] == "config":
        out.append(("config", "medium", f"Config changed: {base} (+{f['additions']} −{f['deletions']})",
                    file_evidence(f)))
    if f["kind"] == "dto" and not f["generated"] and f["status"] in ("modified", "renamed", "deleted"):
        for ln, text in changed_lines(f, "-"):
            if member_names(path, text):
                out.append(("public-api", "high" if f["status"] == "deleted" else "medium",
                            "DTO / contract shape changed", f"{path}:{ln}"))
                break
    is_doc = f["kind"] == "unmapped" or ext_of(path) in R.doc_ext
    if not f["generated"] and not is_doc and f["status"] == "added" and f["additions"] >= LARGE_NEW_FILE:
        out.append(("large-hunk", "medium", f"Large new file: {f['additions']} lines", file_evidence(f)))
    for h in f["hunks"] if not f["generated"] and not is_doc and f["status"] != "added" else []:
        n = sum(1 for line in h["lines"] if line[:1] in "+-")
        if n >= LARGE_HUNK:
            out.append(("large-hunk", "medium", f"Large hunk: {n} changed lines", f"{path}:{max(h['startLine'], 1)}"))
    if f["kind"] == "test":
        return out
    rules = [r for r in R.flags
             if (not r["filesRx"] or r["filesRx"].search(path)) and (not r.get("kinds") or f["kind"] in r["kinds"])
             and f["kind"] not in r.get("notKinds", []) and not (r.get("skipGenerated") and f["generated"])
             and not (r.get("codeOnly") and not f["family"])]
    for tag in "+-":
        for ln, text in changed_lines(f, tag):
            for r in rules:
                if tag not in r.get("on", "+"):
                    continue
                m = r["rx"].search(text)
                if not m:
                    continue
                if r.get("safe") and (m.group(r["safe"]["group"]) or "").startswith(tuple(r["safe"]["prefixes"])):
                    continue
                if r.get("ignore") and (m.group(r["ignore"]["group"]) or "") in r["ignore"]["values"]:
                    continue
                sev = r["severity"][tag] if isinstance(r["severity"], dict) else r["severity"]
                if r["escRx"] and r["escRx"].search(m.group(r["escalate"]["group"]) or ""):
                    sev = r["escalate"]["to"]
                tmpl = r["reason"][tag] if isinstance(r["reason"], dict) else r["reason"]
                out.append((r["type"], sev, fill_reason(tmpl, m, text, tag), f"{path}:{ln}"))
    return out


def merge_findings(findings: list[tuple]) -> list[dict]:
    merged = {}
    for typ, sev, reason, ev in findings:
        cur = merged.setdefault((typ, sev), {"type": typ, "severity": sev, "reasons": [], "evidence": [],
                                             "source": "rule"})
        if reason not in cur["reasons"]:
            cur["reasons"].append(reason)
        if ev not in cur["evidence"] and len(cur["evidence"]) < 6:
            cur["evidence"].append(ev)
    out = []
    for r in merged.values():
        extra = f" (+{len(r['reasons']) - 2} more)" if len(r["reasons"]) > 2 else ""
        out.append({"type": r["type"], "severity": r["severity"], "reason": "; ".join(r["reasons"][:2]) + extra,
                    "evidence": r["evidence"], "source": "rule"})
    return sorted(out, key=lambda r: (-SEVERITY_WEIGHT[r["severity"]], r["type"]))


def apply_risks(nodes: list[dict], files: list[dict], file_block: dict, deleted_refs: dict, tree: Tree) -> None:
    per_block = defaultdict(list)
    for f in files:
        for finding in file_findings(f):
            per_block[file_block[f["path"]]].append(finding)
    for owner, refs in deleted_refs.items():
        names = sorted({n for _, n in refs})
        for ev, _ in refs[:6]:
            per_block[file_block[owner]].append(("deleted-referenced", "high",
                                                 f"Deleted, but still referenced: {', '.join(names[:3])}", ev))
    tests = [tree.read(f["path"]) or "" for f in files if f["kind"] == "test" and f["status"] != "deleted"]
    test_idents = set()
    for t in tests:
        test_idents |= set(IDENT_RE.findall(t))
    by_path = {f["path"]: f for f in files}
    for n in nodes:
        if n["kind"] in ("endpoint", "handler", "entity", "ui") and not n["generated"]:
            names = {d[0] for gf in n["files"] for d in by_path[gf["path"]].get("declared", [])}
            if names and not (names & test_idents):
                per_block[n["id"]].append(("no-tests", "low", "No test in this PR references this block",
                                           n["summary"]["evidence"][0]))
        n["risk"] = merge_findings(per_block.get(n["id"], []))


# ----------------------------------------------------------------------------- review path, enrich

def risk_score(n: dict) -> float:
    s = sum(SEVERITY_WEIGHT[r["severity"]] for r in n["risk"] if r["type"] != "no-tests")
    return s + math.log2(1 + n["stats"]["additions"] + n["stats"]["deletions"]) / 4


def default_review_path(nodes: list[dict]) -> None:
    leaf = [n for n in nodes if n["status"] != "affected"]
    ranked = sorted([n for n in leaf if not n["generated"] and n["kind"] not in ("test", "unmapped")
                     and any(r["severity"] == "high" or (r["severity"] == "medium" and n["kind"] != "config")
                             for r in n["risk"])],
                    key=lambda n: (-risk_score(n), n["id"]))
    if len(ranked) < 2:  # few flags: the biggest code change is still worth reading
        biggest = sorted([n for n in leaf if not n["generated"] and n not in ranked
                          and n["kind"] not in ("test", "config", "unmapped")],
                         key=lambda n: (-(n["stats"]["additions"] + n["stats"]["deletions"]), n["id"]))
        ranked = ranked + biggest[:2 - len(ranked)]
    for n in ranked[:3]:
        n["mustRead"] = True
    order = [n for n in ranked[:3]] + sorted([n for n in leaf if not n["mustRead"]],
                                             key=lambda n: (LAYER_ORDER.index(n["layer"]), n["generated"], n["id"]))
    for i, n in enumerate(order, 1):
        n["reviewOrder"] = i


WHY_SOURCES = ("pr-body", "ticket", "kb", "docs", "commits", "inferred")
BLOCK_KEYS = {"label", "summary", "why", "questions", "risks", "mustRead", "reviewOrder"}
TOP_KEYS = {"ticket", "headline", "why", "questions", "readme", "blocks", "$schema"}
PLACEHOLDER_RE = re.compile(r"\b(TODO|TBD|FIXME|XXX|lorem ipsum)\b|\?\?\?|<fill", re.I)


def evidence_checker(files: list[dict], tree: Tree, extra_known: set):
    """Returns check(e) -> problem text or None. Lines must exist in the head file (or in the diff if deleted)."""
    by_path = {f["path"]: f for f in files}
    cache = {}

    def lines(path):
        if path not in cache:
            t = tree.read(path) if path in tree.fileset else None
            if t is not None:
                cache[path] = t.count("\n") + 1
            elif path in by_path:
                f = by_path[path]
                cache[path] = max([h["oldStart"] + len(h["lines"]) for h in f["hunks"]] + [f["deletions"] + 1])
            else:
                cache[path] = None
        return cache[path]

    def check(e):
        m = re.fullmatch(r"(.+):(\d+)", str(e))
        if not m:
            return f"'{e}' is not path:line"
        path, ln = m.group(1), int(m.group(2))
        if path not in by_path and path not in tree.fileset and path not in extra_known:
            return f"'{e}': file is neither in the PR nor in the head tree"
        n = lines(path)
        if n is not None and not 1 <= ln <= n:
            return f"'{e}': line {ln} is outside the file (1-{n})"
        return None
    return check


def apply_enrich(enrich: dict, nodes: list[dict], meta: dict, check) -> list[str]:
    """Merge the agent's enrich.json. Returns every problem found (empty list = well-formed)."""
    problems = []
    by_id = {n["id"]: n for n in nodes}

    def ev_ok(ev, where):
        if not isinstance(ev, list) or not ev:
            problems.append(f"{where}: needs a non-empty evidence list of 'path:line'.")
            return False
        bad = [p for p in (check(e) for e in ev) if p]
        problems.extend(f"{where}: {p}" for p in bad)
        return not bad

    unknown = set(k for k in enrich if not k.startswith("_")) - TOP_KEYS
    if unknown:
        problems.append(f"top level: unknown key(s) {sorted(unknown)}; allowed: {sorted(TOP_KEYS)}.")
    blocks = enrich.get("blocks") or {}
    for bid, b in blocks.items():
        if bid not in by_id:
            problems.append(f"blocks.{bid}: unknown id. Copy ids from enrich.skeleton.json.")
            continue
        n, where = by_id[bid], f"blocks.{bid}"
        n.setdefault("_agent", set())
        bad_keys = set(k for k in b if not k.startswith("_")) - BLOCK_KEYS
        if bad_keys:
            problems.append(f"{where}: unknown key(s) {sorted(bad_keys)}; allowed: {sorted(BLOCK_KEYS)}.")
        if n["kind"] == "group" and (b.get("mustRead") or b.get("reviewOrder")):
            problems.append(f"{where}: groups cannot be must-read or in the review path; mark their blocks instead.")
        if b.get("label"):
            n["label"] = str(b["label"]).strip()
            n["_agent"].add("label")
        s = b.get("summary")
        if s:
            if not s.get("text") or s.get("source") not in ("extracted", "inferred"):
                problems.append(f"{where}.summary: needs 'text' and 'source' = extracted | inferred.")
            elif ev_ok(s.get("evidence"), f"{where}.summary"):
                n["summary"] = {"text": s["text"], "source": s["source"], "evidence": s["evidence"]}
                n["_agent"].add("summary")
        w = b.get("why")
        if w:
            if not w.get("text") or w.get("source") not in WHY_SOURCES:
                problems.append(f"{where}.why: needs 'text' and 'source' in {list(WHY_SOURCES)}.")
            else:
                n["why"] = {"text": w["text"], "source": w["source"], "ref": w.get("ref", "")}
                n["_agent"].add("why")
        for i, q in enumerate(b.get("questions") or []):
            q = {"text": q} if isinstance(q, str) else q
            if q.get("evidence") and not ev_ok(q["evidence"], f"{where}.questions[{i}]"):
                continue
            n["questions"].append({"text": q.get("text", ""), "evidence": q.get("evidence", []), "source": "inferred"})
        for i, r in enumerate(b.get("risks") or []):
            if (not r.get("type") or not r.get("reason") or r.get("severity", "medium") not in SEVERITY_WEIGHT
                    or r.get("source", "inferred") not in ("extracted", "inferred")):
                problems.append(f"{where}.risks[{i}]: needs 'type', 'reason', severity high | medium | low and "
                                f"source extracted | inferred.")
            elif ev_ok(r.get("evidence"), f"{where}.risks[{i}]"):
                n["risk"].append({"type": r["type"], "severity": r.get("severity", "medium"), "reason": r["reason"],
                                  "evidence": r["evidence"], "source": r.get("source", "inferred"), "by": "agent"})
        n["risk"].sort(key=lambda r: (-SEVERITY_WEIGHT[r["severity"]], r.get("by") == "agent", r["type"]))
    leaf = [n for n in nodes if n["kind"] != "group" and n["status"] != "affected"]
    if any("reviewOrder" in b or "mustRead" in b for b in blocks.values()):
        for n in leaf:
            n["mustRead"], n["reviewOrder"] = bool(blocks.get(n["id"], {}).get("mustRead")), None
        chosen = sorted((b["reviewOrder"], bid) for bid, b in blocks.items()
                        if b.get("reviewOrder") and bid in by_id and by_id[bid] in leaf)
        rest = sorted([n for n in leaf if n["id"] not in {bid for _, bid in chosen}],
                      key=lambda n: (LAYER_ORDER.index(n["layer"]), n["generated"], n["id"]))
        for i, nid in enumerate([bid for _, bid in chosen] + [n["id"] for n in rest], 1):
            by_id[nid]["reviewOrder"] = i
        meta["_agentPath"] = True
    if enrich.get("ticket"):
        meta["ticket"] = enrich["ticket"]
    if enrich.get("headline"):
        meta["headline"] = enrich["headline"]
        meta["_agentHeadline"] = True
    w = enrich.get("why")
    if w:
        if not w.get("text") or w.get("source") not in WHY_SOURCES:
            problems.append(f"why: needs 'text' and 'source' in {list(WHY_SOURCES)}.")
        else:
            meta["why"] = {"text": w["text"], "source": w["source"], "ref": w.get("ref", "")}
            meta["_agentWhy"] = True
    for i, q in enumerate(enrich.get("questions") or []):
        if q.get("block") and q["block"] not in by_id:
            problems.append(f"questions[{i}]: unknown block '{q['block']}'.")
        elif q.get("text") and ev_ok(q.get("evidence"), f"questions[{i}]"):
            meta["questions"].append({"text": q["text"], "block": q.get("block"), "evidence": q["evidence"],
                                      "source": q.get("source", "inferred")})
        elif not q.get("text"):
            problems.append(f"questions[{i}]: needs 'text'.")
    meta["readmeNotes"] = enrich.get("readme", [])
    return problems


def refresh_groups(nodes: list[dict]) -> None:
    """After enrich: group must-read marks and default summaries follow their blocks."""
    kids = defaultdict(list)
    for n in nodes:
        if n["parent"]:
            kids[n["parent"]].append(n)

    def leaves(g):
        return [x for k in kids[g["id"]] for x in (leaves(k) if k["kind"] == "group" else [k])]
    for g in (n for n in nodes if n["kind"] == "group"):
        ls = leaves(g)
        g["mustRead"] = any(x["mustRead"] for x in ls)
        if "summary" not in g.get("_agent", ()):
            g["summary"]["text"] = f"{len(ls)} blocks inside: " + ", ".join(x["label"] for x in ls[:5]) + (
                " …" if len(ls) > 5 else "")


def agent_checks(nodes: list[dict], meta: dict, problems: list[str]) -> list[dict]:
    """What a finished agent part must satisfy. Each failing check carries a fix hint."""
    checks = []

    def add(cid, name, ok, detail, fix):
        checks.append({"id": cid, "name": name, "passed": bool(ok), "detail": detail, "fix": "" if ok else fix})

    leaf = [n for n in nodes if n["kind"] != "group" and n["status"] != "affected"]
    eligible = [n for n in leaf if not n["generated"] and n["kind"] != "test"]
    must = sorted([n for n in leaf if n["mustRead"]], key=lambda n: n["reviewOrder"] or 999)
    top_groups = [n for n in nodes if n["kind"] == "group" and not n["parent"]]
    agent = lambda n, k: k in n.get("_agent", ())  # noqa: E731
    add("A0", "enrich.json is well-formed", not problems, f"{len(problems)} problem(s)" if problems else "ok",
        "Fix each problem listed under 'Fix these first'.")
    h = meta.get("headline") or ""
    add("A1", "headline says what the PR is for", meta.get("_agentHeadline") and 20 <= len(h) <= 300,
        f"{len(h)} characters" if h else "missing",
        "Set 'headline': one or two plain sentences (20-300 characters): what the PR does and what it touches.")
    add("A2", "the PR-level why has a source", meta.get("_agentWhy"),
        "set" if meta.get("_agentWhy") else "not set in enrich.json (the page shows a PR-body excerpt meanwhile)",
        f"Set 'why': {{'text', 'source' in {list(WHY_SOURCES)}, 'ref'}}. Use the PR body if it states the reason; "
        "otherwise look in commits, ticket, docs and knowledge bases; if nothing states it, write your best "
        "reading with source 'inferred'.")
    need = {n["id"]: n for n in must}
    need.update({n["id"]: n for n in leaf if not n["generated"]
                 and any(r["severity"] in ("high", "medium") for r in n["risk"])})
    if len(leaf) <= MAX_TOP_LEVEL:
        need.update({n["id"]: n for n in leaf if not n["generated"]})
    need.update({n["id"]: n for n in top_groups})
    missing = [i for i, n in need.items() if not agent(n, "summary")]
    add("A3", "summaries for the blocks that matter", not missing, f"{len(need) - len(missing)} of {len(need)}",
        "Add blocks.<id>.summary for: " + ", ".join(missing[:12]) + (" …" if len(missing) > 12 else "")
        + ". Required: must-read blocks, flagged blocks, top-level groups, and every block when there are 12 or fewer.")
    lo, hi = (2, 4) if len(eligible) >= 2 else (min(1, len(eligible)), 1)
    weak = [n["id"] for n in must if not agent(n, "why") or not (n["risk"] or n["questions"])]
    add("A4", f"{lo}-{hi} must-read blocks, each with a why and a risk or question",
        lo <= len(must) <= hi and not weak and meta.get("_agentPath"),
        f"{len(must)} must-read" + (f"; incomplete: {', '.join(weak)}" if weak else "")
        + ("" if meta.get("_agentPath") else "; mustRead/reviewOrder not in enrich.json"),
        f"Set mustRead and reviewOrder in enrich.json (copying the skeleton's values counts as your decision; change "
        f"them where you disagree). {lo}-{hi} blocks must be must-read, each with a 'why' and a risk or question.")
    first = sorted(n["reviewOrder"] for n in must if n["reviewOrder"])
    add("A5", "must-read blocks come first in the review path", first == list(range(1, len(must) + 1)),
        f"must-read at steps {first}", "Give the must-read blocks reviewOrder 1..k, the rest after them.")
    add("A6", "every cited line exists", not any("outside the file" in p or "neither in the PR" in p for p in problems),
        "ok" if not problems else "see A0", "Cite path:line pairs that exist at the PR head (or in the diff).")
    labels = Counter(n["label"] for n in nodes)
    bad = [n["label"] for n in nodes if len(n["label"].split()) > 3] + [k for k, v in labels.items() if v > 1]
    add("A7", "labels: max 3 words, all different", not bad, "ok" if not bad else ", ".join(sorted(set(bad))[:8]),
        "Shorten or rename those labels in blocks.<id>.label.")
    qs = meta.get("questions") or []
    add("A8", "1-5 questions for the whole PR, each with evidence", 1 <= len(qs) <= 5, f"{len(qs)} question(s)",
        "Add 1-5 entries to 'questions': {text, block, evidence}.")
    kids = defaultdict(list)
    for n in nodes:
        if n["parent"]:
            kids[n["parent"]].append(n)

    def paths(n):
        return {f["path"] for f in n["files"]} | {p for k in kids[n["id"]] for p in paths(k)}
    off = [n["id"] for n in nodes if agent(n, "summary")
           and not any(str(e).rsplit(":", 1)[0] in paths(n) for e in n["summary"]["evidence"])]
    add("A9", "each summary cites its own block's files", not off, "ok" if not off else ", ".join(off[:8]),
        "Cite at least one file of the block itself in its summary evidence.")
    texts = [meta.get("headline") or "", (meta.get("why") or {}).get("text", "")] + [q["text"] for q in qs]
    for n in nodes:
        texts += [n["label"]] + ([n["summary"]["text"]] if agent(n, "summary") else [])
        texts += [(n["why"] or {}).get("text", "")] + [q["text"] for q in n["questions"]]
        texts += [r["reason"] for r in n["risk"] if r.get("by") == "agent"]
    ph = [t[:60] for t in texts if PLACEHOLDER_RE.search(t or "")]
    add("A10", "no placeholders left", not ph, "ok" if not ph else "; ".join(ph[:3]),
        "Replace TODO/TBD/??? text with real content or remove it.")
    return checks


def skeleton(nodes: list[dict], meta: dict) -> dict:
    kids = defaultdict(list)
    for n in nodes:
        if n["parent"]:
            kids[n["parent"]].append(n["id"])
    blocks = {}
    for n in sorted(nodes, key=lambda n: (n["kind"] != "group", n["reviewOrder"] or 999, n["id"])):
        b = {"label": n["label"]}
        if n["kind"] == "group":
            b["_children"] = kids[n["id"]]
        else:
            b["_files"] = [f["path"] for f in n["files"]][:12]
            b["_flags"] = [f"{r['severity']} {r['type']}: {r['reason']}" for r in n["risk"]]
        b.update({"summary": None, "why": None, "questions": [], "risks": []})
        if n["kind"] != "group" and n["status"] != "affected":
            b.update({"mustRead": n["mustRead"], "reviewOrder": n["reviewOrder"]})
        blocks[n["id"]] = b
    return {"_schema": "enrich.schema.json (next to make_map.py)", "ticket": meta.get("ticket", ""), "headline": "",
            "why": None, "questions": [], "readme": [], "blocks": blocks}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve_context(tree: Tree, files: list[dict], repo: str, meta: dict) -> dict:
    """Where an agent should look for the 'why': repo docs, project docs, overlay sources (e.g. a knowledge base)."""
    docs = [p for p in tree.files if any(rx.search(p) for rx in R.docs)][:20]
    if R.follow_links:  # one level of local Markdown links from those docs (CONTRIBUTING -> development.md)
        linked = []
        for d in list(docs):
            base = d.rsplit("/", 1)[0] if "/" in d else ""
            for target in re.findall(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)", tree.read(d) or ""):
                if re.match(r"^[a-z]+:", target):
                    continue
                path = posixpath.normpath(posixpath.join(base, target))
                if path in tree.fileset and path not in docs and path not in linked:
                    linked.append(path)
        docs += linked[:12]
    proj_docs = sorted({f"{f['project']}/{p.rsplit('/', 1)[-1]}" if f["project"] else p for f in files
                        for p in tree.files if p.rsplit("/", 1)[0] == f["project"] and "/" in p
                        and any(rx.search(p.rsplit("/", 1)[-1]) for rx in R.project_docs)} - set(docs))[:12]
    ids = [t["id"] for t in meta.get("tickets", [])]
    branch_folder = (meta.get("head") or "").replace("/", "-")
    sources, output = [], None
    for s in R.sources:
        root = Path(s["path"]).expanduser()
        e = {"name": s["name"], "pack": s["pack"], "path": str(root), "exists": root.exists(),
             "search": s.get("search", ""), "rules": s.get("rules", []), "readFirst": [], "repoFolders": [],
             "ticketFolders": []}
        if root.exists():
            e["readFirst"] = [str(root / x) for x in s.get("readFirst", []) if (root / x).exists()]
            for rf in s.get("repoFolders", []):
                base = root / rf
                if base.is_dir():
                    e["repoFolders"] += [str(p) for p in base.iterdir() if p.is_dir() and norm(p.name) == norm(repo.split("/")[-1])]
            for tf in s.get("ticketFolders", []):
                base = root / tf
                if base.is_dir():
                    e["ticketFolders"] += [str(p) for p in sorted(base.iterdir()) if p.is_dir() and (
                        any(i.lower() in p.name.lower() for i in ids) or p.name == branch_folder)]
        sources.append(e)
    for o in R.outputs:
        root = Path(o["sourcePath"]).expanduser()
        hit = next((Path(p) for e in sources for p in e["ticketFolders"]
                    if Path(p).parent.name == o.get("whenTicketFolder")), None)
        if hit:
            output = o["path"].format(source=str(root), folder=hit.name, repoName=repo.split("/")[-1], pr=meta["pr"])
    return {"docs": docs, "projectDocs": proj_docs, "sources": sources, "suggestedOutput": output}


def hunk_ranges(f: dict) -> list[str]:
    """Line ranges to read; deleted files use old-file line numbers (marked)."""
    out, old = [], f["status"] == "deleted"
    for h in f["hunks"][:6]:
        n = sum(1 for line in h["lines"] if line[:1] in (" -" if old else " +"))
        start = h["oldStart"] if old else h["startLine"]
        out.append(f"{f['path']}:{start}-{start + max(n, 1) - 1}" + (" (old file)" if old else ""))
    return out


def write_brief(out: Path, graph: dict, ctx: dict, extra: dict, cmd: str) -> None:
    """agent-brief.md: the agent's task file. Same structure for every repo and PR."""
    meta, nodes, edges = graph["meta"], graph["nodes"], graph["edges"]
    ag = meta["agent"]
    by_id = {n["id"]: n for n in nodes}
    leaf = sorted([n for n in nodes if n["kind"] != "group" and n["status"] != "affected"],
                  key=lambda n: n["reviewOrder"] or 999)
    L = [f"# Agent brief: {meta['repo']} #{meta['pr']}", "", f"**{meta['title']}**", "",
         f"Agent part: **{ag['status'].upper()}**", ""]
    failing = [c for c in ag["checks"] if not c["passed"]]
    if ag["problems"] or failing:
        L += ["## Fix these first", ""]
        L += [f"- {p}" for p in ag["problems"][:40]]
        L += [f"- {c['id']} {c['name']}: {c['detail']}. Fix: {c['fix']}" for c in failing if c["id"] != "A0"]
        L += [""]
    L += ["## Your job", "",
          "1. Read this brief, then the context sources below. The brief only points; the code is the truth.",
          "2. Read the code of every must-read candidate (hunk ranges per block) and of every high flag.",
          "3. Write `enrich.json` in this folder. Start from `enrich.skeleton.json`; the format is "
          "`enrich.schema.json` (next to `make_map.py`).",
          f"4. Re-run until it prints `agent part: PASSED`:  `{cmd}`  (last run: add `--final`)",
          "5. Look at the page (screenshots with `?sel=<id>`, `?open=<groupId>`) and run `check_page.mjs`.", "",
          "## Rules for what you write", "",
          "- Plain English, short sentences. Many reviewers read English as a second language.",
          "- Every statement cites `path:line` that exists at the PR head. For a deleted file, cite the line in the "
          "old file (the brief marks those ranges \"old file\").",
          "- Summaries and risks carry a source: read it in the code = `extracted`, concluded it = `inferred`.",
          f"- `why` sources: {', '.join(WHY_SOURCES)}. Never present a guess as a fact.",
          "- Labels: max 3 words, all different, words a reviewer of this repo would use (docs and KB names first).",
          "- Must-read: 2-4 blocks where a mistake hurts most (auth, data, money, public contracts, infra, "
          "irreversible steps). Each needs a `why` and at least one risk or question. They come first in `reviewOrder`.",
          "- Questions: 1-5 for the whole PR, answerable from the cited lines. Ask, don't accuse.",
          "- Do not call the PR safe. Do not post anything anywhere. No secrets, tokens or customer data.", "",
          "## Acceptance checks (enforced by the generator)", "", "| Id | Check | Now |", "|---|---|---|"]
    L += [f"| {c['id']} | {c['name']} | {'✓' if c['passed'] else '✗ ' + c['detail']} |" for c in ag["checks"]]
    L += ["", "## PR at a glance", "",
          f"- {meta['base']} ← {meta['head']} · {meta.get('state', '').lower()} · {meta.get('author', '')}",
          f"- {meta['totals']['files']} files · +{meta['totals']['additions']} −{meta['totals']['deletions']}"]
    for t in meta.get("tickets", []):
        L.append(f"- Ticket {t['id']}" + (f" ({t['url']})" if t["url"] else ""))
    for i in extra.get("issues", []):
        L.append(f"- Linked issue #{i.get('number')}: {i.get('title', '')}")
    if extra.get("commits"):
        L += ["- Commits:"] + [f"  - {c}" for c in extra["commits"][:30]]
    if extra.get("body"):
        L += ["", "PR description (excerpt):", "", "> " + "\n> ".join(extra["body"][:1500].splitlines())]
    L += ["", "## Context sources (read before writing any 'why')", ""]
    L += ["- PR description and commits above (source `pr-body` / `commits`)."]
    L += [f"- Repo doc: `{p}` (source `docs`)" for p in ctx["docs"]]
    L += [f"- Project doc near the change: `{p}` (source `docs`)" for p in ctx["projectDocs"]]
    for s in ctx["sources"]:
        L += [f"- **{s['name']}** (pack `{s['pack']}`): `{s['path']}`" + ("" if s["exists"] else " (not found on this machine)")]
        L += [f"  - Read first: `{p}`" for p in s["readFirst"]]
        L += [f"  - This repo's folder: `{p}`" for p in s["repoFolders"]] or (["  - No folder for this repo (say \"not in the KB\")."] if s["exists"] else [])
        L += [f"  - Ticket folder: `{p}`" for p in s["ticketFolders"]]
        if s["search"]:
            L.append(f"  - Search: {s['search']}")
        L += [f"  - {r}" for r in s["rules"]]
    if ctx["suggestedOutput"]:
        L += ["", f"Suggested output folder (overlay rule): `{ctx['suggestedOutput']}`"]
    flags = Counter(f"{r['severity']} {r['type']}" for n in leaf for r in n["risk"] if r["source"] == "rule")
    br = meta["blastRadius"]
    L += ["", "## What the rules found", "",
          "- Rule packs: " + ", ".join(f"{p['name']} ({p['source']})" for p in meta["profiles"]),
          "- Flags: " + (", ".join(f"{k} ×{v}" for k, v in sorted(flags.items())) or "none"),
          f"- Blast radius: {br['affectedFiles']} unchanged file(s) reference changed symbols"
          + (" (capped)" if br["capped"] else ""),
          "- Deviations: " + ("; ".join(meta["deviations"]) or "none")]
    detailed = {n["id"] for n in leaf if n["mustRead"] or any(r["severity"] in ("high", "medium") for r in n["risk"])}
    detailed |= {n["id"] for n in leaf[:25]}
    L += ["", f"## Blocks ({len(leaf)}, in the current review order; MUST-READ = rule default until you choose)", ""]
    for n in leaf:
        tag = " · **MUST-READ candidate**" if n["mustRead"] else ""
        grp = f" · in group `{n['parent']}`" if n["parent"] else ""
        L.append(f"### {n['reviewOrder']}. {n['label']} · `{n['id']}` · {n['kind']}, {n['status']}, "
                 f"+{n['stats']['additions']} −{n['stats']['deletions']}{tag}{grp}")
        if n["id"] not in detailed:
            L += [f"- {len(n['files'])} file(s), e.g. `{n['files'][0]['path']}`", ""]
            continue
        L += [f"- File: `{f['path']}` (+{f['additions']} −{f['deletions']}, {f['status']}"
              f"{', generated' if f['generated'] else ''})" for f in n["files"][:12]]
        if len(n["files"]) > 12:
            L.append(f"- … {len(n['files']) - 12} more files")
        L += [f"- Flag: {r['severity']} {r['type']}: {r['reason']} ({', '.join(r['evidence'][:3])})" for r in n["risk"]]
        for e in edges:
            if e["from"] == n["id"] and e["to"] in by_id:
                L.append(f"- Uses → `{e['to']}` ({e['source']}{': ' + ', '.join(e['symbols'][:4]) if e.get('symbols') else ''})")
            elif e["to"] == n["id"] and e["from"] in by_id:
                L.append(f"- Used by ← `{e['from']}` ({e['source']})")
        big = sorted(n["files"], key=lambda f: -(f["additions"] + f["deletions"]))
        L += [f"- Read: `{r}`" for f in big[:6] for r in hunk_ranges(f)][:10]
        L.append("")
    groups = [n for n in nodes if n["kind"] == "group"]
    if groups:
        L += ["## Groups (top-level groups need a summary)", ""]
        L += [f"- `{g['id']}` {g['label']} (level {g['level']}): "
              + ", ".join(f"`{n['id']}`" for n in nodes if n["parent"] == g["id"]) for g in groups]
        L.append("")
    L += ["## enrich.json shape (minimal)", "", "```json", json.dumps({
        "headline": "One or two sentences: what the PR does and what it touches.",
        "why": {"text": "Why the change was made.", "source": "pr-body", "ref": "PR description"},
        "questions": [{"text": "A question a reviewer should ask.", "block": leaf[0]["id"] if leaf else "<id>",
                       "evidence": ["path/to/file.cs:42"]}],
        "readme": ["Notes for the README: KB corrections, deviations."],
        "blocks": {leaf[0]["id"] if leaf else "<id>": {
            "label": "Max three words", "summary": {"text": "What changed, one sentence.", "source": "extracted",
                                                    "evidence": ["path/to/file.cs:42"]},
            "why": {"text": "Why this part changed.", "source": "inferred", "ref": ""},
            "risks": [{"type": "error-handling", "severity": "medium", "reason": "What could break.",
                       "evidence": ["path/to/file.cs:50"]}],
            "questions": ["Block-level question."], "mustRead": True, "reviewOrder": 1}}}, indent=2), "```", ""]
    (out / "agent-brief.md").write_text("\n".join(L), encoding="utf-8")


# ----------------------------------------------------------------------------- grouping (> 12 blocks)

def cluster(nodes: list[dict], shorts: dict) -> list[dict]:
    """Nest blocks into groups so no level shows more than MAX_TOP_LEVEL blocks."""
    changed = [n for n in nodes if n["status"] != "affected"]
    affected = [n for n in nodes if n["status"] == "affected"]
    groups, used = [], {n["id"] for n in nodes}
    layer_label = {"entry": "Entry points", "logic": "Logic", "data": "Data", "infra": "Infra & config",
                   "tests": "Tests"}
    options = [("project", lambda n: n["project"] or "root"), ("top", lambda n: n["top"] or "root"),
               ("dir1", lambda n: f"{n['project'] or 'root'}|" + (n["dirParts"][0] if n.get("dirParts") else "(root)")),
               ("dir2", lambda n: f"{n['project'] or 'root'}|" + ("/".join(n["dirParts"][:2]) if n.get("dirParts")
                                                                    else "(root)")),
               ("layer", lambda n: n["layer"])]

    def make_group(key, label, members, parent, level):
        gid = base = "group-" + slug(key)
        i = 2
        while gid in used:
            gid, i = f"{base}-{i}", i + 1
        used.add(gid)
        weights = Counter()
        for m in members:
            weights[m["layer"]] += max(1, m["stats"]["files"])
        groups.append({
            "id": gid, "label": label, "kind": "group", "level": level, "parent": parent,
            "layer": weights.most_common(1)[0][0],
            "status": "affected" if all(m["status"] == "affected" for m in members)
            else status_of(m["status"] for m in members if m["status"] != "affected"),
            "stats": {k: sum(m["stats"].get(k, 0) for m in members) for k in ("files", "additions", "deletions")},
            "risk": [], "summary": {"text": f"{len(members)} blocks inside: " + ", ".join(m["label"] for m in members[:5])
                                            + (" …" if len(members) > 5 else ""), "source": "extracted",
                                    "evidence": [m["summary"]["evidence"][0] for m in members][:6]},
            "why": None, "files": [], "mustRead": any(m["mustRead"] for m in members), "reviewOrder": None,
            "questions": [], "generated": all(m["generated"] for m in members), "project": None, "top": None,
            "dirParts": []})
        return gid

    def place(members, parent, level, budget, tried):
        if len(members) <= budget:
            for m in members:
                m["parent"], m["level"] = parent, level
            return
        chosen, too_fine, cands = None, None, []
        for idx, (name, fn) in enumerate(options):
            if name in tried:
                continue
            buckets = defaultdict(list)
            for m in members:
                buckets[fn(m)].append(m)
            if 1 < len(buckets) <= budget:
                balanced = max(len(v) for v in buckets.values()) <= 0.75 * len(members)
                cands.append((balanced, len(buckets), -idx, name, buckets))
            elif len(buckets) > budget and too_fine is None:
                too_fine = buckets
        if cands:
            best = max(cands, key=lambda c: c[:3])
            if best[0] or too_fine is None:
                chosen = (best[3], best[4])
        if chosen is None:  # too many folders: contiguous alphabetical ranges of them, balanced by size
            if too_fine is None:
                too_fine = {m["id"]: [m] for m in members}
            keys = sorted(too_fine)
            target = len(members) / budget
            ranges, cur, count = {}, [], 0
            for i, k in enumerate(keys):
                cur.append(k)
                count += len(too_fine[k])
                left = len(keys) - i - 1
                if count >= target and left >= budget - len(ranges) - 1 and len(ranges) < budget - 1 or not left:
                    ranges[(cur[0], cur[-1])] = [m for kk in cur for m in too_fine[kk]]
                    cur, count = [], 0
            chosen = ("range", ranges)
        name, buckets = chosen
        prefix = f"{parent}-" if parent else ""
        for key, ms in sorted(buckets.items()):
            if len(ms) == 1:
                ms[0]["parent"], ms[0]["level"] = parent, level
                continue
            if name == "layer":
                label = layer_label[key]
            elif name == "range":
                a, b = ((humanize(k.split("|")[-1].rsplit("/", 1)[-1]) or ["?"])[0] for k in key)
                label = a if a == b else f"{a} – {b}"
                key = f"{key[0]}-{key[1]}"
            elif key.split("|")[-1] in ("(root)", "root"):
                label = "Top level"
            else:
                tail = key.split("|")[-1]
                label = " ".join(humanize(shorts.get(tail, tail.rsplit("/", 1)[-1]))[-3:]) or "Top level"
            gid = make_group(prefix.replace("group-", "") + f"{name}-{key}", label, ms, parent, level)
            place(ms, gid, level + 1, MAX_TOP_LEVEL, tried | {name})

    budget = MAX_TOP_LEVEL
    if len(affected) > 3:
        gid = make_group("affected", "Affected code", affected, None, 1)
        place(affected, gid, 2, MAX_TOP_LEVEL, {"project", "top"})
        budget -= 1
    else:
        budget -= len(affected)
        for m in affected:
            m["parent"], m["level"] = None, 1
    place(changed, None, 1, budget, set())
    return groups


# ----------------------------------------------------------------------------- self-check

EXTERNAL_RE = re.compile(r"""(?:\bsrc|\bhref)\s*=\s*["']?\s*(?:https?:)?//|@import\s+(?:url\()?\s*["']?\s*(?:https?:)?//|"""
                         r"""url\(\s*["']?\s*(?:https?:)?//""", re.I)


def self_check(graph: dict, rest_files: list[dict], html: str | None) -> list[dict]:
    meta, nodes, edges = graph["meta"], graph["nodes"], graph["edges"]
    leaf = [n for n in nodes if n["kind"] != "group" and n["status"] != "affected"]
    checks = []

    def add(cid, name, ok, detail):
        checks.append({"id": cid, "name": name, "passed": bool(ok), "detail": detail})

    mapped = [f["path"] for n in leaf for f in n["files"]]
    want = {f["filename"] for f in rest_files}
    add(1, "every PR file is in exactly one block",
        len(mapped) == meta["totals"]["files"] == len(want) and set(mapped) == want,
        f"{len(mapped)} mapped of {meta['totals']['files']} (GitHub lists {len(want)})")
    a = sum(f["additions"] for n in leaf for f in n["files"])
    d = sum(f["deletions"] for n in leaf for f in n["files"])
    mism = [x for x in meta["deviations"] if "parsed +" in x or "missing from the diff" in x]
    add(2, "line totals match GitHub", a == meta["totals"]["additions"] and d == meta["totals"]["deletions"]
        and not mism, f"+{a} −{d} vs +{meta['totals']['additions']} −{meta['totals']['deletions']}"
        + (f"; {len(mism)} diff-parse deviation(s)" if mism else ""))
    missing = [n["id"] for n in nodes if not n["summary"].get("evidence")]
    missing += [f"{e['from']}->{e['to']}" for e in edges if not e.get("evidence")]
    missing += [f"{n['id']}:{r['type']}" for n in nodes for r in n["risk"] if not r.get("evidence")]
    all_ev = [e for n in nodes for e in n["summary"].get("evidence", [])] + [e for n in nodes for r in n["risk"]
                                                                              for e in r.get("evidence", [])]
    all_ev += [e for x in edges for e in x.get("evidence", [])]
    missing += [f"bad line {e}" for e in all_ev if not re.fullmatch(r".+:[1-9]\d*", str(e))]
    add(3, "every block, edge and flag has evidence", not missing,
        "ok" if not missing else "missing: " + ", ".join(missing[:8]))
    owners = defaultdict(list)
    for n in nodes:
        for f in n["files"]:
            owners[f["path"]].append(n["id"])
    shared = [p for p, ids in owners.items() if len(ids) > 1]
    add(4, "no file in two blocks", not shared, "ok" if not shared else ", ".join(shared[:5]))
    aff = [n for n in nodes if n["status"] == "affected" and n["kind"] != "group"]
    bad = [n["id"] for n in aff if not any(e["from"] == n["id"] and e["source"] == "affected" for e in edges)
           or not any(h for f in n["files"] for h in f["hunks"])]
    add(5, "every affected block references a changed symbol", not bad,
        f"{len(aff)} affected block(s)" if not bad else "bad: " + ", ".join(bad))
    ext = EXTERNAL_RE.findall(html) if html is not None else []
    add(6, "page loads nothing from the network", html is not None and not ext,
        "no external src/href/@import" if not ext else f"{len(ext)} external reference(s)")
    long_labels = [n["label"] for n in nodes if len(n["label"].split()) > 3]
    add(7, "labels have at most 3 words", not long_labels, "ok" if not long_labels else ", ".join(long_labels))
    must = [n["id"] for n in nodes if n["mustRead"] and n["kind"] != "group"]
    add(8, "1 to 4 must-read blocks", 1 <= len(must) <= 4 or len(leaf) < 2, f"{len(must)} must-read")
    return checks


# ----------------------------------------------------------------------------- output

def embed_json(graph: dict) -> str:
    s = json.dumps(graph, ensure_ascii=False, separators=(",", ":"))
    for a, b in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("'", "\\u0027"), ("http", "\\u0068ttp"),
                 ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        s = s.replace(a, b)
    return s


def render(graph: dict) -> str:
    tpl = (HERE / "page.html").read_text(encoding="utf-8")
    title = f"PR map · {graph['meta']['repo'].split('/')[-1]} #{graph['meta']['pr']}"
    title = title.replace("&", "&amp;").replace("<", "&lt;")
    return tpl.replace("__TITLE__", title).replace("__GRAPH_JSON__", embed_json(graph))


def clean_body(body: str) -> str:
    body = re.sub(r"<!--.*?-->|<[^>]+>|!\[[^\]]*\]\([^)]*\)|\[\[\d+\]\]\([^)]*\)", "", body or "", flags=re.S)
    body = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)
    keep = [ln for ln in body.splitlines() if not re.match(r"\s*- \[[ xX]\]|\s*\[[^\]]+\]:\s*\S+", ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()


def summary_lines(graph: dict) -> list[str]:
    nodes, edges, meta = graph["nodes"], graph["edges"], graph["meta"]
    leaf = [n for n in nodes if n["kind"] != "group" and n["status"] != "affected"]
    aff = [n for n in nodes if n["status"] == "affected" and n["kind"] != "group"]
    groups = [n for n in nodes if n["kind"] == "group"]
    flags = Counter(r["type"] for n in nodes for r in n["risk"] if r.get("by") != "agent")
    agent_risks = Counter(r["type"] for n in nodes for r in n["risk"] if r.get("by") == "agent")
    src = Counter(e["source"] for e in edges)
    sc = meta["selfCheck"]
    return [
        f"map: {meta['repo']} #{meta['pr']} — {meta['title']}",
        f"blocks: {len(leaf)} changed + {len(aff)} affected, {len(groups)} group(s) · edges: {len(edges)} "
        f"({', '.join(f'{v} {k}' for k, v in sorted(src.items())) or 'none'})",
        f"files mapped: {meta['coverage']['mapped']} of {meta['coverage']['total']} · "
        f"+{meta['totals']['additions']} −{meta['totals']['deletions']}",
        "rule flags: " + (", ".join(f"{k} {v}" for k, v in sorted(flags.items())) or "none")
        + (" · agent risks: " + ", ".join(f"{k} {v}" for k, v in sorted(agent_risks.items())) if agent_risks else ""),
        "must read: " + ", ".join(f"{n['reviewOrder']}. {n['label']}" for n in
                                  sorted([n for n in leaf if n["mustRead"]], key=lambda n: n["reviewOrder"] or 99)),
        f"blast radius: {meta['blastRadius']['affectedFiles']} affected file(s) from "
        f"{len(meta['blastRadius']['symbolsSearched'])} searched symbol(s)"
        + (" (capped)" if meta["blastRadius"]["capped"] else ""),
        f"self-check: {'PASSED' if sc['passed'] else 'FAILED'} "
        f"({sum(c['passed'] for c in sc['checks'])}/{len(sc['checks'])})",
        "rule packs: " + ", ".join(p["name"] for p in meta["profiles"]),
        f"agent part: {meta['agent']['status'].upper()}"
        + (f" ({sum(c['passed'] for c in meta['agent']['checks'])}/{len(meta['agent']['checks'])})"
           if meta["agent"]["status"] != "missing" else ""),
    ]


def write_readme(out: Path, graph: dict, cmd: str) -> None:
    meta = graph["meta"]
    lines = [f"# PR map: {meta['repo']} #{meta['pr']}", "",
             f"{meta['title']}", "",
             "Open `index.html` (double-click; works offline, no network requests).", "",
             "## What it shows", "",
             "Blocks of changed files grouped by project, feature folder and kind, laid out "
             "Entry → Logic → Data → Infra with a Tests lane. Edges are type references found in the code "
             "(solid), links inferred from URLs or config keys (dashed) and unchanged code that references "
             "changed symbols (dotted, \"affected\"). Rule flags come from the rule packs; risks an agent added "
             "are marked as the agent's, and anything concluded rather than read is labelled inferred. Every "
             "statement cites file:line.", "",
             "## Summary (printed by the generator)", "", "```", *summary_lines(graph), "```", "",
             "## Self-check", ""]
    lines += [f"- {'✓' if c['passed'] else '✗'} {c['id']}. {c['name']}: {c['detail']}" for c in meta["selfCheck"]["checks"]]
    ag = meta["agent"]
    lines += ["", f"## Agent part: {ag['status']}", ""]
    if ag["status"] == "missing":
        lines += ["- Draft: no `enrich.json` yet. Summaries, the why and the review path are rule defaults. "
                  "See `agent-brief.md`."]
    else:
        lines += [f"- {'✓' if c['passed'] else '✗'} {c['id']}. {c['name']}: {c['detail']}" for c in ag["checks"]]
    lines += ["", "## Rule packs", ""] + [f"- {p['name']} (`{p['source']}`): {p['description']}" for p in meta["profiles"]]
    lines += ["", "## Deviations", ""]
    lines += [f"- {d}" for d in meta["deviations"]] or ["- None. File count and line totals match GitHub exactly."]
    if meta.get("readmeNotes"):
        lines += ["", "## Notes", ""] + [f"- {x}" for x in meta["readmeNotes"]]
    lines += ["", "## Regenerate", "", "```", cmd + "          # add --final to require the agent part", "```", "",
              "`make_map.py`, `page.html` and `profiles/` here are snapshots; the maintained copies live in the "
              "skill's `templates/` folder. `enrich.json` holds the agent-written part "
              "(labels, summaries, why, review path, questions); `agent-brief.md` is the agent's task file and "
              "`enrich.skeleton.json` lists the block ids.", ""]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Map a pull request as graph.json + index.html")
    ap.add_argument("--repo", help="OWNER/NAME (PR mode)")
    ap.add_argument("--pr", type=int, help="PR number (PR mode)")
    ap.add_argument("--local", help="path of a local clone (branch mode)")
    ap.add_argument("--base", help="base ref (branch mode)")
    ap.add_argument("--head", default="HEAD", help="head ref (branch mode)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--enrich", help="enrich.json (default: OUT/enrich.json if present)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch PR data instead of using OUT/.raw")
    ap.add_argument("--final", action="store_true", help="fail unless the agent part (enrich.json) passes its checks")
    args = ap.parse_args()
    out = Path(args.out).expanduser().resolve()
    deviations = []
    if args.local:
        repo_dir = Path(args.local).expanduser().resolve()
        if not args.base:
            die("--local needs --base")
        if out == repo_dir or repo_dir in out.parents:
            tracked = run(["git", "-C", repo_dir, "ls-files", "--error-unmatch", str(out)], check=False)
            ignored = subprocess.run(["git", "-C", repo_dir, "check-ignore", "-q", str(out)]).returncode == 0
            if tracked or not ignored:
                die("--out must not be inside a tracked path of the repo under review.")
        head_oid = run(["git", "-C", repo_dir, "rev-parse", args.head]).strip()
        base_oid = run(["git", "-C", repo_dir, "merge-base", args.base, head_oid]).strip()
        rest_files = local_files(repo_dir, args.base, head_oid)
        diff_text = run(["git", "-C", repo_dir, "diff", "-M", f"{args.base}...{head_oid}"])
        remote = run(["git", "-C", repo_dir, "remote", "get-url", "origin"], check=False).strip()
        repo = re.sub(r"\.git$", "", remote.split(":")[-1].split("github.com/")[-1]) or repo_dir.name
        a, d = sum(f["additions"] for f in rest_files), sum(f["deletions"] for f in rest_files)
        meta_in = {"number": 0, "title": f"{args.head} vs {args.base}", "body": "", "url": "", "state": "LOCAL",
                   "baseRefName": args.base, "headRefName": args.head, "baseRefOid": base_oid,
                   "headRefOid": head_oid, "additions": a, "deletions": d, "changedFiles": len(rest_files),
                   "author": {"login": ""}, "mergedAt": None}
        tree = Tree(repo_dir, head_oid, worktree=False)
        cmd = f"python3 {HERE / 'make_map.py'} --local {repo_dir} --base {args.base} --head {args.head} --out {out}"
    else:
        if not (args.repo and args.pr):
            die("give --repo and --pr, or --local and --base")
        out.mkdir(parents=True, exist_ok=True)
        data = fetch_pr(args.repo, args.pr, out / ".raw", args.refresh)
        meta_in, rest_files, repo = data["meta"], data["files"], args.repo
        tree_dir = ensure_pr_checkout(repo, args.pr, meta_in["headRefOid"], meta_in["baseRefOid"])
        tree = Tree(tree_dir, meta_in["headRefOid"], worktree=True)
        diff_text = data["diff"]
        if diff_text is None:
            mb = run(["git", "-C", tree_dir, "merge-base", meta_in["baseRefOid"], meta_in["headRefOid"]]).strip()
            diff_text = run(["git", "-C", tree_dir, "diff", "-M", f"{mb}..{meta_in['headRefOid']}"])
            deviations.append("gh pr diff failed (too large?); diff computed locally from merge-base..head.")
        cmd = f"python3 {HERE / 'make_map.py'} --repo {repo} --pr {args.pr} --out {out}"
    out.mkdir(parents=True, exist_ok=True)
    global R, REPO_SHORT
    R = load_rules(tree.files + [f["filename"] for f in rest_files], repo, tree.git_dir, meta_in["baseRefOid"])
    REPO_SHORT = repo.split("/")[-1] or "root"

    diff_files = parse_diff(diff_text)
    files, devs = build_file_facts(diff_files, rest_files, tree)
    deviations += devs
    assign_projects(files, tree)
    fedges = file_edges(files, tree)
    budget = [MAX_LINES_TOTAL]
    nodes, file_block = make_nodes(group_files(files, fedges), budget)
    dedupe_labels(nodes)
    edges = block_edges(fedges, file_block)
    blast = blast_radius(files, tree, file_block, budget)
    apply_risks(nodes, files, file_block, blast["deletedRefs"], tree)
    nodes += blast["nodes"]
    edges += blast["edges"]
    dedupe_labels(nodes)
    default_review_path(nodes)
    if budget[0] <= 0:
        deviations.append(f"Stored diff lines capped at {MAX_LINES_TOTAL}; some hunks are truncated in the page.")

    body = meta_in.get("body") or ""
    desc = re.split(r"\n#+ ", "\n" + body)[1] if "\n#" in "\n" + body else body
    desc = re.sub(r"<[^>]+>|\[\[\d+\]\]\([^)]*\)", "", desc.split("\n", 1)[-1] if desc.startswith("Description") else desc)
    desc = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", desc)
    desc = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", desc)
    desc = " ".join(desc.split())[:400]
    tickets = tickets_of(meta_in["title"], meta_in["headRefName"], body)
    meta = {"repo": repo, "pr": meta_in["number"], "title": meta_in["title"],
            "ticket": tickets[0]["id"] if tickets else "", "tickets": tickets,
            "base": meta_in["baseRefName"], "head": meta_in["headRefName"], "baseSha": meta_in["baseRefOid"],
            "headSha": meta_in["headRefOid"], "url": meta_in.get("url", ""), "state": meta_in.get("state", ""),
            "author": (meta_in.get("author") or {}).get("login", ""), "mergedAt": meta_in.get("mergedAt"),
            "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "generator": GENERATOR,
            "profiles": R.packs,
            "totals": {"files": meta_in["changedFiles"], "additions": meta_in["additions"],
                       "deletions": meta_in["deletions"]},
            "headline": "", "why": {"text": desc, "source": "pr-body", "ref": "PR description"} if desc else None,
            "questions": [], "blastRadius": blast["summary"], "deviations": deviations,
            "selfCheck": {"passed": False, "checks": []}}
    shorts = {}
    for f in files:
        shorts[f["project"]], shorts[f["top"]] = f["projectShort"], f["topShort"]
    nodes += cluster(nodes, shorts)
    for n in nodes:
        n.pop("dirParts", None)

    # the agent part: enrich.json, merged and checked
    enrich_path = Path(args.enrich).expanduser() if args.enrich else out / "enrich.json"
    known = {f["path"] for f in files} | {f["path"] for n in blast["nodes"] for f in n["files"]}
    problems, status = [], "missing"
    if enrich_path.exists():
        try:
            enrich = json.loads(enrich_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            enrich, problems = {}, [f"enrich.json is not valid JSON: {e}"]
        problems += apply_enrich(enrich, nodes, meta, evidence_checker(files, tree, known))
        status = "pending"
    elif args.enrich:
        die(f"enrich file not found: {enrich_path}")
    refresh_groups(nodes)
    achecks = agent_checks(nodes, meta, problems)
    if status == "pending":
        status = "passed" if all(c["passed"] for c in achecks) else "failed"
    meta["agent"] = {"status": status, "checks": achecks, "problems": problems}
    for k in [k for k in meta if k.startswith("_")]:
        meta.pop(k)
    for n in nodes:
        n.pop("_agent", None)

    order = {k: i for i, k in enumerate(KIND_ORDER + ["group"])}
    for i, n in enumerate(sorted(nodes, key=lambda n: (n["top"] or "~", n["project"] or "~",
                                                         order[n["kind"]], n["id"]))):
        n["rank"] = i
    mapped = sum(len(n["files"]) for n in nodes if n["kind"] != "group" and n["status"] != "affected")
    meta["coverage"] = {"mapped": mapped, "total": meta["totals"]["files"]}
    graph = {"schema": SCHEMA, "meta": meta, "nodes": nodes, "edges": edges}

    checks = self_check(graph, rest_files, "")
    meta["selfCheck"] = {"passed": all(c["passed"] for c in checks), "checks": checks}
    html = render(graph)
    checks = self_check(graph, rest_files, html)
    meta["selfCheck"] = {"passed": all(c["passed"] for c in checks), "checks": checks}
    (out / "enrich.skeleton.json").write_text(json.dumps(skeleton(nodes, meta), indent=2, ensure_ascii=False),
                                              encoding="utf-8")
    extra = {"commits": [c.get("messageHeadline", "") for c in meta_in.get("commits") or []],
             "issues": meta_in.get("closingIssuesReferences") or [], "body": clean_body(body)}
    write_brief(out, graph, resolve_context(tree, files, repo, meta), extra, cmd)

    def fail(msg):
        (out / "graph.failed.json").write_text(json.dumps(graph, indent=1, ensure_ascii=False), encoding="utf-8")
        for stale in ("index.html", "graph.json"):  # never leave an older page that looks valid
            if (out / stale).exists():
                (out / stale).unlink()
        die(msg)
    if not meta["selfCheck"]["passed"]:
        failed = "\n".join(f"  ✗ {c['id']}. {c['name']}: {c['detail']}" for c in checks if not c["passed"])
        fail(f"self-check FAILED, page not written:\n{failed}\n(see {out / 'graph.failed.json'})")
    if status == "failed":
        lines = [f"  - {p}" for p in problems[:20]] + [f"  ✗ {c['id']} {c['name']}: {c['detail']}\n      fix: {c['fix']}"
                                                         for c in achecks if not c["passed"] and c["id"] != "A0"]
        fail("agent part FAILED, page not written:\n" + "\n".join(lines) + f"\n(details: {out / 'agent-brief.md'})")
    if status == "missing" and args.final:
        fail(f"agent part MISSING (--final): write {out / 'enrich.json'} as described in {out / 'agent-brief.md'}")
    html = render(graph)
    (out / "graph.json").write_text(json.dumps(graph, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "index.html").write_text(html, encoding="utf-8")
    failed_old = out / "graph.failed.json"
    if failed_old.exists():
        failed_old.unlink()
    for name in ("make_map.py", "page.html", "enrich.schema.json"):
        if (HERE / name).exists() and (HERE / name).resolve() != (out / name).resolve():
            shutil.copyfile(HERE / name, out / name)
    if PROFILE_DIR.resolve() != (out / "profiles").resolve():
        shutil.copytree(PROFILE_DIR, out / "profiles", dirs_exist_ok=True)
    write_readme(out, graph, cmd)
    print("\n".join(summary_lines(graph)))
    if status == "missing":
        print(f"next: write the agent part as described in {out / 'agent-brief.md'}")
    print(f"open: {out / 'index.html'}")


if __name__ == "__main__":
    main()
