"""The context's side of contracts/runner-context.json: every body the runner POSTs for the triage must
pass the endpoint's model. The runner's tests build the same bodies from the same file, so a field one
side drops or renames fails a gate instead of answering 422 in a live run."""

import json
from pathlib import Path

from autonome_context.api import _JEV, PrOpened, RevisionVerdict, RunnerStatus, Screening, TriageVerdict

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "runner-context.json").read_text())


def test_the_deviation_verdict_body_passes_the_endpoint_model():
    v = TriageVerdict.model_validate(CONTRACT["triage_verdict"]["body"])
    assert v.round == CONTRACT["triage_verdict"]["item"]["round"] >= 1


def test_the_screening_body_passes_the_endpoint_model():
    Screening.model_validate(CONTRACT["screening"]["body"])


def test_the_human_choice_bodies_pass_the_endpoint_models():
    v = TriageVerdict.model_validate(CONTRACT["triage_verdict_human_choice"]["body"])
    assert (v.repeats_human_choice, v.human_choice_of) == (0.92, 634)
    s = Screening.model_validate(CONTRACT["screening_human_choice"]["body"])
    assert (s.repeats_human_choice, s.human_choice_of) == (0.9, 634)


def test_the_runner_status_body_passes_the_endpoint_model():
    s = RunnerStatus.model_validate(CONTRACT["runner_status"]["body"])
    assert s.triage_key is True


def test_the_in_pr_body_passes_the_endpoint_model():
    body = CONTRACT["task_in_pr"]["body"]
    assert body["to"] == "in_pr"
    pr = PrOpened.model_validate(body["run"])
    assert (pr.pr_url, pr.pr_mode) == (CONTRACT["task_in_pr"]["item"]["pr_url"], "manual")


def test_the_revision_verdict_body_passes_the_endpoint_model():
    v = RevisionVerdict.model_validate(CONTRACT["revision_verdict"]["body"])
    assert json.loads(v.state) == CONTRACT["revision_verdict"]["item"]["state"]
    assert _JEV.fullmatch(v.model), "the endpoint refuses a model that is not a Jev"
