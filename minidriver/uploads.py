"""Submitting a survey to WiGLE, WDGWars and Wardrift, and remembering what has
already gone where.

Ported from m1ndb0x, trimmed to this box's shape: the survey is already written
as WigleWifi-1.6, so there is no Kismet export step, and there are no handshakes,
so there is no wpa-sec. What remains is the three services' clients, the
multipart framing, and a content-hash ledger so a capture re-offered on the next
trip home is not sent twice.

Credentials cannot be hashed: every API needs the literal value. They live in
the 0600 config, are never logged, and never leave the box except to the service
they belong to.
"""
import base64
import hashlib
import json
import logging
import secrets
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

WIGLE_UPLOAD = "https://api.wigle.net/api/v2/file/upload"
WIGLE_STATS = "https://api.wigle.net/api/v2/stats/user"
WDGWARS_UPLOAD = "https://wdgwars.pl/api/upload-csv"
WDGWARS_ME = "https://wdgwars.pl/api/me"
# Wardrift takes the same CSV at /v1/wardrive/logs, but behind a signed-in
# session rather than a standing key: the client signs in with username/password
# for a bearer token. Served from the site root (no api. host).
WARDRIFT_BASE = "https://wardrift.net"
TIMEOUT = 120
# Cloudflare in front of WDGWars rejects urllib's default agent with a bare 403.
# Identifying ourselves properly is accepted and is the right thing to do -- an
# operator reading their logs should be able to tell what is calling them.
USER_AGENT = "minidriver/0.1 (+https://github.com/m1ndgames/minidriver)"


class UploadError(Exception):
    """An upload failed. Carries the HTTP status when known, so a caller can
    tell an expired session (401) or an already-uploaded route (409) apart from
    a real failure without parsing the message."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# --- identity ----------------------------------------------------------------
def sha256_of(path) -> str:
    """Content hash of a file, read in chunks: captures can be large."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 256), b""):
            digest.update(block)
    return digest.hexdigest()


# --- credentials -------------------------------------------------------------
def basic_auth_header(credential: str) -> str:
    """An Authorization value from WiGLE's token or a name:token pair.

    WiGLE's "encoded for use" value is handed out unpadded, and an unpadded
    base64 Authorization is rejected by some servers -- so it is decoded and
    re-encoded properly rather than passed through.
    """
    credential = (credential or "").strip()
    if not credential:
        raise ValueError("empty credential")

    if ":" in credential:
        pair = credential
    else:
        padded = credential + "=" * (-len(credential) % 4)
        try:
            pair = base64.b64decode(padded).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("credential is neither name:token nor base64") from exc
        if ":" not in pair:
            raise ValueError("decoded credential has no name:token separator")
    return "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")


# --- multipart ---------------------------------------------------------------
def multipart_body(field: str, filename: str, content: bytes,
                   part_type: str = "text/csv"):
    """A multipart/form-data body carrying one file. Hand-rolled rather than a
    dependency: the box must install without a package index."""
    while True:
        boundary = "minidriver" + secrets.token_hex(16)
        # A boundary occurring inside the payload would truncate the upload.
        if boundary.encode() not in content:
            break
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {part_type}\r\n\r\n".encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    return body, f"multipart/form-data; boundary={boundary}"


# --- what has already been sent ----------------------------------------------
LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service     TEXT NOT NULL,
    digest      TEXT NOT NULL,
    filename    TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    result      TEXT,
    -- The capture this came from and how big it was: lets an already-sent
    -- capture be skipped before the work of filtering, and lets one that has
    -- since grown be offered again.
    source      TEXT,
    source_size INTEGER,
    UNIQUE (service, digest)
);
"""


class UploadLedger:
    """Remembers which captures have gone where, keyed by content.

    By content hash rather than filename: a capture re-offered on the next trip
    home produces the same bytes, and re-submitting it would inflate the very
    statistics the survey is collecting. Owns its own SQLite file so it needs no
    wider store.
    """

    def __init__(self, db_path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path))
        self._db.row_factory = sqlite3.Row
        # WAL survives an abrupt power cut better than the default journal, which
        # matters on a box that is unplugged in the field.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(LEDGER_SCHEMA)
        self._db.commit()

    def already_sent(self, service: str, digest: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM uploads WHERE service = ? AND digest = ?",
            (service, digest)).fetchone()
        return row is not None

    def record(self, service: str, digest: str, filename: str, result,
               source=None) -> None:
        size = None
        if source is not None:
            try:
                size = Path(source).stat().st_size
            except OSError:
                size = None
        self._db.execute(
            "INSERT OR IGNORE INTO uploads "
            "(service, digest, filename, uploaded_at, result, source, source_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (service, digest, filename,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             json.dumps(result or {}),
             str(source) if source is not None else None, size))
        self._db.commit()

    def sent_source(self, service: str, source, size: int) -> bool:
        """Whether this exact capture, at this exact size, already went."""
        row = self._db.execute(
            "SELECT 1 FROM uploads WHERE service = ? AND source = ? "
            "AND source_size = ?", (service, str(source), size)).fetchone()
        return row is not None

    def recent(self, limit: int = 20) -> list:
        rows = self._db.execute(
            "SELECT service, digest, filename, uploaded_at, result "
            "FROM uploads ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            entry = dict(row)
            try:
                entry["result"] = json.loads(entry["result"] or "{}")
            except ValueError:
                entry["result"] = {}
            out.append(entry)
        return out

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass


# --- the services ------------------------------------------------------------
def _request(url, headers, data=None, content_type=None, timeout=TIMEOUT):
    """One HTTP call, returning parsed JSON. Never leaks the key into an error."""
    request = urllib.request.Request(url, data=data,
                                     method="POST" if data is not None else "GET")
    request.add_header("User-Agent", USER_AGENT)
    for name, value in headers.items():
        request.add_header(name, value)
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace")
        raise UploadError(f"{exc.code} from {urllib.parse.urlsplit(url).netloc}: "
                          f"{detail}", status=exc.code) from None
    except (urllib.error.URLError, OSError) as exc:
        raise UploadError(f"cannot reach {urllib.parse.urlsplit(url).netloc}: "
                          f"{getattr(exc, 'reason', exc)}") from None
    try:
        return json.loads(body)
    except ValueError:
        return {"raw": body[:400].decode("utf-8", "replace")}


class WigleClient:
    """api.wigle.net -- HTTP Basic with the account's encoded token."""

    service = "wigle"

    def __init__(self, credential: str):
        self._auth = basic_auth_header(credential)

    def upload(self, csv_path) -> dict:
        content = Path(csv_path).read_bytes()
        body, content_type = multipart_body("file", Path(csv_path).name, content)
        return _request(WIGLE_UPLOAD, {"Authorization": self._auth},
                        data=body, content_type=content_type)

    def stats(self) -> dict:
        data = _request(WIGLE_STATS, {"Authorization": self._auth})
        stats = (data.get("statistics") or {}) if isinstance(data, dict) else {}
        return {
            "user": data.get("user") if isinstance(data, dict) else None,
            "discovered_wifi": stats.get("discoveredWiFi"),
            "discovered_bt": stats.get("discoveredBt"),
            "rank": stats.get("rank"),
            "month_rank": stats.get("monthRank"),
        }


