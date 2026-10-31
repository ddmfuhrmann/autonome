"""The scribe's second job: keep each feature's living doc and the system overview true to the code.

A headless Claude reads the dossier (current doc, recent handoffs, plans, decisions) and the code
itself (read-only tools), and rewrites the doc. Updates, not rewrites from scratch.
"""

import json
import os
import subprocess

from . import common

MODEL = os.getenv("AUTONOME_DOCS_MODEL", "sonnet")
READ_ONLY = ["Read", "Grep", "Glob", "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)"]

SHAPE = """{
  "overview": "markdown: what this feature is, for whom, its boundaries (5-10 lines)",
  "how_it_works": "markdown: the main flows, where the code lives (paths), the important rules",
  "diagrams": [{"title": "string", "mermaid": "valid mermaid: flowchart, sequenceDiagram, stateDiagram-v2 or erDiagram"}],
  "entities": ["Name: one line"],
  "interfaces": ["endpoint, event, screen or command: one line"],
  "decisions_in_effect": ["decision title: one line"],
  "known_issues": ["one line"]
}"""

FEATURE_PROMPT = """You maintain the living documentation of the feature "{feature}" of project "{project}".
Below is its current doc (may be empty), its recent handoffs, plans and decisions. Read the actual
code in the working directory to confirm what is true; the code wins over any note.

Update the doc so it describes the feature as it is NOW. Keep what is still true, fix what changed,
drop what is gone. 1-3 diagrams that explain the feature (a flow, a state machine for anything with
status, the entities). Mermaid must be valid: quote labels with spaces or symbols, no HTML.
Never include secrets or personal data.

Answer with ONLY one JSON object, no prose, no code fence, in this shape:
{shape}

Dossier:
{dossier}
"""

OVERVIEW_PROMPT = """You maintain the system overview of project "{project}": one page a newcomer reads
first. Below are the current overview (may be empty) and the overview of every feature. Describe the
system as a whole: what it does, its main parts and how features relate. Include a C4-style context
or container diagram as a mermaid flowchart whose nodes are the features and external systems.

Answer with ONLY one JSON object, no prose, no code fence, in this shape:
{shape}

Current overview:
{current}

Features:
{features}
"""


def _ask(prompt: str, cwd: str, tools: list[str]) -> dict:
    out = subprocess.run(
        ["claude", "-p", "--model", MODEL, "--output-format", "json", *(["--allowedTools", *tools] if tools else [])],
        input=prompt, cwd=cwd, capture_output=True, text=True, timeout=900,
        env={**os.environ, "AUTONOME_HANDOFF_CHILD": "1"},
    )
    if out.returncode != 0:
        raise RuntimeError(f"claude -p failed: {out.stderr[-1500:]}")
    result = json.loads(out.stdout)["result"]
    return json.loads(result[result.find("{"): result.rfind("}") + 1])


def update_feature(project: str, feature: str, cwd: str) -> int:
    dossier = common.get_json(f"/api/projects/{project}/features/{feature}/dossier", timeout=30)
    if dossier is None:
        raise RuntimeError("autonome-context is unreachable")
    doc = _ask(FEATURE_PROMPT.format(feature=feature, project=project, shape=SHAPE,
                                     dossier=json.dumps(dossier, ensure_ascii=False)[:60_000]), cwd, READ_ONLY)
    layers = (dossier.get("doc") or {}).get("layers") or []
    result = common.send_json(f"/api/projects/{project}/features/{feature}/doc",
                              {"doc": doc, "layers": layers, "commit_sha": common.git(cwd, "rev-parse", "HEAD")},
                              method="PUT")
    return result["document_id"]


def update_overview(project: str, cwd: str) -> int:
    feats = common.get_json(f"/api/projects/{project}/features", timeout=30) or []
    current = (common.get_json(f"/api/projects/{project}/overview", timeout=30) or {}).get("doc")
    doc = _ask(OVERVIEW_PROMPT.format(project=project, shape=SHAPE, current=json.dumps((current or {}).get("body"), ensure_ascii=False),
                                      features=json.dumps([f for f in feats if f.get("overview")], default=str)[:60_000]),
               cwd, [])
    result = common.send_json(f"/api/projects/{project}/overview",
                              {"doc": doc, "commit_sha": common.git(cwd, "rev-parse", "HEAD")}, method="PUT")
    return result["document_id"]
