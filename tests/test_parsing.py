import pytest

from ustc_bb_mcp.errors import BBError
from ustc_bb_mcp.parsing import (
    content_key,
    is_authenticated_portal,
    is_login_page,
    parse_announcements,
    parse_assignment_detail,
    parse_content,
    parse_course_menu,
    parse_courses,
    parse_grades,
    safe_public_url,
    submission_form,
)

BASE = "https://www.bb.ustc.edu.cn"
COURSE = "_101_1"
PAGE = f"{BASE}/webapps/blackboard/content/listContent.jsp?course_id={COURSE}&content_id=_1_1"
ASSIGNMENT = f"{BASE}/webapps/assignment/uploadAssignment?course_id={COURSE}&content_id=_3_1"
FORM = '''<h1 id="pageTitleText">上载作业: 作业一</h1>
<form id="upload" action="/webapps/assignment/uploadAssignment" method="post">
<input type="hidden" name="course_id" value="_101_1">
<input type="hidden" name="content_id" value="_3_1">
<input type="hidden" name="csrf_token" value="private">
<input type="file" name="newFile"><textarea name="comment"></textarea>
<input type="submit" name="save" value="保存草稿">
<input type="submit" name="bottom_Submit" value="提交"></form>'''


def test_courses_and_positive_portal_marker():
    html = '''<div id="courseList"><a href="/webapps/blackboard/execute/launcher?type=Course&amp;id=_101_1">CS.2026FA: 数据结构</a>
    <a href="/webapps/blackboard/execute/launcher?type=Course&amp;id=_101_1">duplicate</a>
    <a href="/x?course_id=_102_1">Other</a></div>'''
    courses = parse_courses(html, BASE)
    assert [c.id for c in courses] == ["_101_1", "_102_1"]
    assert courses[0].term == "2026FA"
    assert is_authenticated_portal(html, BASE)
    assert not is_authenticated_portal("<title>Error</title><p>Service unavailable</p>", BASE)
    assert not is_authenticated_portal('<form id="login"><input type="password"></form>', BASE)


def test_login_and_gateway_detection():
    assert is_login_page('<input type="password" name="password">')
    assert is_login_page("您好，请验证身份", BASE + "/nginx_auth/?next=/")
    assert not is_login_page('<h1>Course</h1><a href="/logout">Logout</a>')


def test_all_content_areas_and_stable_cycle_keys():
    same = "/webapps/blackboard/content/contentWrapper.jsp?content_id=_1_1&course_id=_101_1&x=2"
    html = f'''<a href="{PAGE}">资料</a><a href="{same}">same</a>
    <a href="/webapps/blackboard/content/listContentEditable.jsp?course_id=_101_1&amp;content_id=_2_1">作业</a>
    <a href="/webapps/blackboard/execute/announcement?course_id=_101_1">公告</a>'''
    folders, announcements = parse_course_menu(html, PAGE)
    assert len(folders) == 2
    assert "announcement" in announcements
    assert content_key(PAGE) == content_key(BASE + same)


def test_content_files_assignment_test_and_unknown_date():
    html = '''<ul id="content_listContainer">
    <li><h3><a href="/bbcswebdav/pid-1/测试课件.pptx">测试课件</a></h3><p>发布日期: 2026-11-02</p></li>
    <li><a href="/bbcswebdav/pid-2/no-date.pdf">未标日期</a></li>
    <li><a href="/webapps/assignment/uploadAssignment?course_id=_101_1&amp;content_id=_3_1">作业</a><p>截止日期: 2026年11月12日 下午 11:59</p></li>
    <li><a href="/webapps/assessment/take/launch.jsp?course_id=_101_1&amp;content_id=_4_1">测试</a></li>
    <li><a href="/webapps/blackboard/content/listContent.jsp?course_id=_101_1&amp;content_id=_5_1">folder</a></li>
    <li><a href="/webapps/blackboard/content/listContent.jsp?course_id=_999_1&amp;content_id=_5_1">Other course</a></li></ul>'''
    content = parse_content(html, PAGE, COURSE)
    assert len(content.files) == 2
    assert content.files[0].date == "2026-11-02"
    assert content.files[1].date is None
    assert content.assignments[0].due_at == "2026-11-12T23:59:00+08:00"
    assert content.assignments[1].kind == "test"
    assert len(content.folders) == 1


