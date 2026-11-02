import pytest
from pydantic import ValidationError

from autonome_context import store
from autonome_context.handoff import Decision

from conftest import make_handoff


def _decision(quote):
    return Decision(title="Use option 1", rationale="the human picked it", human_choice={"quote": quote})


def test_decision_with_human_choice_is_stored_with_the_quote(run_db, project, indexer):
    async def go(conn):
        h = make_handoff(project, decisions=[_decision("1, vai com a primeira")])
        await store.put_handoff(conn, indexer, h, None)
        return await store.list_documents(conn, project, type="decision")

    [d] = run_db(go)
    assert d["body"]["human_choice"] == {"quote": "1, vai com a primeira"}


def test_decision_without_human_choice_has_no_field(run_db, project, indexer):
    async def go(conn):
        await store.put_handoff(conn, indexer, make_handoff(project), None)
        return await store.list_documents(conn, project, type="decision")

    [d] = run_db(go)
    assert "human_choice" not in d["body"]


@pytest.mark.parametrize("quote", ["", "x" * 301, "a\x00b"])
def test_invalid_quote_is_refused(quote):
    with pytest.raises(ValidationError):
        _decision(quote)


def test_quote_of_300_characters_is_accepted():
    assert _decision("x" * 300).human_choice.quote == "x" * 300


def test_email_in_the_quote_is_redacted(run_db, project, indexer):
    async def go(conn):
        h = make_handoff(project, decisions=[_decision("sim, manda para joao.silva@example.com")])
        await store.put_handoff(conn, indexer, h, None)
        return await store.list_documents(conn, project, type="decision")

    [d] = run_db(go)
    assert "joao.silva@example.com" not in d["body"]["human_choice"]["quote"]
