from types import SimpleNamespace

import httpx
import pytest

from ustc_bb_mcp.errors import BBError
from ustc_bb_mcp.models import Assignment, Course
from ustc_bb_mcp.service import Blackboard, choose

BASE = "https://www.bb.ustc.edu.cn"
LAUNCHER = "/webapps/blackboard/execute/launcher"
CONTENT = "/webapps/blackboard/content/listContent.jsp"
WRAPPER = "/webapps/blackboard/content/contentWrapper.jsp"
PORTAL = "/webapps/portal/execute/tabs/tabAction"
GRADES = "/webapps/bb-mygrades-BBLEARN/myGrades"
UPLOAD = "/webapps/assignment/uploadAssignment"
ANNOUNCEMENTS = "/webapps/blackboard/execute/announcement"


def content_url(content, course="_101_1"):
    return f"{CONTENT}?course_id={course}&content_id={content}"


def assignment_url(assignment, course="_101_1"):
    return f"{UPLOAD}?course_id={course}&content_id={assignment}"


def portal(*courses):
    return '<div id="courseList">' + "".join(
        f'<a href="{LAUNCHER}?type=Course&amp;id={course_id}">{title}</a>'
        for course_id, title in courses
    ) + "</div>"


def page(*items):
    return '<ul id="content_listContainer">' + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def link(url, title):
    return f'<a href="{url}">{title}</a>'


def upload_form(assignment, course="_101_1"):
    return f'''<h1 id="pageTitleText">上载作业</h1>
    <form method="post" action="{UPLOAD}">
    <input type="hidden" name="course_id" value="{course}">
    <input type="hidden" name="content_id" value="{assignment}">
    <input type="file" name="newFile"><input type="submit" name="submit" value="提交">
    </form>'''


def settings(tmp_path, **overrides):
    return SimpleNamespace(**{"base_url": BASE, "output_dir": tmp_path, "max_pages": 500, "cache_ttl": 60, **overrides})


def course(course_id="_101_1", title="数据结构"):
    return Course(id=course_id, name=title, url=BASE + f"{LAUNCHER}?type=Course&id={course_id}")


def test_choose_prefers_id_and_exact_name_and_never_first_ambiguous_match():
    courses = [course(title="数据结构 A"), course("_102_1", "数据结构 B")]
    assert choose(courses, "_102_1", "课程").name == "数据结构 B"
    assert choose(courses, "数据结构 A", "课程").id == "_101_1"
    with pytest.raises(BBError) as error:
        choose(courses, "数据结构", "课程")
    assert error.value.code == "ambiguous_selection"
    assert {item["id"] for item in error.value.details["candidates"]} == {"_101_1", "_102_1"}


def test_choose_duplicate_exact_names_and_empty_or_missing_query():
    courses = [course(), course("_102_1")]
    with pytest.raises(BBError, match="多个"):
        choose(courses, "数据结构", "课程")
    with pytest.raises(BBError) as error:
        choose(courses, "   ", "课程")
    assert error.value.code == "invalid_argument"
    with pytest.raises(BBError) as error:
        choose(courses, "量子力学", "课程")
    assert error.value.code == "not_found"


async def test_scan_all_menus_four_levels_cycle_and_sort_without_invented_dates(
    session_factory, tmp_path
):
    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        if request.url.path == LAUNCHER:
            return httpx.Response(200, text=link(content_url("_1_1"), "资料") + link(content_url("_2_1"), "额外资料"))
        cid = request.url.params.get("content_id")
        pages = {
            "_1_1": page(link(content_url("_3_1"), "第2层")),
            "_2_1": page(
                link("/bbcswebdav/undated.pdf", "无日期课件"),
                link("/bbcswebdav/old.pdf", "旧课件") + "<p>2026-10-01</p>",
            ),
            "_3_1": page(link(content_url("_4_1"), "第3层")),
            "_4_1": page(link(content_url("_5_1"), "第4层")),
            "_5_1": page(
                link("/bbcswebdav/deep.pdf", "深层课件") + "<p>2026-11-11</p>",
                link(f"{WRAPPER}?content_id=_1_1&course_id=_101_1&wrapper=1", "返回根目录"),
            ),
        }
        return httpx.Response(200, text=pages[cid])

    session = session_factory(handle)
    result = await Blackboard(session, settings(tmp_path)).list_files("数据结构")
    assert result.complete
    assert [item["name"] for item in result.data["files"]] == ["深层课件", "旧课件", "无日期课件"]
    assert result.data["files"][-1]["date"] is None
    content_requests = [r for r in session.requests if r.url.path in {CONTENT, WRAPPER}]
    assert len([request for request in content_requests if request.url.path == CONTENT]) == 5
    assert len(content_requests) <= 6  # A wrapper may need one separate transport request.
    assert {r.url.params["content_id"] for r in content_requests} == {f"_{i}_1" for i in range(1, 6)}