def _row(title, state, grade="-", icon="", activity="", cid=""):
    link = f'<a href="/webapps/assignment/uploadAssignment?content_id={cid}&amp;action=showHistory">{title}</a>' if cid else title
    return f'''<div class="row {state}"><div class="gradable">{link}<div class="itemCat">作业</div></div>
    <div class="activity"><span class="activityType">{activity}</span></div>
    <div class="grade"><span class="grade">{grade}</span><span class="pointsPossible">/100</span></div>
    <div class="gradeStatus">{icon}</div></div>'''


def test_grade_zero_draft_submitted_unknown_and_duplicate_names():
    html = '<div id="grades_wrapper">' + ''.join([
        _row("零分", "graded_item_row", "0.0000", activity="Graded", cid="_3_1"),
        _row("待交", "upcoming_item_row", activity="Not graded"),
        _row("草稿", "upcoming_item_row", icon='<img src="in_progress.png">', activity="进行中"),
        _row("已交", "submitted_item_row", activity="Submitted"),
        _row("不明", "upcoming_item_row", icon='<img src="unknown.png">', activity="Not graded"),
        _row("同名", "submitted_item_row", activity="Submitted", cid="_8_1"),
        _row("同名", "upcoming_item_row", activity="Not graded"),
    ]) + '</div>'
    states = parse_grades(html)
    assert states["_3_1"] == {"status": "graded", "grade": "0.0000 /100"}
    assert states["待交"]["status"] == "not_submitted"
    assert states["草稿"]["status"] == "draft"
    assert states["已交"]["status"] == "submitted"
    assert states["不明"]["status"] == "unknown"
    assert "同名" not in states
    assert states["_8_1"]["status"] == "submitted"


def test_standard_assignment_form_without_starting_or_uploading():
    form = submission_form(FORM, ASSIGNMENT)
    assert form["can_submit"]
    assert form["course_id"] == COURSE
    assert form["assignment_id"] == "_3_1"
    assert form["file_input"] == 'input[name="newFile"]'
    assert form["comment_input"] == 'textarea[name="comment"]'
    assert form["submit_name"] == "bottom_Submit"
    assert form["hidden_fields"]["csrf_token"] == "private"
    assert not form["has_receipt"]
    assert form["existing_files"] == []


def test_ustc_real_form_file_input_id_and_tinymce_comments():
    html = '''<h1 id="pageTitleText">上载作业：习题</h1>
    <div><strong>截止日期</strong><p>2026年11月16日 星期一\n下午2:00</p></div>
    <form id="uploadAssignmentFormId" action="/webapps/assignment/uploadAssignment?action=submit">
    <input type="hidden" name="course_id" value="_101_1"><input type="hidden" name="content_id" value="_3_1">
    <input type="hidden" name="attempt_id" value="_11_1">
    <input type="file" id="newFile_chooseLocalFile" class="hiddenInput" multiple>
    <textarea name="studentSubmission.text" id="studentSubmission.text"></textarea>
    <textarea name="student_commentstext" id="student_commentstext"></textarea>
    <input type="submit" name="bottom_提交" value="提交" onclick="return checkDupeFile('submit');"></form>'''
    form = submission_form(html, ASSIGNMENT)
    assert form["can_submit"]
    assert form["file_input"] == 'input[id="newFile_chooseLocalFile"]'
    assert form["comment_input"] == 'textarea[id="student_commentstext"]'
    assert form["submit_selector"] == 'input[name="bottom_提交"]'
    detail = parse_assignment_detail(html, ASSIGNMENT, COURSE, "_3_1")
    assert detail.status == "not_submitted"
    assert detail.due_at == "2026-11-16T14:00:00+08:00"


def test_submission_body_is_never_used_as_comment_fallback():
    html = FORM.replace('<textarea name="comment"></textarea>',
                        '<textarea name="studentSubmission.text" id="studentSubmission.text"></textarea>')
    assert submission_form(html, ASSIGNMENT)["comment_input"] is None


def test_test_and_group_forms_never_submittable():
    assert not submission_form(FORM, BASE + "/webapps/assessment/take/launch.jsp")["can_submit"]
    grouped = FORM.replace('name="course_id"', 'name="group_id"')
    assert not submission_form(grouped, ASSIGNMENT)["can_submit"]


