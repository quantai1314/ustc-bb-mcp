"""Blackboard read operations; content is data, never instructions for the agent."""

import asyncio
import time
from collections import Counter, deque
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .downloads import Downloader
from .errors import BBError
from .models import Course, Result
from .parsing import (
    content_key,
    parse_announcements,
    parse_assignment_detail,
    parse_content,
    parse_course_menu,
    parse_courses,
    parse_grades,
)

PORTAL = "/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1"


def choose(items, query: str, label: str):
    query = query.strip()
    if not query:
        raise BBError("invalid_argument", f"请输入{label} ID 或名称。")
    for key in ("id", "name"):
        matches = [item for item in items if getattr(item, key).casefold() == query.casefold()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            break
    else:
        matches = [item for item in items if query.casefold() in item.name.casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise BBError("not_found", f"未找到匹配的{label}。",
                      details={"candidates": [{"id": i.id, "name": i.name} for i in items]})
    raise BBError("ambiguous_selection", f"匹配到多个{label}，请使用准确 ID。",
                  details={"candidates": [{"id": i.id, "name": i.name} for i in matches]})


class Blackboard:
    def __init__(self, session, settings, *, clock=time.monotonic):
        self.session = session
        self.settings = settings
        self.downloader = Downloader(session, settings.output_dir)
        self.clock = clock
        # Short-lived read cache: the course list and content-tree scans are
        # fetched by almost every tool, and `course="all"` repeats them per course.
        # Entries are keyed by the authenticated identity so a re-login never
        # serves another account's data. Only complete results are cached.
        self._cache: dict[tuple, tuple[float, object]] = {}

    def url(self, path):
        return urljoin(self.settings.base_url + "/", path)

    def _cached(self, *key):
        entry = self._cache.get((self.session.identity, *key))
        if entry and entry[0] > self.clock():
            return entry[1]
        return None

    def _remember(self, value, *key):
        if self.settings.cache_ttl > 0:
            self._cache[(self.session.identity, *key)] = (self.clock() + self.settings.cache_ttl, value)
        return value

    def invalidate(self) -> None:
        self._cache.clear()

    async def courses(self) -> list[Course]:
        cached = self._cached("courses")
        if cached is not None:
            return [course.model_copy() for course in cached]
        response = await self.session.get(self.url(PORTAL))
        courses = parse_courses(response.text, str(response.url))
        if not courses and not any(word in response.text for word in ("无课程", "未注册", "No courses", "not enrolled")):
            raise BBError("layout_changed", "门户中未识别到课程，不能确认账号没有课程。")
        self._remember(courses, "courses")
        return [course.model_copy() for course in courses]

    async def list_courses(self) -> Result:
        courses = await self.courses()
        return Result(message=f"读取到 {len(courses)} 门课程。", data={"courses": [c.model_dump() for c in courses]})

    async def select_course(self, query):
        return choose(await self.courses(), query, "课程")

    async def scan(self, course):
        cached = self._cached("scan", course.id)
        if cached is not None:
            files, assignments, announcement_url = cached
            return ([f.model_copy() for f in files], [a.model_copy(deep=True) for a in assignments],
                    announcement_url, [])
        result = await self._scan(course)
        files, assignments, announcement_url, warnings = result
        if not warnings:
            self._remember((files, assignments, announcement_url), "scan", course.id)
            return ([f.model_copy() for f in files], [a.model_copy(deep=True) for a in assignments],
                    announcement_url, [])
        return result

    async def _scan(self, course):
        response = await self.session.get(course.url)
        roots, announcement_url = parse_course_menu(response.text, str(response.url))
        # Some courses open straight into a content area.
        if not roots and ("listContent" in str(response.url) or BeautifulSoup(response.text, "html.parser").select_one("#content_listContainer, #contentList, .contentList")):
            roots = [str(response.url)]
        pending, visited = deque(roots), set()
        files, assignments, warnings = {}, {}, []
        if not roots and not BeautifulSoup(response.text, "html.parser").select_one("#courseMenuPalette_contents, #navigationPane"):
            warnings.append(f"{course.name}：未识别到课程菜单结构，不能确认没有内容区。")
        while pending:
            current = pending.popleft()
            key = content_key(current)
            # A wrapper and its iframe can share the same Blackboard content ID.
            # They are one logical item but two distinct documents to read.
            if "contentwrapper" in urlsplit(current).path.lower():
                key = "wrapper:" + key
            if key in visited:
                continue
            if len(visited) >= self.settings.max_pages:
                warnings.append(f"{course.name}：扫描达到 {self.settings.max_pages} 页上限，结果不完整。")
                break
            visited.add(key)
            try:
                page = await self.session.get(current)
                content = parse_content(page.text, str(page.url), course.id)
                if not any(marker in page.text for marker in ("content_listContainer", "contentList", "No items", "没有可用", "无内容")) and not content.files and not content.assignments and not content.folders:
                    warnings.append(f"{course.name}：有内容区未识别到列表结构，可能是布局变化。")
                for file in content.files:
                    files.setdefault(file.id, file)
                for assignment in content.assignments:
                    assignments.setdefault(assignment.id, assignment)
                pending.extend(content.folders)
            except BBError as error:
                warnings.append(f"{course.name}：一个内容区读取失败（{error.code}）。")
        return list(files.values()), list(assignments.values()), announcement_url, warnings

    async def list_files(self, course_query):
        course = await self.select_course(course_query)
        files, _, _, warnings = await self.scan(course)
        files.sort(key=lambda file: (file.date is not None, file.date or ""), reverse=True)
        return Result(message=f"找到 {len(files)} 个课程文件。", data={"course": course.model_dump(), "files": [f.model_dump() for f in files]}, complete=not warnings, warnings=warnings)

    async def download_courseware(self, course_query, keyword="", latest=2):
        if isinstance(latest, bool) or not 1 <= latest <= 1000:
            raise BBError("invalid_argument", "latest 应为 1 到 1000 的整数。")
        course = await self.select_course(course_query)
        files, _, _, warnings = await self.scan(course)
        files.sort(key=lambda file: (file.date is not None, file.date or ""), reverse=True)
        selected = [f for f in files if keyword.casefold() in f.name.casefold()][:latest]
        saved = []
        for file in selected:
            try:
                saved.append(await self.downloader.download(file, course.name))
            except (BBError, OSError) as error:
                warnings.append(f"{file.name}：下载失败（{getattr(error, 'code', 'local_io_error')}）。")
        return Result(message=f"处理 {len(selected)} 个文件，成功保存或复用 {len(saved)} 个。", data={"course": course.model_dump(), "downloads": saved}, complete=not warnings, warnings=warnings)

    async def _statuses(self, course, assignments, warnings):
        query = urlencode({"course_id": course.id, "stream_name": "mygrades", "is_stream": "false", "callback": "portal"})
        try:
            response = await self.session.get(self.url("/webapps/bb-mygrades-BBLEARN/myGrades?" + query))
            grades = parse_grades(response.text)
        except BBError:
            grades = {}
        counts = Counter(" ".join(item.name.split()).casefold() for item in assignments)

        async def resolve(item):
            if item.kind == "test":
                return item
            normalized = " ".join(item.name.split())
            record = grades.get(item.id)
            if record is None and counts[normalized.casefold()] == 1:
                record = grades.get(normalized) or grades.get(normalized.casefold())
            if record and record.get("status") != "unknown":
                item.status = record["status"]
                item.grade = record.get("grade")
                return item
            try:
                detail = await self.detail(item)
                item.status, item.grade = detail.status, detail.grade
                item.due_at = detail.due_at or item.due_at
                item.due_text = detail.due_text or item.due_text
                if item.status == "unknown":
                    warnings.append(f"{item.name}：提交状态无法确认。")
            except BBError as error:
                warnings.append(f"{item.name}：状态查询失败（{error.code}）。")
            return item
        return await asyncio.gather(*(resolve(item) for item in assignments))

    async def assignments(self, course):
        _, assignments, _, warnings = await self.scan(course)
        assignments = await self._statuses(course, assignments, warnings)
        return assignments, warnings

    async def list_assignments(self, course_query):
        courses = await self.courses()
        selected = courses if course_query.strip().casefold() == "all" else [choose(courses, course_query, "课程")]

        async def one(course):
            try:
                assignments, notices = await self.assignments(course)
                return [{**a.model_dump(), "course_name": course.name} for a in assignments], notices
            except BBError as error:
                return [], [f"{course.name}：查询失败（{error.code}）。"]

        # Courses are read concurrently; the session semaphore bounds real
        # HTTP concurrency, and results keep the portal's course order.
        items, warnings = [], []
        for course_items, notices in await asyncio.gather(*(one(course) for course in selected)):
            items.extend(course_items)
            warnings.extend(notices)
        return Result(message=f"读取到 {len(items)} 项作业或测试入口。", data={"assignments": items}, complete=not warnings, warnings=warnings)

    async def detail(self, assignment):
        # Do not launch assessments or consume their attempts just to inspect them.
        if assignment.kind == "test":
            return assignment
        response = await self.session.get(assignment.url)
        detail = parse_assignment_detail(response.text, str(response.url), assignment.course_id, assignment.id, assignment.name)
        detail.name = assignment.name
        detail.requirements = detail.requirements or assignment.requirements
        detail.attachments = list({f.id: f for f in [*assignment.attachments, *detail.attachments]}.values())
        return detail

    async def assignment_details(self, course_query, keyword="", download=False):
        course = await self.select_course(course_query)
        _, items, _, warnings = await self.scan(course)
        if keyword:
            # Prefer exact ID/title; an ambiguous keyword must be resolved explicitly.
            items = [choose(items, keyword, "作业")]
        details, saved = [], []
        for item in items:
            try:
                detail = await self.detail(item)
                details.append(detail.model_dump())
                if detail.status == "unknown" and detail.kind != "test":
                    warnings.append(f"{item.name}：提交状态无法确认。")
                if download:
                    for file in detail.attachments:
                        try:
                            saved.append(await self.downloader.download(file, course.name, assignment=True))
                        except (BBError, OSError) as error:
                            warnings.append(f"{file.name}：附件下载失败（{getattr(error, 'code', 'local_io_error')}）。")
            except BBError as error:
                warnings.append(f"{item.name}：详情读取失败（{error.code}）。")
        return Result(message=f"读取到 {len(details)} 项详情。", data={"course": course.model_dump(), "assignments": details, "downloads": saved}, complete=not warnings, warnings=warnings)

    async def announcements(self, course_query, limit=8):
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise BBError("invalid_argument", "limit 应为 1 到 1000 的整数。")
        courses = await self.courses()
        selected = courses if course_query.strip().casefold() == "all" else [choose(courses, course_query, "课程")]

        async def one(course):
            try:
                target = self._cached("announcement_url", course.id)
                if target is None:
                    response = await self.session.get(course.url)
                    _, target = parse_course_menu(response.text, str(response.url))
                    if not target:
                        # Blackboard's standard course-scoped announcement reader.
                        target = self.url("/webapps/blackboard/execute/announcement?" + urlencode({"method": "search", "context": "course", "course_id": course.id, "viewChoice": 3}))
                    self._remember(target, "announcement_url", course.id)
                page = await self.session.get(target)
                items = parse_announcements(page.text, str(page.url), course.id)
                notices = []
                if not items and not any(marker in page.text for marker in ("announcementList", "announcement_list", "没有公告", "无公告", "No announcements")):
                    notices.append(f"{course.name}：未识别到公告列表结构，不能确认没有公告。")
                return [{**a.model_dump(), "course_name": course.name} for a in items[:limit]], notices
            except BBError as error:
                return [], [f"{course.name}：公告读取失败（{error.code}）。"]

        announcements, warnings = [], []
        for course_items, notices in await asyncio.gather(*(one(course) for course in selected)):
            announcements.extend(course_items)
            warnings.extend(notices)
        return Result(message=f"读取到 {len(announcements)} 条公告。", data={"announcements": announcements}, complete=not warnings, warnings=warnings)
