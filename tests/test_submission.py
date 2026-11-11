"""Real Chromium + loopback HTTP tests; these never submit to a real BB site."""

from types import SimpleNamespace

import httpx
import pytest
from mock_bb import MockBlackboard

from ustc_bb_mcp.config import Settings
from ustc_bb_mcp.errors import BBError
from ustc_bb_mcp.models import Assignment, Course
from ustc_bb_mcp.parsing import submission_form
from ustc_bb_mcp.submission import Submitter


@pytest.fixture
def mock_bb():
    server = MockBlackboard()
    yield server
    server.close()


class Session:
    identity = "fictional-account-session-123456789"

    def __init__(self):
        self.client = httpx.AsyncClient(follow_redirects=True)
        self.cookie_updates = 0

    async def get(self, url):
        try:
            response = await self.client.get(url)
        except httpx.HTTPError as exc:
            raise BBError("network_error", "Mock read failed") from exc
        if response.status_code >= 400:
            raise BBError("http_error", "Mock platform rejected request")
        return response

    async def storage_state(self):
        return {"cookies": [], "origins": []}

    async def update_cookies(self, _state):
        self.cookie_updates += 1

    def _bb_url(self, url):
        return url


@pytest.fixture
async def rig(mock_bb, tmp_path):
    session = Session()
    settings = Settings(base_url=mock_bb.base_url, data_dir=tmp_path / "state",
                        output_dir=tmp_path / "output", timeout=1, test_mode=True)
    course = Course(id="_1_1", name="Fictional course", url=mock_bb.url())
    assignment = Assignment(id="_2_1", course_id=course.id, name="Fictional report", url=mock_bb.url())
    other_assignment = Assignment(id="_3_1", course_id=course.id, name="Other report", url=mock_bb.url(assignment_id="_3_1"))
    assessment = Assignment(id="_9_1", course_id=course.id, name="Fictional test", kind="test", url=mock_bb.url(assignment_id="_9_1"))

    async def select_course(query):
        if query == "_4_1":
            return Course(id="_4_1", name="Other course", url=mock_bb.url(course_id="_4_1"))
        return course

    async def scan(selected):
        items = [item.model_copy(update={"course_id": selected.id,
                                        "url": mock_bb.url(selected.id, item.id)})
                 for item in (assignment, other_assignment, assessment)]
        return [], items, None, []

    service = SimpleNamespace(session=session, settings=settings, select_course=select_course, scan=scan)
    now = [1000.0]
    submitter = Submitter(service, clock=lambda: now[0], receipt_poll_delays=(0, 0, 0))
    path = tmp_path / "fictional-report.txt"
    path.write_text("Fictional local report.\n", encoding="utf-8")
    yield SimpleNamespace(server=mock_bb, session=session, service=service, submitter=submitter,
                          path=path, now=now)
    await session.client.aclose()


async def preview(rig, **kwargs):
    return await rig.submitter.submit("_1_1", "_2_1", str(rig.path), **kwargs)


async def confirm(rig, token, **kwargs):
    return await rig.submitter.submit("_1_1", "_2_1", str(rig.path),
                                     confirm=True, preview_token=token, **kwargs)


async def test_preview_reads_only_and_reports_exact_binding(rig):
    result = await preview(rig, comment="Fictional comment")
    assert result.ok
    assert result.data["submitted"] is False
    assert result.data["filename"] == rig.path.name
    assert result.data["bytes"] == rig.path.stat().st_size
    assert len(result.data["sha256"]) == 64
    assert result.data["comment"] == "Fictional comment"
    assert result.data["expires_in_seconds"] == 600
    assert result.data["preview_token"]
    assert result.data["existing_attempt_id"] is None
    assert rig.server.state.posts == []