def test_assignment_detail_requirements_due_date_and_attachments():
    html = FORM + '''<div class="vtbegenerated"><p>请完成实验报告。</p>
    <a href="/bbcswebdav/assignment.pdf">实验说明.pdf</a></div>
    <dl><dt>Due Date</dt><dd>November 12, 2026 11:59 PM</dd></dl>'''
    detail = parse_assignment_detail(html, ASSIGNMENT, COURSE, "_3_1", "实验")
    assert detail.name == "实验"
    assert detail.status == "not_submitted"
    assert detail.due_at == "2026-11-12T23:59:00+08:00"
    assert detail.due_text == "November 12, 2026 11:59 PM"
    assert "请完成实验报告" in detail.requirements
    assert detail.attachments[0].name == "实验说明.pdf"


def test_history_receipt_is_distinct_from_assignment_attachment():
    html = '''<h1 id="pageTitleText">Review Submission History</h1>
    <div class="vtbegenerated"><a href="/bbcswebdav/instructions.pdf">instructions.pdf</a></div>
    <div id="submittedFiles"><a href="/bbcswebdav/report.pdf">report.pdf</a></div>
    <span id="confirmationNumber">ABCD-1234</span><input name="attempt_id" value="_11_1">
    <input id="aggregateGrade" value="0"><span id="aggregateGrade_pointsPossible">/100</span>'''
    form = submission_form(html, ASSIGNMENT)
    assert form["has_receipt"] and form["receipt_id"] == "ABCD-1234"
    assert form["existing_files"] == ["report.pdf"]
    assert form["attempt_id"] == "_11_1"
    assert not form["can_submit"]
    detail = parse_assignment_detail(html, ASSIGNMENT, COURSE, "_3_1")
    assert detail.status == "graded" and detail.grade == "0 /100"


def test_ustc_history_download_endpoint_supplies_filename_and_attempt_identity():
    html = '''<h1 id="pageTitleText">复查提交历史记录: hw1-2</h1>
    <input id="aggregateGrade" value="10.00000">
    <span id="aggregateGrade_pointsPossible">/10</span>
    <a href="/webapps/assignment/download?course_id=_101_1&amp;attempt_id=_1605851_1&amp;file_id=_20_1&amp;fileName=hw1-2.md">下载</a>
    <a href="#">复查提交历史记录</a>'''
    form = submission_form(html, ASSIGNMENT)
    assert form["attempt_id"] == "_1605851_1"
    assert form["existing_files"] == ["hw1-2.md"]
    assert form["has_receipt"]
    assert form["history_links"] == []
    content = parse_content(html, ASSIGNMENT, COURSE)
    assert not content.assignments
    assert content.files[0].name == "hw1-2.md"


def test_preallocated_attempt_id_survives_successful_history_without_receipt_number():
    before = submission_form(FORM.replace('name="csrf_token" value="private"', 'name="attempt_id" value="_1605851_1"'), ASSIGNMENT)
    html = '''<h1 id="pageTitleText">复查提交历史记录: hw1-2</h1>
    <a href="/webapps/assignment/download?course_id=_101_1&amp;attempt_id=_1605851_1&amp;file_id=_20_1&amp;fileName=hw1-2.md">下载</a>
    <a href="/webapps/assignment/download?fileName=hw1-2.md&amp;file_id=_20_1&amp;attempt_id=_1605851_1&amp;course_id=_101_1">重复链接</a>'''
    history = submission_form(html, ASSIGNMENT)
    assert history["attempt_id"] == before["attempt_id"] == "_1605851_1"
    assert history["receipt_id"] is None and history["has_receipt"]
    assert history["existing_files"] == ["hw1-2.md"]
    assert history["submitted_files"] == [{
        "name": "hw1-2.md",
        "url": BASE + "/webapps/assignment/download?course_id=_101_1&attempt_id=_1605851_1&file_id=_20_1&fileName=hw1-2.md",
        "attempt_id": "_1605851_1", "file_id": "_20_1",
    }]


