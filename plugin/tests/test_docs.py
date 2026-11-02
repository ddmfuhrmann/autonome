"""Run with: uv run --project context pytest plugin/tests"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from autonome import common, docs  # noqa: E402

DOC = {
    "kind": "product",
    "product": {"what_it_does": "Runs tasks."},
    "technical": {"how_it_works": "A loop."},
    "catalog": {"description": "Runs approved tasks.", "paths": ["plugin/runner/src/**"]},
}


def _fake(monkeypatch, doc):
    seen = {"prompts": [], "puts": []}

    def fake_run(cmd, input, **kwargs):
        seen["prompts"].append(input)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"result": json.dumps(doc)}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(common, "project_slug", lambda cwd: "p")
    monkeypatch.setattr(common, "model_args", lambda project, role: [])
    monkeypatch.setattr(common, "language_rule", lambda project: "")
    monkeypatch.setattr(common, "git", lambda cwd, *args: "abc123")
    monkeypatch.setattr(common, "get_json", lambda path, timeout=5: {"doc": {"layers": ["docs"]}})

    def fake_put(path, body, method="POST", **kwargs):
        seen["puts"].append((path, body, method))
        return {"document_id": 7}

    monkeypatch.setattr(common, "send_json", fake_put)
    return seen


def test_update_feature_sends_description_and_paths_of_the_catalog(monkeypatch):
    seen = _fake(monkeypatch, json.loads(json.dumps(DOC)))

    assert docs.update_feature("p", "runner", "/tmp") == 7

    path, body, method = seen["puts"][0]
    assert (path, method) == ("/api/projects/p/features/runner/doc", "PUT")
    assert body["description"] == "Runs approved tasks."
    assert body["paths"] == ["plugin/runner/src/**"]
    assert "catalog" not in body["doc"]
    assert body["doc"]["product"] == DOC["product"]
    assert body["layers"] == ["docs"]


def test_update_feature_without_catalog_sends_only_the_doc(monkeypatch):
    seen = _fake(monkeypatch, {"product": {}, "technical": {}})

    docs.update_feature("p", "runner", "/tmp")

    body = seen["puts"][0][1]
    assert "description" not in body and "paths" not in body


def test_feature_prompt_asks_for_product_and_technical(monkeypatch):
    seen = _fake(monkeypatch, json.loads(json.dumps(DOC)))

    docs.update_feature("p", "runner", "/tmp")

    prompt = seen["prompts"][0]
    assert '"product"' in prompt and '"technical"' in prompt
    assert '"catalog"' in prompt
