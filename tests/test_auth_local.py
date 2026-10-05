"""Local username/password sign-in: default admin, forced password change, lockout, user administration.

Tests in this module run in file order and share the state of the default local account.
"""

from fastapi.testclient import TestClient

from cyfun.passwords import hash_password, password_problems, verify_password

NEW_PASSWORD = "Correct-Horse-Battery-9"


def fresh(app) -> TestClient:
    c = TestClient(app, base_url="http://testserver")
    c.headers["Origin"] = "http://testserver"
    return c


def local_user_id(username: str) -> int:
    from sqlalchemy import select

    from cyfun import db as database
    from cyfun.models import User

    with database.session() as s:
        return s.execute(select(User.id).where(User.username == username)).scalar_one()


def ensure_admin_password(app) -> None:
    """Bring the default admin to NEW_PASSWORD whatever state earlier tests left it in."""
    with fresh(app) as c:
        r = c.post("/auth/local", data={"username": "admin", "password": NEW_PASSWORD}, follow_redirects=False)
        if r.status_code == 303 and not r.headers["location"].startswith("/auth/password"):
            return
        r = c.post("/auth/local", data={"username": "admin", "password": "admin"}, follow_redirects=False)
        assert r.status_code == 303, r.status_code
        r = c.post("/auth/password", data={"current": "admin", "new": NEW_PASSWORD, "confirm": NEW_PASSWORD, "next": "/"}, follow_redirects=False)
        assert r.status_code == 303, r.text


def test_password_hashing_and_policy():
    h = hash_password("s3cret-password-value")
    assert h.startswith("scrypt$") and verify_password("s3cret-password-value", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "garbage")
    assert password_problems("short", "bob")
    assert password_problems("bobbobbobbob1", "bob")
    assert password_problems("administrator", "x")
    assert password_problems(NEW_PASSWORD, "admin") == []


def test_default_admin_must_change_password(app):
    with fresh(app) as c:
        r = c.post("/auth/local", data={"username": "admin", "password": "admin", "next": "/assessment"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/auth/password")
        # every page redirects to the password form until it is changed
        r = c.get("/assessment", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/password")
        # weak password refused
        r = c.post("/auth/password", data={"current": "admin", "new": "admin", "confirm": "admin", "next": "/assessment"})
        assert r.status_code == 400
        r = c.post("/auth/password", data={"current": "admin", "new": NEW_PASSWORD, "confirm": "different-thing-12", "next": "/"})
        assert r.status_code == 400
        r = c.post("/auth/password", data={"current": "wrong", "new": NEW_PASSWORD, "confirm": NEW_PASSWORD, "next": "/"})
        assert r.status_code == 400
        r = c.post("/auth/password", data={"current": "admin", "new": NEW_PASSWORD, "confirm": NEW_PASSWORD, "next": "/assessment"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/assessment"
        assert c.get("/assessment").status_code == 200
        assert "password_change" in c.get("/activity").text


def test_login_with_new_password_and_wrong_password(app):
    ensure_admin_password(app)
    with fresh(app) as c:
        r = c.post("/auth/local", data={"username": "admin", "password": "admin"}, follow_redirects=False)
        assert r.status_code == 401
        r = c.post("/auth/local", data={"username": "nobody", "password": "x"}, follow_redirects=False)
        assert r.status_code == 401
        r = c.post("/auth/local", data={"username": "ADMIN", "password": NEW_PASSWORD, "next": "//evil.example"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert c.get("/").status_code == 200
        assert "Administrator" in c.get("/").text


def test_user_administration_and_lockout(app):
    ensure_admin_password(app)
    with fresh(app) as c:
        r = c.post("/auth/local", data={"username": "admin", "password": NEW_PASSWORD}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        r = c.post("/auth/users", data={"username": "auditor.cab", "display_name": "External auditor", "role": "auditor"})
        assert r.status_code == 200
        assert "Temporary password for auditor.cab" in r.text
        temp = r.text.split('class="temp-pw">')[1].split("<")[0]
        assert len(temp) == 16
        # duplicate refused
        r = c.post("/auth/users", data={"username": "auditor.cab", "role": "auditor"}, follow_redirects=False)
        assert "err=" in r.headers["location"]
        # cannot disable or delete yourself
        me = c.get("/auth/users").text
        assert "(you)" in me
    with fresh(app) as a:
        # auditor signs in with the temporary password, must change it, then is read-only
        r = a.post("/auth/local", data={"username": "auditor.cab", "password": temp}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/password")
        r = a.post(
            "/auth/password", data={"current": temp, "new": "Auditor-Pass-Phrase-77", "confirm": "Auditor-Pass-Phrase-77", "next": "/"}, follow_redirects=False
        )
        assert r.status_code == 303
        assert a.get("/audit").status_code == 200
        assert a.post("/actions", data={"title": "x"}, follow_redirects=False).status_code == 403
        assert a.get("/auth/users", follow_redirects=False).status_code == 403
    with fresh(app) as b:
        # five wrong attempts lock the account
        for _ in range(5):
            assert b.post("/auth/local", data={"username": "auditor.cab", "password": "nope"}, follow_redirects=False).status_code == 401
        r = b.post("/auth/local", data={"username": "auditor.cab", "password": "Auditor-Pass-Phrase-77"}, follow_redirects=False)
        assert r.status_code == 423
    with fresh(app) as c:
        c.post("/auth/local", data={"username": "admin", "password": NEW_PASSWORD}, follow_redirects=False)
        users = c.get("/auth/users").text
        assert "locked" in users
        uid = local_user_id("auditor.cab")
        # reset clears the lock and forces a new password
        r = c.post(f"/auth/users/{uid}/reset")
        assert r.status_code == 200 and "Temporary password" in r.text
        # disable, then the account cannot sign in
        r = c.post(f"/auth/users/{uid}/update", data={"role": "auditor", "disabled": "1"}, follow_redirects=False)
        assert "msg=" in r.headers["location"]
    with fresh(app) as d:
        assert d.post("/auth/local", data={"username": "auditor.cab", "password": "anything-at-all-123"}, follow_redirects=False).status_code == 401
    with fresh(app) as c:
        c.post("/auth/local", data={"username": "admin", "password": NEW_PASSWORD}, follow_redirects=False)
        r = c.post(f"/auth/users/{uid}/delete", follow_redirects=False)
        assert "msg=" in r.headers["location"]
        assert "auditor.cab" not in c.get("/auth/users").text