def test_history_exposes_target_bound_new_attempt_without_opening_it():
    target = f"/webapps/assignment/uploadAssignment?action=newAttempt&course_id={COURSE}&content_id=_3_1"
    html = f'''<h1 id="pageTitleText">复查提交历史记录</h1>
    <input name="attempt_id" value="_11_1"><span id="confirmationNumber">MOCK-1111</span>
    <input type="submit" name="bottom_开始新的" value="开始新的" onclick="document.location='{target}';">'''
    info = submission_form(html, ASSIGNMENT)
    assert info["can_start_new_attempt"] and not info["can_submit"]
    assert info["new_attempt_url"] == BASE + target
    assert info["new_attempt_selector"] == 'input[name="bottom_开始新的"]'


@pytest.mark.parametrize("change", ["foreign", "course", "assignment", "action", "disabled", "script"])
def test_new_attempt_control_must_be_enabled_and_bound_to_current_assignment(change):
    target = f"{BASE}/webapps/assignment/uploadAssignment?action=newAttempt&course_id={COURSE}&content_id=_3_1"
    disabled = ""
    if change == "foreign":
        target = target.replace(BASE, "https://foreign.invalid")
    elif change == "course":
        target = target.replace(COURSE, "_999_1")
    elif change == "assignment":
        target = target.replace("_3_1", "_999_1")
    elif change == "action":
        target = target.replace("newAttempt", "deleteAttempt")
    elif change == "disabled":
        disabled = "disabled"
    onclick = f"document.location='{target}';"
    if change == "script":
        onclick = "doSomething();" + onclick
    html = f'''<h1 id="pageTitleText">Review Submission History</h1>
    <input name="attempt_id" value="_11_1"><span id="confirmationNumber">MOCK-1111</span>
    <input type="submit" name="start" value="Start New" {disabled} onclick="{onclick}">'''
    assert not submission_form(html, ASSIGNMENT)["can_start_new_attempt"]


def test_submitted_file_evidence_requires_actual_same_origin_download_links():
    html = '''<h1 id="pageTitleText">Review Submission History</h1>
    <div id="submittedFiles"><a href="/bbcswebdav/instructions.pdf">instructions.pdf</a></div>
    <a href="https://other.invalid/webapps/assignment/download?attempt_id=_1_1&amp;file_id=_2_1&amp;fileName=evil.pdf">下载</a>
    <a href="/webapps/assignment/downloadExtra?attempt_id=_1_1&amp;file_id=_2_1&amp;fileName=wrong.pdf">下载</a>
    <a href="/webapps/assignment/download?course_id=_999_1&amp;attempt_id=_1_1&amp;file_id=_2_1&amp;fileName=wrong-course.pdf">下载</a>
    <a href="/webapps/assignment/download?attempt_id=_1_1&amp;fileName=missing-file-id.pdf">下载</a>
    <a href="/webapps/assignment/download?attempt_id=_1_1&amp;file_id=_2_1">下载</a>'''
    assert submission_form(html, ASSIGNMENT)["submitted_files"] == []


@pytest.mark.parametrize("field,value", [("course_id", "_999_1"), ("content_id", "_999_1"), ("assignment_id", "_999_1")])
def test_detail_rejects_explicit_target_mismatch(field, value):
    html = f'''<h1 id="pageTitleText">复查提交历史记录</h1><input type="hidden" name="{field}" value="{value}">
    <input id="aggregateGrade" value="100">'''
    with pytest.raises(BBError) as error:
        parse_assignment_detail(html, ASSIGNMENT, COURSE, "_3_1")
    assert error.value.code == "target_mismatch"


def test_detail_missing_ids_does_not_assert_a_false_mismatch():
    detail = parse_assignment_detail('<h1 id="pageTitleText">复查提交历史记录</h1><input id="aggregateGrade" value="0">',
                                     BASE + "/history", COURSE, "_3_1")
    assert detail.status == "graded" and detail.grade == "0"


def test_mentioning_drafts_in_instructions_does_not_change_submission_status():
    html = FORM + '<div class="vtbegenerated">请勿保存草稿，请提交完整附件。</div>'
    detail = parse_assignment_detail(html, ASSIGNMENT, COURSE, "_3_1")
    assert detail.status == "not_submitted"


