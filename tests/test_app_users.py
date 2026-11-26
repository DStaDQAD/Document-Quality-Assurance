"""Guest accounts (APP_USERS): they log in like the main account and use every page but
Pengaturan, which the server refuses them. Their passwords are stored as PBKDF2 hashes."""
from fastapi.testclient import TestClient

import app_users
import main

ADMIN, ADMIN_PASS = "admin@example.com", "s3cret*#"
GUEST, GUEST_PASS = "DSta_DSSR", "Kunci2026abcd"


def _enable(monkeypatch, guests=None):
    monkeypatch.setenv("APP_USERNAME", ADMIN)
    monkeypatch.setenv("APP_PASSWORD", ADMIN_PASS)
    guests = guests if guests is not None else {GUEST: GUEST_PASS}
    monkeypatch.setenv("APP_USERS", ";".join(f"{u}:{app_users.hash_password(p)}" for u, p in guests.items()))


def _login(client, user, password):
    return client.post("/api/login", data={"username": user, "password": password})


# --- passwords -----------------------------------------------------------------------------------

def test_a_password_hash_verifies_only_its_own_password():
    stored = app_users.hash_password(GUEST_PASS)

    assert stored.startswith("pbkdf2_sha256$") and GUEST_PASS not in stored
    assert app_users.verify_password(GUEST_PASS, stored)
    assert not app_users.verify_password("Kunci2026abce", stored)
    assert not app_users.verify_password(GUEST_PASS, "not-a-hash")


def test_the_same_password_hashes_differently_each_time():
    assert app_users.hash_password(GUEST_PASS) != app_users.hash_password(GUEST_PASS)


def test_guest_accounts_are_read_from_app_users_by_semicolon_or_line(monkeypatch):
    monkeypatch.setenv("APP_USERS", " DSta_DSSR:pbkdf2_sha256$1$aa$bb ;\nDSta_DANP:pbkdf2_sha256$1$cc$dd\n")

    assert app_users.guest_users() == {"DSta_DSSR": "pbkdf2_sha256$1$aa$bb", "DSta_DANP": "pbkdf2_sha256$1$cc$dd"}


def test_a_weak_password_is_named_for_what_it_lacks():
    assert app_users.password_problem("Kunci2026abcd") is None
    assert "12" in app_users.password_problem("Kunci2026")
    assert "angka" in app_users.password_problem("KunciRahasiaSekali")
    assert "huruf" in app_users.password_problem("202620262026")


# --- logging in and what a guest may open ---------------------------------------------------------

def test_a_guest_logs_in_and_uses_the_data_apis(monkeypatch):
    _enable(monkeypatch)
    client = TestClient(main.app)

    assert _login(client, GUEST, GUEST_PASS).status_code == 200
    assert client.get("/api/tables").status_code != 401


def test_a_guest_with_a_wrong_password_is_refused(monkeypatch):
    _enable(monkeypatch)

    assert _login(TestClient(main.app), GUEST, "Kunci2026abce").status_code == 401


def test_a_guest_cannot_change_the_settings_or_read_their_changes(monkeypatch):
    _enable(monkeypatch)
    client = TestClient(main.app)
    _login(client, GUEST, GUEST_PASS)

    assert client.put("/api/settings/npi/visibility", json={"hidden": []}).status_code == 403
    assert client.post("/api/settings/npi/reset").status_code == 403
    assert client.delete("/api/settings/npi/notes/1").status_code == 403
    assert client.get("/api/settings/changes").status_code == 403


def test_a_guest_reads_what_the_verification_page_needs_from_the_settings(monkeypatch):
    # The Quality Assurance page reads /api/settings for the publications shown, the upload hints
    # and the checker names.
    _enable(monkeypatch)
    client = TestClient(main.app)
    _login(client, GUEST, GUEST_PASS)

    assert client.get("/api/settings").status_code == 200


def test_the_main_account_still_opens_the_settings(monkeypatch):
    _enable(monkeypatch)
    client = TestClient(main.app)
    _login(client, ADMIN, ADMIN_PASS)

    assert client.get("/api/settings").status_code == 200


def test_auth_status_says_who_may_open_the_settings(monkeypatch):
    _enable(monkeypatch)
    guest, admin = TestClient(main.app), TestClient(main.app)
    _login(guest, GUEST, GUEST_PASS)
    _login(admin, ADMIN, ADMIN_PASS)

    assert guest.get("/api/auth-status").json()["settings"] is False
    assert admin.get("/api/auth-status").json()["settings"] is True


def test_a_guest_removed_from_app_users_is_logged_out(monkeypatch):
    _enable(monkeypatch)
    client = TestClient(main.app)
    _login(client, GUEST, GUEST_PASS)

    _enable(monkeypatch, guests={"DSta_DANP": "Lain2026abcdef"})

    assert client.get("/api/tables").status_code == 401


def test_guests_alone_do_not_turn_the_login_on(monkeypatch):
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("APP_USERS", f"{GUEST}:{app_users.hash_password(GUEST_PASS)}")

    assert TestClient(main.app).get("/api/auth-status").json()["auth_required"] is False
