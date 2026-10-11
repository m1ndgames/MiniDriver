"""The upload sweep: every capture that has not gone to a service yet, sent when
the box is home. Adapted from m1ndb0x's shared survey-upload machinery, with the
payload/UI coupling stripped out. Clients are injected, so no network is touched.
"""
from minidriver import sweep
from minidriver.survey import COLUMNS, VERSION
from minidriver.uploads import UploadError, UploadLedger

HEADER = VERSION + "\n" + ",".join(COLUMNS) + "\n"
LOCATED = "AA:BB:CC:00:00:01,net,,,,,,51.2,6.7,,,,,WIFI\n"
UNLOCATED = "AA:BB:CC:00:00:02,net,,,,,,0,0,,,,,WIFI\n"


def capture(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(HEADER + body)
    return path


class FakeClient:
    service = "wigle"

    def __init__(self):
        self.uploaded = []

    def upload(self, csv_path):
        self.uploaded.append(csv_path)
        return {"success": True}


class TestPendingFor:
    def test_an_unsent_capture_is_pending(self, tmp_path):
        cap = capture(tmp_path, "survey.csv", LOCATED)
        led = UploadLedger(tmp_path / "db")
        assert sweep.pending_for([cap], "wigle", led) == [cap]

    def test_an_already_sent_capture_is_not_pending(self, tmp_path):
        cap = capture(tmp_path, "survey.csv", LOCATED)
        led = UploadLedger(tmp_path / "db")
        led.record("wigle", "digest", cap.name, {}, source=cap)
        assert sweep.pending_for([cap], "wigle", led) == []

    def test_a_grown_capture_is_pending_again(self, tmp_path):
        cap = capture(tmp_path, "survey.csv", LOCATED)
        led = UploadLedger(tmp_path / "db")
        led.record("wigle", "digest", cap.name, {}, source=cap)
        cap.write_text(HEADER + LOCATED + LOCATED)     # grew
        assert sweep.pending_for([cap], "wigle", led) == [cap]

    def test_an_empty_file_is_never_pending(self, tmp_path):
        cap = tmp_path / "empty.csv"
        cap.write_text("")
        led = UploadLedger(tmp_path / "db")
        assert sweep.pending_for([cap], "wigle", led) == []


class TestPrepareCapture:
    def test_strips_unlocated_and_writes_a_service_copy(self, tmp_path):
        cap = capture(tmp_path, "survey.csv", LOCATED + UNLOCATED)
        out = sweep.prepare_capture(cap, "wigle")
        assert out != cap and out.exists()
        from minidriver.survey import records_in
        assert records_in(out.read_text()) == 1       # the 0,0 row was dropped

    def test_a_capture_with_nothing_located_raises(self, tmp_path):
        cap = capture(tmp_path, "survey.csv", UNLOCATED)
        try:
            sweep.prepare_capture(cap, "wigle")
            assert False, "expected UploadError"
        except UploadError as exc:
            assert "nothing located" in str(exc).lower()


class TestUploadPending:
    def test_uploads_each_pending_capture_and_records_it(self, tmp_path):
        cap = capture(tmp_path, "survey.csv", LOCATED)
        led = UploadLedger(tmp_path / "db")
        client = FakeClient()
        result = sweep.upload_pending("wigle", [cap], led, client)
        assert result["sent"] == 1 and len(client.uploaded) == 1
        # Recorded, so a second sweep sends nothing.
        again = sweep.upload_pending("wigle", [cap], led, FakeClient())
        assert again["sent"] == 0

    def test_identical_content_is_skipped_not_resent(self, tmp_path):
        a = capture(tmp_path, "a.csv", LOCATED)
        b = capture(tmp_path, "b.csv", LOCATED)     # same content, different file
        led = UploadLedger(tmp_path / "db")
        client = FakeClient()
        result = sweep.upload_pending("wigle", [a, b], led, client)
        assert result["sent"] == 1 and result["skipped"] == 1
        assert len(client.uploaded) == 1

    def test_an_empty_capture_is_skipped_without_calling_the_client(self, tmp_path):
        cap = capture(tmp_path, "survey.csv", UNLOCATED)
        led = UploadLedger(tmp_path / "db")
        client = FakeClient()
        result = sweep.upload_pending("wigle", [cap], led, client)
        assert result["sent"] == 0 and result["skipped"] == 1
        assert client.uploaded == []
        # Recorded as empty so the next sweep does not re-examine it forever.
        assert sweep.pending_for([cap], "wigle", led) == []