def test_dates_without_time_do_not_invent_midnight_or_missing_due():
    detail = parse_assignment_detail('<h1 id="pageTitleText">Homework</h1><p>Due Date: 2026-11-12</p>', ASSIGNMENT, COURSE, "_3_1")
    assert detail.due_at == "2026-11-12"
    assert parse_assignment_detail('<p>Published: 2026-11-12</p>', ASSIGNMENT, COURSE, "_3_1").due_at is None


@pytest.mark.parametrize("html", [
    '<div class="vtbegenerated">截止日期: 2026-11-12，以上仅为示例。</div>',
    '<p>说明中提到截止日期 2026-11-12，但未设置平台截止日期。</p>',
    '<div><span>Due Date</span><span>Not set</span><p>Published: 2026-11-12</p></div>',
])
def test_due_date_is_not_taken_from_instructions_or_unrelated_dates(html):
    detail = parse_assignment_detail(html, ASSIGNMENT, COURSE, "_3_1")
    assert detail.due_at is None


def test_announcements_and_dates():
    html = '''<ul id="announcementList"><li id="ann_1"><h3>期末安排</h3>
    <div class="vtbegenerated"><p>考试范围为第一至六章。</p></div><span>2026-11-10</span></li>
    <li id="ann_2"><h3>Office hours</h3><div class="announcementBody">Friday afternoon.</div></li></ul>'''
    notices = parse_announcements(html, BASE + "/announcements?course_id=_101_1", COURSE)
    assert [n.title for n in notices] == ["期末安排", "Office hours"]
    assert notices[0].date == "2026-11-10"
    assert notices[1].date is None
    assert "第一至六章" in notices[0].body


def test_public_links_remove_secrets_and_credentials():
    url = BASE.replace("https://", "https://user:password@") + "/x;jsessionid=secret?course_id=_1_1&ticket=ST-private&X-Amz-Signature=private&csrf_token=private#secret"
    assert safe_public_url(url) == BASE + "/x?course_id=_1_1"
    assert safe_public_url("javascript:alert(1)") == ""



def test_announcement_reader_ignores_module_settings_and_prefers_full_reader():
    settings = "/webapps/portal/execute/tabs/tabAction?course_id=_101_1&forwardUrl=edit_module%2Fbbannouncements%3Fcmd%3Dedit"
    partial = "/webapps/blackboard/execute/announcement?course_id=_101_1&method=search&viewChoice=2"
    full = "/webapps/blackboard/execute/announcement?course_id=_101_1&method=search&viewChoice=3"
    html = f'<a href="{settings}">管理我的公告</a><a href="{partial}">通知</a><a href="{full}">更多公告</a>'
    _, target = parse_course_menu(html, PAGE)
    assert target == BASE + full
    _, missing = parse_course_menu(f'<a href="{settings}">管理公告模块</a>', PAGE)
    assert missing is None


def test_ustc_meta_label_due_field_preserves_date_and_time():
    html = FORM + '<p>发布时间：2026年11月1日</p><div class="metaSection"><div class="metaLabel" id="assignMeta2">截止日期</div><div class="metaField" aria-describedby="assignMeta2">2026年11月16日 星期一<span class="metaSubInfo">下午2:00</span></div></div>'
    detail = parse_assignment_detail(html, ASSIGNMENT, COURSE, "_3_1")
    assert detail.due_at == "2026-11-16T14:00:00+08:00"


@pytest.mark.parametrize("audience", ["每个人", "Everyone"])
def test_course_announcements_exclude_pinned_system_messages_and_keep_images(audience):
    html = f'''<ul id="announcementList">
    <li id="system"><h3>平台通知</h3><div class="vtbegenerated">系统维护</div><div class="announcementInfo"><p><span>发布至:</span> {audience}</p></div></li>
    <li id="course"><h3>课程通知</h3><div class="vtbegenerated"><img alt="课程图示" src="/bbcswebdav/diagram.png"></div><div class="announcementInfo"><p><span>发布至:</span> 数据结构</p></div></li></ul>'''
    items = parse_announcements(html, BASE + "/webapps/blackboard/execute/announcement?course_id=" + COURSE, COURSE)
    assert [item.id for item in items] == ["course"]
    assert "课程图示" in items[0].body and BASE + "/bbcswebdav/diagram.png" in items[0].body