async def test_real_chromium_upload_redirect_and_verified_history(rig):
    result = await preview(rig, comment="This is a local mock test.")
    token = result.data["preview_token"]
    submitted = await confirm(rig, token, comment="This is a local mock test.")
    assert submitted.ok and submitted.data["submitted"] is True
    assert submitted.data["attempt_id"] == "_20_1"
    assert submitted.data["receipt_id"] == "MOCK-RECEIPT-001"
    assert submitted.data["course_id"] == "_1_1"
    assert submitted.data["assignment_id"] == "_2_1"
    assert len(rig.server.state.posts) == 1
    posted = rig.server.state.posts[0]
    assert posted["files"] == [{"name": rig.path.name, "content": rig.path.read_bytes()}]
    assert posted["fields"]["student_commentstext"] == "This is a local mock test."
    assert rig.session.cookie_updates == 1
    with pytest.raises(BBError, match="预览") as error:
        await confirm(rig, token, comment="This is a local mock test.")
    assert error.value.code == "preview_required"
    assert len(rig.server.state.posts) == 1


async def test_same_attempt_transitions_to_submitted_without_separate_receipt_id(rig):
    rig.server.state.behavior = "success_same_attempt"
    token = (await preview(rig)).data["preview_token"]
    result = await confirm(rig, token)
    assert result.ok and result.data["submitted"] is True
    assert result.data["attempt_id"] == "_10_1"
    assert len(rig.server.state.posts) == 1
    assert rig.server.state.download_requests
    assert all(request["attempt_id"] == "_10_1" for request in rig.server.state.download_requests)


async def test_delayed_receipt_is_read_again_without_reposting(rig):
    rig.server.state.history_delay_reads = 4
    token = (await preview(rig)).data["preview_token"]
    result = await confirm(rig, token)
    assert result.ok and result.data["submitted"] is True
    assert rig.server.state.history_reads >= 5
    assert len(rig.server.state.posts) == 1


async def test_receipt_read_never_replays_new_attempt_get(rig):
    before = submission_form(rig.server.form("_1_1", "_2_1"), rig.server.url())
    rig.server.state.submitted = True
    rig.server.state.allow_new_attempt = True
    rig.server.state.accepted = {"files": [{"name": rig.path.name, "content": rig.path.read_bytes()}], "fields": {}}

    async def content():
        return rig.server.receipt("_1_1", "_2_1")

    page = SimpleNamespace(url=rig.server.url() + "&action=newAttempt", content=content)
    receipt = await rig.submitter._verify_receipt(page, "_1_1", "_2_1", rig.path.name,
                                                 before, rig.path.read_bytes(), rig.session.identity,
                                                 history_url=rig.server.url())
    assert receipt["attempt_id"] == "_20_1"
    assert rig.server.state.new_attempts == 0 and rig.server.state.posts == []


def existing_submission(rig):
    state = rig.server.state
    state.submitted = True
    state.allow_new_attempt = True
    state.accepted = {"files": [{"name": rig.path.name, "content": rig.path.read_bytes()}], "fields": {}}


async def test_resubmission_preview_does_not_create_attempt_or_upload(rig):
    existing_submission(rig)
    result = await preview(rig)
    assert result.ok and result.data["starts_new_attempt"]
    assert result.data["existing_attempt_id"] == "_20_1"
    assert result.data["existing_files"] == [rig.path.name]
    assert rig.server.state.new_attempts == 0
    assert rig.server.state.posts == []


async def test_confirmed_resubmission_creates_once_and_verifies_new_attempt(rig):
    existing_submission(rig)
    token = (await preview(rig)).data["preview_token"]
    result = await confirm(rig, token, comment="")
    assert result.ok and result.data["submitted"] is True
    assert result.data["attempt_id"] == "_21_1"
    assert rig.server.state.new_attempts == 1
    assert len(rig.server.state.posts) == 1
    assert rig.server.state.posts[0]["files"][0]["content"] == rig.path.read_bytes()
    assert rig.server.state.download_requests
    assert all(request["attempt_id"] == "_21_1" for request in rig.server.state.download_requests)


async def test_resubmission_permission_change_stops_before_creating_attempt(rig):
    existing_submission(rig)
    token = (await preview(rig)).data["preview_token"]
    rig.server.state.enabled = False
    with pytest.raises(BBError) as failure:
        await confirm(rig, token)
    assert failure.value.code == "submission_unavailable"
    assert rig.server.state.new_attempts == 0 and rig.server.state.posts == []


