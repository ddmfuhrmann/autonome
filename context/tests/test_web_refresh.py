from html.parser import HTMLParser

import pytest

from autonome_context import plans, store
from autonome_context.plans import PlanBody, TaskSpec

from conftest import add_feature, make_handoff
from test_web import client  # noqa: F401  (the fixture)


class Marks(HTMLParser):
    """The `data-refresh` elements and the `data-live` regions (with their ids) of a page."""

    def __init__(self, html):
        super().__init__()
        self.refresh = 0
        self.live = []  # ids of the data-live regions, in order
        self.inside = []  # open tags: is each one inside a live region
        self.regions_with = {}  # element id -> inside a live region
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        live = "data-live" in a
        inside = live or any(self.inside)
        if "data-refresh" in a:
            self.refresh += 1
        if live:
            self.live.append(a.get("id"))
        if a.get("id"):
            self.regions_with[a["id"]] = inside
        if tag not in ("input", "br", "hr", "img", "meta", "link"):
            self.inside.append(live)

    def handle_endtag(self, tag):
        if self.inside:
            self.inside.pop()


@pytest.fixture
def pages(client, project, run_db, indexer):  # noqa: F811
    async def go(conn):
        await add_feature(conn, project, "links")
        plan = await plans.draft(conn, indexer, project, "links", [], PlanBody(title="L", goal="g", tasks=[TaskSpec(title="t", spec="s")]))
        wave = await plans.draft_wave(conn, indexer, project, plans.WaveBody(title="MVP", goal="g", features=[plans.WaveFeature(plan_id=plan)]))
        handoff = await store.put_handoff(conn, indexer, make_handoff(project, feature="links", summary="First."), None)
        await conn.commit()
        return plan, wave, handoff
    plan, wave, handoff = run_db(go)
    base = f"/ui/p/{project}"
    return {
        "home": "/ui", "documents": f"{base}/documents", "approvals": f"{base}/approvals", "waves": f"{base}/waves",
        "wave": f"{base}/documents/{wave}", "plan": f"{base}/documents/{plan}", "features": f"{base}/features",
        "feature": f"{base}/features/links", "document": f"{base}/documents/{handoff}", "insights": f"{base}/insights",
        "conventions": f"{base}/conventions", "triage": f"{base}/triage",
    }


READ_PAGES = ["home", "documents", "approvals", "waves", "wave", "plan", "features", "feature", "document", "insights",
              "conventions", "triage"]


@pytest.mark.parametrize("name", READ_PAGES)
def test_a_reading_page_refreshes_and_marks_its_regions(pages, client, project, monkeypatch, name):  # noqa: F811
    if name == "home":  # the shared test database keeps every project of every run: list only this one
        real = store.list_projects

        async def mine(conn):
            return [p for p in await real(conn) if p == project]
        monkeypatch.setattr(store, "list_projects", mine)
    r = client.get(pages[name])
    assert r.status_code == 200
    m = Marks(r.text)
    assert m.refresh == 1, "one data-refresh element"
    assert m.live and all(m.live), "at least one data-live region, each with an id"
    assert len(m.live) == len(set(m.live)), "region ids are unique"


@pytest.mark.parametrize("name", ["settings", "search", "lab"])
def test_forms_and_searches_do_not_refresh(pages, client, project, name):  # noqa: F811
    r = client.get(f"/ui/p/{project}/{name}")
    assert r.status_code == 200 and Marks(r.text).refresh == 0


def test_a_new_handoff_shows_in_the_list_region_of_a_refresh_request(pages, client, project, run_db, indexer):  # noqa: F811
    assert client.get(pages["documents"]).status_code == 200

    async def go(conn):
        await store.put_handoff(conn, indexer, make_handoff(project, feature="links", summary="Second one, after the first GET."), None)
        await conn.commit()
    run_db(go)

    page = client.get(pages["documents"], headers={"X-Refresh": "1"}).text
    start = page.index('id="doc-list"')
    region = page[start:page.index("</section>\n  </div>", start)] if "</section>\n  </div>" in page[start:] else page[start:]
    assert "Second one, after the first GET." in region


def test_the_documents_filter_stays_outside_the_live_regions(pages, client):  # noqa: F811
    m = Marks(client.get(pages["documents"]).text)
    assert m.regions_with["dq"] is False