async def test_scan_partial_failure_keeps_good_files(session_factory, tmp_path):
    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        if request.url.path == LAUNCHER:
            return httpx.Response(200, text=link(content_url("_1_1"), "资料") + link(content_url("_2_1"), "不可访问"))
        if request.url.params["content_id"] == "_1_1":
            return httpx.Response(200, text=page(link("/bbcswebdav/available.pdf", "可用文件")))
        return httpx.Response(503)

    result = await Blackboard(session_factory(handle), settings(tmp_path)).list_files("_101_1")
    assert not result.complete
    assert len(result.data["files"]) == 1
    assert result.warnings and "读取失败" in result.warnings[0]


async def test_content_wrapper_frame_with_same_content_id_is_not_skipped(session_factory, tmp_path):
    wrapped = f"{WRAPPER}?course_id=_101_1&content_id=_1_1"

    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        if request.url.path == LAUNCHER:
            return httpx.Response(200, text=link(wrapped, "课程内容"))
        if request.url.path == WRAPPER:
            return httpx.Response(200, text=f'<iframe src="{content_url("_1_1")}"></iframe>')
        return httpx.Response(200, text=page(link("/bbcswebdav/inside.pdf", "内层课件")))

    session = session_factory(handle)
    result = await Blackboard(session, settings(tmp_path)).list_files("_101_1")
    assert result.complete
    assert [item["name"] for item in result.data["files"]] == ["内层课件"]
    assert len([request for request in session.requests if request.url.path == CONTENT]) == 1


async def test_course_landing_directly_on_wrapper_content_can_read_its_files(session_factory, tmp_path):
    wrapped = f"{WRAPPER}?course_id=_101_1&content_id=_1_1"

    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text='<div id="courseList">' + link(wrapped, "数据结构") + "</div>")
        return httpx.Response(200, text=page(link("/bbcswebdav/direct.pdf", "直接课件")))

    result = await Blackboard(session_factory(handle), settings(tmp_path)).list_files("_101_1")
    assert result.complete
    assert [item["name"] for item in result.data["files"]] == ["直接课件"]


@pytest.mark.parametrize("returned_course,returned_assignment", [
    ("_999_1", "_11_1"), ("_101_1", "_999_1"), ("_999_1", "_999_1"),
])
async def test_assignment_detail_explicit_target_mismatch_is_rejected(
    session_factory, tmp_path, returned_course, returned_assignment
):
    html = f'''<h1 id="pageTitleText">复查提交历史记录</h1>
    <input name="course_id" value="{returned_course}">
    <input name="content_id" value="{returned_assignment}">
    <input id="aggregateGrade" value="91">'''
    session = session_factory(lambda request: httpx.Response(200, text=html))
    assignment = Assignment(id="_11_1", course_id="_101_1", name="作业", url=BASE + assignment_url("_11_1"))
    with pytest.raises(BBError) as error:
        await Blackboard(session, settings(tmp_path)).detail(assignment)
    assert error.value.code == "target_mismatch"


async def test_scan_page_limit_and_unrecognized_structure_are_incomplete(session_factory, tmp_path):
    def handle(request):
        if request.url.path == LAUNCHER:
            return httpx.Response(200, text=link(content_url("_1_1"), "资料"))
        if request.url.params["content_id"] == "_1_1":
            return httpx.Response(200, text=page(link(content_url("_2_1"), "下一层")))
        return httpx.Response(200, text="<p>Unknown replacement layout</p>")

    session = session_factory(handle)
    config = settings(tmp_path)
    config.max_pages = 1
    result = await Blackboard(session, config).scan(course())
    assert "上限" in result[3][0]
    assert len(session.requests) == 2
    config.max_pages = 10
    result = await Blackboard(session_factory(handle), config).scan(course())
    assert any("布局变化" in warning for warning in result[3])


async def test_courses_unknown_layout_is_not_a_successful_empty_list(session_factory, tmp_path):
    session = session_factory(lambda request: httpx.Response(200, text="<h1>Unexpected page</h1>"))
    with pytest.raises(BBError) as error:
        await Blackboard(session, settings(tmp_path)).list_courses()
    assert error.value.code == "layout_changed"


async def test_unrecognized_course_menu_cannot_claim_complete_empty_scan(session_factory, tmp_path):
    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        return httpx.Response(200, text="<h1>Unrecognized course menu layout</h1>")

    result = await Blackboard(session_factory(handle), settings(tmp_path)).list_files("_101_1")
    assert not result.complete
    assert result.warnings
    assert result.data["files"] == []


