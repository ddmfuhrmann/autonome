"""The context's side of contracts/runner-context.json: every body the runner POSTs for the triage must
pass the endpoint's model. The runner's tests build the same bodies from the same file, so a field one
side drops or renames fails a gate instead of answering 422 in a live run."""

import json
from pathlib import Path

from autonome_context.api import RevisionVerdict, Screening, TriageVerdict

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "runner-context.json").read_text())


def test_the_deviation_verdict_body_passes_the_endpoint_model():
    v = TriageVerdict.model_validate(CONTRACT["triage_verdict"]["body"])
    assert v.round == CONTRACT["triage_verdict"]["item"]["round"] >= 1


def test_the_screening_body_passes_the_endpoint_model():
    Screening.model_validate(CONTRACT["screening"]["body"])


def test_the_revision_verdict_body_passes_the_endpoint_model():
    v = RevisionVerdict.model_validate(CONTRACT["revision_verdict"]["body"])
    assert json.loads(v.state) == CONTRACT["revision_verdict"]["item"]["state"]
