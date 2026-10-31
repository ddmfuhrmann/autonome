# autonome task #115: POST /links and GET /{code}

Project shortlink-v2, feature links, layers api, test.

## What to do
In src/shortlink/app.py: pydantic models `LinkCreate(url: str)` and `LinkOut(code: str, url: str, created_at: str)`. POST /links: validate with urls.validate_url; on InvalidURLError raise HTTPException(400, detail=<the error message>); else create_link and return 201 with LinkOut. GET /{code}: declared after every other route; if not codes.is_valid_code(code) or get_link returns None, raise HTTPException(404, detail="link not found"); else return RedirectResponse(url, status_code=302). Add an exception handler for RequestValidationError that returns 422 with {"detail": "<short string>"} (e.g. "invalid request body") so every error is a string detail. Open one connection per request via a dependency that closes it afterwards.

## Done when
- tests/test_api_links.py: POST /links with a valid URL returns 201 and {code, url, created_at} with a 7-char code
- POST /links with an internal host (http://127.0.0.1/) and with a javascript: URL returns 400 with a string detail
- POST /links with no body, with {} and with {"url": 123} returns 422 with a string detail (not a list)
- GET /{code} for a created link returns 302 with Location equal to the stored URL (client with follow_redirects=False)
- GET /{code} for an unknown valid-format code and for an invalid-format code (e.g. 'abc') returns 404 {"detail": "link not found"}
- GET /health and GET /docs still return 200 (not captured by /{code})
- All gates pass, coverage >= 90%

## The plan it belongs to: Links: shorten a URL and redirect
Goal: POST /links validates a URL and creates a random 7-char short code; GET /{code} redirects (302) to the stored URL. Also sets up strict quality gates (ruff format, extra ruff rules, mypy --strict, import-linter layers, coverage >= 90%) so the autonome process is tested on implementing within enforced limits.
Out of scope (do not do):
- Authentication
- Custom codes / aliases
- Link expiration
- Deleting or editing links
- Rate limiting
- Deduplicating the same URL (each POST creates a new code)
- DNS resolution when checking internal hosts (only the literal host is checked)
- Click counting (click-stats plan)

## Conventions for these layers
### Response models and string errors
Every endpoint declares a pydantic response model (or returns a RedirectResponse). Every expected failure is an HTTPException with a string detail, including request validation (422 with a string detail); no path returns a raw 500.

### Routes stay thin
app.py only parses input, calls functions of shortlink.links/stats/urls/codes and maps their results and exceptions to HTTP; no SQL and no validation logic in app.py.

### Tests assert behaviour on a temp database
Tests assert status codes and response bodies or return values, one named test per edge listed in acceptance; they use the tmp_path database via the client fixture or SHORTLINK_DB, never ./shortlink.db. Never add a test only to raise coverage.

## How this feature works today
Feature "links" lets a client shorten a URL and then follow the short code to the original page. It's done: the API, storage and validation are all in place.

- `POST /links` validates a URL and creates a random 7-char base62 code.
- `GET /{code}` redirects (302) to the stored URL.
- Only public http/https URLs are accepted. Internal hosts are rejected by their literal name or IP, with no DNS lookup.
- Every error is JSON `{"detail": "..."}`.
- Out of scope: auth, custom aliases, expiry, edit/delete, rate limiting, deduplication, click counting. `stats.py` is still a docstring-only placeholder for the later click-stats plan.

## Gates
When you finish, the metronome runs every gate: ruff, pytest, ruff-format, mypy, coverage, import-linter. While working, run the fast ones (ruff, pytest, ruff-format, mypy, import-linter) with `autonome-gates --fast`. Never weaken, skip or edit a gate or its rules to make it pass.