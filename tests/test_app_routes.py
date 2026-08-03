"""Route-level tests -- the one deliberate exception to this repo's usual
"verify manually against the running app" convention. Justified because the
redirect/404/401 behavior here is easy to silently break and hard to
eyeball: if `/` stops preserving the query string, every `?ask=`/`?select=`
deep link silently breaks; if an unknown project id doesn't 404 cleanly, a
typo'd URL crashes instead of failing obviously; if a gated route stops
requiring login, that's a real cost/spam exposure on the live site.

None of the tests below trigger board loading/enrichment (no PDF parsing,
no OpenAI calls) -- root_redirect() only reads the project registry,
_resolve_project() 404s before any board/payload access happens, and
_require_admin() 401s even earlier than that (verified explicitly below:
using a project id that doesn't exist still 401s, not 404s, proving the
auth check runs before project resolution, not after).
"""

from fastapi.testclient import TestClient

from schemantic.web.app import _require_admin, app

client = TestClient(app)

# doesn't need to be a real project -- the gated routes must reject an
# unauthenticated request before ever checking whether the project exists
FAKE_PROJECT = "proj_test_gate"


def test_root_redirects_to_last_active_project_preserving_query_string():
    res = client.get("/?ask=where+is+the+IMU", follow_redirects=False)
    assert res.status_code == 307
    location = res.headers["location"]
    assert location.startswith("/p/")
    assert location.endswith("/?ask=where+is+the+IMU")


def test_unknown_project_id_404s_cleanly():
    res = client.get("/p/proj_does_not_exist/api/schematic")
    assert res.status_code == 404


def test_upload_requires_login():
    res = client.post(
        f"/p/{FAKE_PROJECT}/upload",
        files={"file": ("board.pdf", b"dummy", "application/pdf")},
    )
    assert res.status_code == 401


def test_create_project_requires_login():
    res = client.post("/api/projects", json={})
    assert res.status_code == 401


def test_propose_mates_requires_login():
    res = client.post(f"/p/{FAKE_PROJECT}/api/workspace/propose-mates")
    assert res.status_code == 401


def test_mate_decision_requires_login():
    res = client.post(
        f"/p/{FAKE_PROJECT}/api/mates/some_mate_id", json={"status": "confirmed"}
    )
    assert res.status_code == 401


def test_firmware_starter_requires_login():
    res = client.get(f"/p/{FAKE_PROJECT}/api/firmware-starter")
    assert res.status_code == 401


def test_switch_and_chat_stay_public_no_login_required():
    # explicitly NOT gated, per the "anonymous can view/chat" decision --
    # confirms these don't accidentally get 401'd by a too-broad change later
    switch_res = client.post(
        f"/p/{FAKE_PROJECT}/api/workspace/switch", json={"board_id": "x"}
    )
    assert switch_res.status_code != 401
    chat_res = client.post(f"/p/{FAKE_PROJECT}/api/chat", json={"message": "hi"})
    assert chat_res.status_code != 401


def test_authenticated_request_passes_the_gate():
    # bypasses the gate the standard FastAPI way (dependency override) --
    # not by fabricating a session cookie, which would couple this test to
    # Starlette/itsdangerous's internal cookie encoding. Deliberately picks
    # a route+fake-project combination with no side effects when the gate
    # passes (propose-mates on an unknown project just fails project
    # resolution next), so this test doesn't write a real project into the
    # actual .cache/projects.json on disk.
    app.dependency_overrides[_require_admin] = lambda: None
    try:
        res = client.post(f"/p/{FAKE_PROJECT}/api/workspace/propose-mates")
        # past the gate now -- fails on project resolution instead, proving
        # auth and project-lookup are two genuinely separate checks, not
        # one conflated 401-or-404 branch
        assert res.status_code == 404
    finally:
        app.dependency_overrides.pop(_require_admin, None)