class WdgwarsClient:
    """wdgwars.pl -- X-API-Key with the 64-character profile key."""

    service = "wdgwars"

    def __init__(self, key: str):
        key = (key or "").strip()
        if not key:
            raise ValueError("empty api key")
        self._headers = {"X-API-Key": key}

    def upload(self, csv_path) -> dict:
        content = Path(csv_path).read_bytes()
        body, content_type = multipart_body("file", Path(csv_path).name, content)
        return _request(WDGWARS_UPLOAD, self._headers,
                        data=body, content_type=content_type)

    def stats(self) -> dict:
        data = _request(WDGWARS_ME, self._headers)
        if not isinstance(data, dict):
            return {}
        return {
            "username": data.get("username"),
            "wifi": data.get("wifi"),
            "ble": data.get("ble"),
            "total": data.get("total"),
            "gang": data.get("gang"),
            "recent_7d": data.get("recent_7d"),
            "credits": (data.get("credits") or {}).get("balance"),
        }


class WardriftClient:
    """wardrift.net -- a signed-in session, not a standing key.

    The CSV upload is gated by a session, so there is no token to paste: the
    client signs in with the account's username and password, caches the
    returned token, and uses it for upload and stats. The token can expire, so a
    request that comes back 401 triggers one re-sign-in and a single retry --
    invisible to an operator who set the credentials weeks ago.
    """

    service = "wardrift"

    def __init__(self, username: str, password: str, base_url: str = WARDRIFT_BASE):
        username = (username or "").strip()
        if not username or not password:
            raise ValueError("username and password required")
        self._username = username
        self._password = password
        self._base = base_url.rstrip("/")
        self._token = None

    def _sign_in(self) -> str:
        payload = json.dumps({"username": self._username,
                              "password": self._password,
                              "remember_me": True}).encode("utf-8")
        data = _request(self._base + "/v1/auth/sign-in/username", {},
                        data=payload, content_type="application/json")
        token = (data.get("session") or {}).get("token") if isinstance(data, dict) else None
        if not token:
            raise UploadError("sign-in to wardrift returned no session token")
        self._token = token
        return token

    def _authed(self, url, data=None, content_type=None) -> dict:
        """A bearer-token request that re-signs-in once on a 401."""
        if not self._token:
            self._sign_in()
        try:
            return _request(url, {"Authorization": "Bearer %s" % self._token},
                            data=data, content_type=content_type)
        except UploadError as exc:
            if exc.status != 401:
                raise
            self._token = None
            self._sign_in()
            return _request(url, {"Authorization": "Bearer %s" % self._token},
                            data=data, content_type=content_type)

    def upload(self, csv_path) -> dict:
        content = Path(csv_path).read_bytes()
        body, content_type = multipart_body("file", Path(csv_path).name, content)
        try:
            return self._authed(self._base + "/v1/wardrive/logs",
                                data=body, content_type=content_type)
        except UploadError as exc:
            # A route it already has comes back 409. That is not a failure -- the
            # capture is on record -- so it reads as 'already'.
            if exc.status == 409:
                return {"accepted": True, "duplicate": True, "outcome": "already"}
            raise

    def stats(self) -> dict:
        data = self._authed(self._base + "/v1/wardrive/me")
        if not isinstance(data, dict):
            return {}
        lifetime = data.get("lifetime") or {}
        week = data.get("this_week") or {}
        return {
            "routes": lifetime.get("routes_logged"),
            "access_points": lifetime.get("access_points_discovered"),
            "distance_km": lifetime.get("distance_km"),
            "week_routes": week.get("routes_logged"),
            "streak_days": data.get("streak_days"),
        }
