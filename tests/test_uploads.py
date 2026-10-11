"""Uploading a WigleWifi CSV to WiGLE, WDGWars and Wardrift.

All three take the same CSV the survey already writes, so there is no converter
here -- only framing, credentials, the per-service clients, and remembering what
has already been sent. Ported from m1ndb0x.
"""
import base64

import pytest

from minidriver import uploads
from minidriver.uploads import (UploadError, UploadLedger, WardriftClient,
                                WdgwarsClient, WigleClient, basic_auth_header,
                                multipart_body, sha256_of)

# WiGLE hands its "encoded for use" token out unpadded, which base64 rejects.
WIGLE = ("QUlEZmJmM2Y4ODUwYzY2ZTM2OTQzMWQ1MjE5MmU4OWYxNGY6"
         "YWI0MDA2YzkyZTgyZTZjNjVjMDliZTBiNThjNjg5NWU")


class TestCredentials:
    def test_an_unpadded_wigle_token_is_accepted(self):
        header = basic_auth_header(WIGLE)
        assert header.startswith("Basic ")
        assert base64.b64decode(header[len("Basic "):]).decode().count(":") == 1

    def test_a_name_token_pair_is_accepted(self):
        header = basic_auth_header("myname:mytoken")
        assert base64.b64decode(header[len("Basic "):]).decode() == "myname:mytoken"

    def test_an_empty_credential_is_refused(self):
        with pytest.raises(ValueError):
            basic_auth_header("")


class TestMultipart:
    def test_file_is_sent_under_the_field_name(self):
        body, _ = multipart_body("file", "drive.csv", b"MAC,SSID\n")
        assert b'name="file"' in body and b"drive.csv" in body

    def test_content_is_carried_intact(self):
        body, _ = multipart_body("file", "a.csv", b"AA:BB,net\n")
        assert b"AA:BB,net" in body

    def test_boundary_declared_and_never_inside_payload(self):
        content = b"x" * 100
        body, content_type = multipart_body("file", "a.csv", content)
        boundary = content_type.split("boundary=")[1].encode()
        assert boundary not in content
        assert body.count(boundary) == 2       # opening marker and close

    def test_a_colliding_payload_forces_a_new_boundary(self):
        body, content_type = multipart_body("file", "a.csv", b"minidriver" * 500)
        boundary = content_type.split("boundary=")[1].encode()
        assert body.count(boundary) == 2


class TestSha256:
    def test_identical_content_hashes_identically(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.write_bytes(b"MAC,SSID\n")
        b.write_bytes(b"MAC,SSID\n")
        assert sha256_of(a) == sha256_of(b)

    def test_different_content_differs(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.write_bytes(b"one")
        b.write_bytes(b"two")
        assert sha256_of(a) != sha256_of(b)


class TestLedger:
    def test_nothing_sent_to_begin_with(self, tmp_path):
        assert UploadLedger(tmp_path / "db").already_sent("wigle", "abc") is False

    def test_a_recorded_upload_is_remembered(self, tmp_path):
        led = UploadLedger(tmp_path / "db")
        led.record("wigle", "abc", "drive.csv", {"imported": 5})
        assert led.already_sent("wigle", "abc") is True

    def test_each_service_tracked_separately(self, tmp_path):
        led = UploadLedger(tmp_path / "db")
        led.record("wigle", "abc", "d.csv", {})
        assert led.already_sent("wdgwars", "abc") is False

    def test_recording_twice_is_harmless(self, tmp_path):
        led = UploadLedger(tmp_path / "db")
        led.record("wigle", "abc", "a.csv", {})
        led.record("wigle", "abc", "a.csv", {})
        assert len(led.recent()) == 1

    def test_recent_is_newest_first(self, tmp_path):
        led = UploadLedger(tmp_path / "db")
        led.record("wigle", "one", "a.csv", {})
        led.record("wigle", "two", "b.csv", {})
        assert [u["digest"] for u in led.recent()] == ["two", "one"]

    def test_the_ledger_survives_a_restart(self, tmp_path):
        UploadLedger(tmp_path / "db").record("wigle", "abc", "a.csv", {})
        assert UploadLedger(tmp_path / "db").already_sent("wigle", "abc") is True

    def test_sent_source_keys_on_path_and_size(self, tmp_path):
        cap = tmp_path / "survey.csv"
        cap.write_bytes(b"x" * 10)
        led = UploadLedger(tmp_path / "db")
        assert led.sent_source("wigle", cap, 10) is False
        led.record("wigle", "abc", "survey.csv", {}, source=cap)
        assert led.sent_source("wigle", cap, 10) is True
        # A capture that has since grown is not considered already sent.
        assert led.sent_source("wigle", cap, 20) is False


class TestClients:
    def test_wigle_uploads_with_basic_auth(self, tmp_path, monkeypatch):
        captured = {}

        def fake_request(url, headers, data=None, content_type=None, timeout=120):
            captured.update(url=url, headers=headers)
            return {"success": True}

        monkeypatch.setattr(uploads, "_request", fake_request)
        csv = tmp_path / "d.csv"
        csv.write_bytes(b"MAC,SSID\n")
        WigleClient(WIGLE).upload(csv)
        assert captured["url"] == uploads.WIGLE_UPLOAD
        assert captured["headers"]["Authorization"].startswith("Basic ")

    def test_wdgwars_uploads_with_api_key_header(self, tmp_path, monkeypatch):
        captured = {}

        def fake_request(url, headers, data=None, content_type=None, timeout=120):
            captured.update(url=url, headers=headers)
            return {"ok": True}

        monkeypatch.setattr(uploads, "_request", fake_request)
        csv = tmp_path / "d.csv"
        csv.write_bytes(b"x")
        WdgwarsClient("A" * 64).upload(csv)
        assert captured["headers"]["X-API-Key"] == "A" * 64

    def test_wdgwars_rejects_an_empty_key(self):
        with pytest.raises(ValueError):
            WdgwarsClient("")

    def test_wardrift_signs_in_then_uploads_with_bearer(self, tmp_path, monkeypatch):
        calls = []

        def fake_request(url, headers, data=None, content_type=None, timeout=120):
            calls.append((url, headers))
            if url.endswith("/v1/auth/sign-in/username"):
                return {"session": {"token": "TOK"}}
            return {"accepted": True}

        monkeypatch.setattr(uploads, "_request", fake_request)
        csv = tmp_path / "d.csv"
        csv.write_bytes(b"x")
        WardriftClient("me", "pw").upload(csv)
        # First a sign-in, then the upload carrying the bearer token.
        assert calls[0][0].endswith("/v1/auth/sign-in/username")
        assert calls[1][1]["Authorization"] == "Bearer TOK"

    def test_wardrift_treats_a_duplicate_as_already_sent(self, tmp_path, monkeypatch):
        def fake_request(url, headers, data=None, content_type=None, timeout=120):
            if url.endswith("/v1/auth/sign-in/username"):
                return {"session": {"token": "TOK"}}
            raise UploadError("409 duplicate", status=409)

        monkeypatch.setattr(uploads, "_request", fake_request)
        csv = tmp_path / "d.csv"
        csv.write_bytes(b"x")
        result = WardriftClient("me", "pw").upload(csv)
        assert result.get("duplicate") is True


def test_the_user_agent_identifies_the_box_not_urllib():
    # WDGWars sits behind Cloudflare, which 403s urllib's default agent.
    assert "minidriver" in uploads.USER_AGENT.lower()
    assert "urllib" not in uploads.USER_AGENT.lower()
