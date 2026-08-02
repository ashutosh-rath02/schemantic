"""Route-level tests -- the one deliberate exception to this repo's usual
"verify manually against the running app" convention. Justified because the
redirect/404 behavior here is easy to silently break and hard to eyeball:
if `/` stops preserving the query string, every `?ask=`/`?select=` deep
link silently breaks; if an unknown project id doesn't 404 cleanly, a typo'd
URL crashes instead of failing obviously.

Neither test below triggers board loading/enrichment (no PDF parsing, no
OpenAI calls) -- root_redirect() only reads the project registry, and
_resolve_project() 404s before any board/payload access happens.
"""

from fastapi.testclient import TestClient

from schemantic.web.app import app

client = TestClient(app)


def test_root_redirects_to_last_active_project_preserving_query_string():
    res = client.get("/?ask=where+is+the+IMU", follow_redirects=False)
    assert res.status_code == 307
    location = res.headers["location"]
    assert location.startswith("/p/")
    assert location.endswith("/?ask=where+is+the+IMU")


def test_unknown_project_id_404s_cleanly():
    res = client.get("/p/proj_does_not_exist/api/schematic")
    assert res.status_code == 404