@pytest.mark.parametrize("change", ["target", "requirements"])
async def test_new_attempt_is_revalidated_before_file_upload(rig, change):
    existing_submission(rig)
    token = (await preview(rig)).data["preview_token"]
    if change == "target":
        rig.server.state.form_assignment_id = "_77_1"
    else:
        rig.server.state.browser_changed = True
    result = await confirm(rig, token)
    assert not result.ok and result.data["submitted"] is None
    assert rig.server.state.new_attempts == 1 and rig.server.state.posts == []
    assert token not in rig.submitter.pending


@pytest.mark.parametrize("behavior,uploads", [("new_attempt_old_history", 0), ("resubmit_old_receipt", 1)])
async def test_resubmission_cannot_use_old_identical_attachment_as_receipt(rig, behavior, uploads):
    existing_submission(rig)
    rig.server.state.behavior = behavior
    token = (await preview(rig)).data["preview_token"]
    result = await confirm(rig, token)
    assert not result.ok and result.data["submitted"] is None
    assert result.error["code"] == "submission_unknown"
    assert rig.server.state.new_attempts == 1
    assert len(rig.server.state.posts) == uploads
    assert token not in rig.submitter.pending


@pytest.mark.parametrize("behavior", ["wrong_attachment_bytes", "old_attempt_same_name"])
async def test_same_filename_with_wrong_attachment_content_never_reports_success(rig, behavior):
    rig.server.state.behavior = behavior
    token = (await preview(rig)).data["preview_token"]
    result = await confirm(rig, token)
    assert not result.ok
    assert result.data["submitted"] is None
    assert result.error["code"] == "submission_unknown"
    assert len(rig.server.state.posts) == 1
    if behavior == "wrong_attachment_bytes":
        assert rig.server.state.download_requests
    assert token not in rig.submitter.pending


async def test_old_attempt_with_identical_attachment_bytes_cannot_confirm_current_submission(rig):
    rig.server.state.behavior = "old_attempt_identical_bytes"
    initial = await preview(rig)
    assert initial.data["existing_attempt_id"] == "_10_1"
    token = initial.data["preview_token"]
    result = await confirm(rig, token)
    assert not result.ok and result.data["submitted"] is None
    assert result.error["code"] == "submission_unknown"
    assert len(rig.server.state.posts) == 1
    assert rig.server.state.accepted["files"][0]["content"] == rig.path.read_bytes()
    assert token not in rig.submitter.pending


async def test_comment_without_available_editor_is_rejected_before_upload(rig):
    rig.server.state.comment_available = False
    token = (await preview(rig, comment="Comment that must not disappear")).data["preview_token"]
    with pytest.raises(BBError) as error:
        await confirm(rig, token, comment="Comment that must not disappear")
    assert error.value.code == "comment_unavailable"
    assert rig.server.state.posts == []


async def test_session_change_after_binding_comparison_cannot_upload(rig, monkeypatch):
    token = (await preview(rig)).data["preview_token"]
    original = rig.session.storage_state

    async def changed_state():
        rig.session.identity = "changed-after-confirm-binding-comparison"
        return await original()

    monkeypatch.setattr(rig.session, "storage_state", changed_state)
    with pytest.raises(BBError) as error:
        await confirm(rig, token)
    assert error.value.code in {"preview_changed", "session_expired"}
    assert rig.server.state.posts == []


@pytest.mark.parametrize("token", ["", "not-a-token"])
async def test_confirm_never_writes_without_valid_preview(rig, token):
    with pytest.raises(BBError) as error:
        await confirm(rig, token)
    assert error.value.code == "preview_required"
    assert rig.server.state.posts == []


async def test_expired_preview_never_writes(rig):
    token = (await preview(rig)).data["preview_token"]
    rig.now[0] += 600
    with pytest.raises(BBError) as error:
        await confirm(rig, token)
    assert error.value.code == "preview_required"
    assert rig.server.state.posts == []