async def test_recognized_course_menu_without_content_areas_can_be_empty(session_factory, tmp_path):
    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        return httpx.Response(200, text='''<ul id="courseMenuPalette_contents">
        <li><a href="/webapps/blackboard/execute/announcement?course_id=_101_1">通知</a></li>
        <li><a href="/webapps/blackboard/execute/tool?course_id=_101_1">工具</a></li></ul>''')

    result = await Blackboard(session_factory(handle), settings(tmp_path)).list_files("_101_1")
    assert result.complete
    assert result.data["files"] == []
    assert not result.warnings


async def test_assignments_missing_grades_fall_back_without_launching_test(session_factory, tmp_path):
    test_path = "/webapps/assessment/take/launch.jsp"

    def handle(request):
        path = request.url.path
        if path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        if path == LAUNCHER:
            return httpx.Response(200, text=link(content_url("_1_1"), "作业区"))
        if path == CONTENT:
            return httpx.Response(200, text=page(
                link(assignment_url("_11_1"), "实验一"),
                link(f"{test_path}?course_id=_101_1&content_id=_12_1", "在线测试"),
            ))
        if path == GRADES:
            return httpx.Response(200, text='<div id="grades_wrapper"></div>')
        if path == UPLOAD:
            return httpx.Response(200, text=upload_form("_11_1"))
        raise AssertionError(f"unexpected request {request.url}")

    session = session_factory(handle)
    result = await Blackboard(session, settings(tmp_path)).list_assignments("_101_1")
    assert result.complete
    items = result.data["assignments"]
    assert items[0]["status"] == "not_submitted"
    assert items[1]["kind"] == "test" and items[1]["status"] == "unknown"
    assert not any(r.url.path == test_path for r in session.requests)
    assert len([r for r in session.requests if r.url.path == UPLOAD]) == 1


async def test_grade_title_duplicate_falls_back_per_id_and_zero_is_preserved(session_factory, tmp_path):
    def handle(request):
        path = request.url.path
        if path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        if path == LAUNCHER:
            return httpx.Response(200, text=link(content_url("_1_1"), "作业区"))
        if path == CONTENT:
            return httpx.Response(200, text=page(link(assignment_url("_11_1"), "同名作业"), link(assignment_url("_12_1"), "同名作业")))
        if path == GRADES:
            return httpx.Response(200, text='''<div id="grades_wrapper"><div class="row submitted_item_row">
            <div class="gradable">同名作业<div class="itemCat">作业</div></div>
            <div class="activity"><span class="activityType">已提交</span></div>
            <div class="grade"><span class="grade">-</span></div></div></div>''')
        if request.url.params["content_id"] == "_11_1":
            return httpx.Response(200, text=upload_form("_11_1"))
        return httpx.Response(200, text='''<h1 id="pageTitleText">复查提交历史记录</h1>
        <input id="aggregateGrade" value="0"><span id="aggregateGrade_pointsPossible">/100</span>''')

    session = session_factory(handle)
    result = await Blackboard(session, settings(tmp_path)).list_assignments("_101_1")
    assert result.complete
    items = {item["id"]: item for item in result.data["assignments"]}
    assert items["_11_1"]["status"] == "not_submitted"
    assert items["_12_1"]["status"] == "graded"
    assert items["_12_1"]["grade"].startswith("0")
    assert len([r for r in session.requests if r.url.path == UPLOAD]) == 2


async def test_all_assignments_keeps_successes_and_reports_course_failure(session_factory, tmp_path):
    def handle(request):
        path = request.url.path
        if path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构"), ("_102_1", "电磁学")))
        if path == LAUNCHER:
            if request.url.params["id"] == "_102_1":
                return httpx.Response(403)
            return httpx.Response(200, text=link(content_url("_1_1"), "作业区"))
        if path == CONTENT:
            return httpx.Response(200, text=page(link(assignment_url("_11_1"), "实验一")))
        if path == GRADES:
            return httpx.Response(503)
        return httpx.Response(200, text=upload_form("_11_1"))

    result = await Blackboard(session_factory(handle), settings(tmp_path)).list_assignments("all")
    assert not result.complete
    assert len(result.data["assignments"]) == 1
    assert result.data["assignments"][0]["course_name"] == "数据结构"
    assert any("电磁学" in warning for warning in result.warnings)


async def test_unknown_assignment_status_marks_result_incomplete(session_factory, tmp_path):
    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        if request.url.path == LAUNCHER:
            return httpx.Response(200, text=link(content_url("_1_1"), "作业区"))
        if request.url.path == CONTENT:
            return httpx.Response(200, text=page(link(assignment_url("_11_1"), "实验一")))
        if request.url.path == GRADES:
            return httpx.Response(200, text='<div id="grades_wrapper"></div>')
        return httpx.Response(200, text="<p>Unrecognized assignment layout</p>")

    result = await Blackboard(session_factory(handle), settings(tmp_path)).list_assignments("all")
    assert not result.complete
    assert result.data["assignments"][0]["status"] == "unknown"
    assert any("无法确认" in warning for warning in result.warnings)