@pytest.mark.parametrize("change", ["file", "comment", "session", "requirements", "assignment", "course"])
async def test_changed_binding_never_writes(rig, change):
    token = (await preview(rig)).data["preview_token"]
    course, assignment, comment = "_1_1", "_2_1", ""
    if change == "file":
        rig.path.write_text("Changed fictional local content.", encoding="utf-8")
    elif change == "comment":
        comment = "Changed comment"
    elif change == "session":
        rig.session.identity = "different-fictional-session"
    elif change == "requirements":
        rig.server.state.requirements = "Changed fictional requirements."
    elif change == "assignment":
        assignment = "_3_1"
    elif change == "course":
        course = "_4_1"
    with pytest.raises(BBError) as error:
        await rig.submitter.submit(course, assignment, str(rig.path), comment=comment,
                                   confirm=True, preview_token=token)
    assert error.value.code == "preview_changed"
    assert rig.server.state.posts == []


async def test_changed_page_target_never_writes(rig):
    token = (await preview(rig)).data["preview_token"]
    rig.server.state.form_assignment_id = "_77_1"
    with pytest.raises(BBError) as error:
        await confirm(rig, token)
    assert error.value.code == "target_mismatch"
    assert rig.server.state.posts == []


async def test_closed_submission_never_writes(rig):
    token = (await preview(rig)).data["preview_token"]
    rig.server.state.enabled = False
    with pytest.raises(BBError) as error:
        await confirm(rig, token)
    assert error.value.code == "submission_unavailable"
    assert rig.server.state.posts == []


async def test_test_is_refused_without_opening_or_starting_it(rig):
    with pytest.raises(BBError) as error:
        await rig.submitter.submit("_1_1", "_9_1", str(rig.path))
    assert error.value.code == "unsupported_assignment"
    assert rig.server.state.posts == []


async def test_browser_revalidation_rejects_changed_conditions_before_upload(rig):
    token = (await preview(rig)).data["preview_token"]
    rig.server.state.browser_changed = True
    with pytest.raises(BBError) as error:
        await confirm(rig, token)
    assert error.value.code == "preview_changed"
    assert rig.server.state.posts == []


async def test_platform_rejection_never_reports_success_or_retries(rig):
    rig.server.state.behavior = "reject"
    token = (await preview(rig)).data["preview_token"]
    result = await confirm(rig, token)
    assert not result.ok
    assert result.data["submitted"] is not True
    assert result.error["code"] in {"submission_unknown", "submission_rejected"}
    assert len(rig.server.state.posts) == 1
    assert token not in rig.submitter.pending


@pytest.mark.parametrize("behavior,proved", [("timeout_saved", True), ("timeout_unknown", False)])
async def test_submit_response_timeout_reads_history_without_reposting(rig, behavior, proved):
    rig.server.state.behavior = behavior
    token = (await preview(rig)).data["preview_token"]
    result = await confirm(rig, token)
    assert len(rig.server.state.posts) == 1
    if proved:
        assert result.ok and result.data["submitted"] is True
        assert result.data["attempt_id"] == "_10_1"
    else:
        assert not result.ok and result.data["submitted"] is None
        assert not result.complete
        assert result.error["code"] == "submission_unknown"
    with pytest.raises(BBError) as error:
        await confirm(rig, token)
    assert error.value.code == "preview_required"
    assert len(rig.server.state.posts) == 1


async def test_oversized_or_empty_file_is_rejected_before_any_request(rig):
    rig.service.settings.max_upload_bytes = 8
    with pytest.raises(BBError) as failure:
        await preview(rig)
    assert failure.value.code == "file_too_large"
    assert failure.value.details["limit"] == 8
    rig.path.write_bytes(b"")
    rig.service.settings.max_upload_bytes = 1024
    with pytest.raises(BBError) as failure:
        await preview(rig)
    assert failure.value.code == "file_empty"
    assert rig.server.state.download_requests == []