async def test_assignment_details_ambiguous_keyword_requires_explicit_choice(session_factory, tmp_path):
    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        if request.url.path == LAUNCHER:
            return httpx.Response(200, text=link(content_url("_1_1"), "作业区"))
        return httpx.Response(200, text=page(link(assignment_url("_11_1"), "实验一"), link(assignment_url("_12_1"), "实验二")))

    session = session_factory(handle)
    with pytest.raises(BBError) as error:
        await Blackboard(session, settings(tmp_path)).assignment_details("_101_1", "实验", False)
    assert error.value.code == "ambiguous_selection"
    assert not any(r.url.path == UPLOAD for r in session.requests)


async def test_all_announcements_aggregate_per_course_limit_and_partial_failure(session_factory, tmp_path):
    def handle(request):
        path = request.url.path
        if path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构"), ("_102_1", "电磁学"), ("_103_1", "数学")))
        if path == LAUNCHER:
            cid = request.url.params["id"]
            return httpx.Response(200, text=link(f"{ANNOUNCEMENTS}?course_id={cid}", "公告"))
        cid = request.url.params["course_id"]
        if cid == "_103_1":
            return httpx.Response(503)
        return httpx.Response(200, text='''<ul id="announcementList">
        <li id="announcement_1"><h3>期末复习</h3><div class="vtbegenerated">请复习</div></li>
        <li id="announcement_2"><h3>课件更新</h3><div class="vtbegenerated">已更新</div></li></ul>''')

    result = await Blackboard(session_factory(handle), settings(tmp_path)).announcements("all", limit=1)
    assert not result.complete
    assert len(result.data["announcements"]) == 2
    assert {item["course_id"] for item in result.data["announcements"]} == {"_101_1", "_102_1"}
    assert all(item["title"] == "期末复习" for item in result.data["announcements"])
    assert any("数学" in warning for warning in result.warnings)


@pytest.mark.parametrize("value", [0, -1, 1001, True])
async def test_invalid_download_count_and_announcement_limit_fail_before_network(
    session_factory, tmp_path, value
):
    def handle(request):
        raise AssertionError("invalid parameters must not trigger network requests")

    service = Blackboard(session_factory(handle), settings(tmp_path))
    with pytest.raises(BBError) as error:
        await service.download_courseware("课程", latest=value)
    assert error.value.code == "invalid_argument"
    with pytest.raises(BBError) as error:
        await service.announcements("all", limit=value)
    assert error.value.code == "invalid_argument"


async def test_course_and_scan_cache_reuses_complete_results_per_identity(session_factory, tmp_path):
    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        if request.url.path == LAUNCHER:
            return httpx.Response(200, text=link(content_url("_1_1"), "资料"))
        return httpx.Response(200, text=page(link("/bbcswebdav/a.pdf", "课件")))

    now = [1000.0]
    session = session_factory(handle)
    service = Blackboard(session, settings(tmp_path), clock=lambda: now[0])
    first = await service.list_files("_101_1")
    requests_after_first = len(session.requests)
    second = await service.list_files("_101_1")
    assert first.data == second.data and second.complete
    assert len(session.requests) == requests_after_first  # fully served from cache
    # Mutating a returned item never leaks into the cache.
    (await service.scan(course()))[0][0].name = "changed"
    assert (await service.scan(course()))[0][0].name == "课件"
    # The TTL expires and a different login identity never shares entries.
    now[0] += 61
    await service.list_files("_101_1")
    assert len(session.requests) > requests_after_first
    count = len(session.requests)
    session.identity = "another-account"
    await service.list_files("_101_1")
    assert len(session.requests) > count
    service.invalidate()
    assert not service._cache


async def test_incomplete_scan_is_never_cached(session_factory, tmp_path):
    failures = [True]

    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        if request.url.path == LAUNCHER:
            return httpx.Response(200, text=link(content_url("_1_1"), "资料"))
        if failures and failures.pop():
            return httpx.Response(503)
        return httpx.Response(200, text=page(link("/bbcswebdav/a.pdf", "课件")))

    service = Blackboard(session_factory(handle), settings(tmp_path))
    assert not (await service.list_files("_101_1")).complete
    recovered = await service.list_files("_101_1")
    assert recovered.complete and len(recovered.data["files"]) == 1


async def test_cache_disabled_by_zero_ttl(session_factory, tmp_path):
    def handle(request):
        if request.url.path == PORTAL:
            return httpx.Response(200, text=portal(("_101_1", "数据结构")))
        return httpx.Response(200, text='<div id="courseMenuPalette_contents"></div>')

    session = session_factory(handle)
    service = Blackboard(session, settings(tmp_path, cache_ttl=0))
    await service.list_courses()
    await service.list_courses()
    assert len(session.requests) == 2
