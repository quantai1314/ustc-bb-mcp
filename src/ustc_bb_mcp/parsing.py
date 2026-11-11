"""Pure HTML parsers for Blackboard Learn Original/Classic.

The parsers deliberately leave absent or ambiguous values unknown. They never
execute a page's JavaScript, follow a URL, or start an assessment attempt.
"""

import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .errors import BBError
from .models import Announcement, Assignment, ContentPage, Course, CourseFile

_TZ = timezone(timedelta(hours=8))
_FILE_EXT = re.compile(
    r"\.(?:pdf|pptx?|docx?|xlsx?|zip|rar|7z|txt|md|csv|png|jpe?g|gif|mp4|mp3|"
    r"ipynb|py|java|[ch]|cpp|v|sv|m|tex|tar|gz)(?:$|[?#])", re.I
)
_SECRET = re.compile(
    r"ticket|token|session|jsessionid|password|passwd|credential|authorization|"
    r"signature|^sig$|^key$|^auth$|^saml|^relaystate$|^execution$", re.I
)
_MONTHS = {
    name: i for i, names in enumerate(
        ("jan january", "feb february", "mar march", "apr april", "may",
         "jun june", "jul july", "aug august", "sep sept september", "oct october",
         "nov november", "dec december"), 1
    ) for name in names.split()
}


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _text(node: Tag | BeautifulSoup | None, separator: str = " ") -> str:
    return re.sub(r"[ \t\xa0]+", " ", node.get_text(separator, strip=True)).strip() if node else ""


def normalize_name(value: str) -> str:
    return " ".join(value.split()).strip()


def safe_public_url(url: str) -> str:
    """Drop authentication material before returning a Blackboard source link."""
    p = urlsplit(url)
    if p.scheme not in {"http", "https", ""}:
        return ""
    host = p.netloc.rsplit("@", 1)[-1]
    path = re.sub(r";jsessionid=[^/?;]*", "", p.path, flags=re.I)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not _SECRET.search(k)])
    return urlunsplit((p.scheme, host, path, query, ""))


def _url(base: str, href: str | None) -> str:
    return safe_public_url(urljoin(base, href or ""))


def _params(url: str) -> dict[str, str]:
    return {k.lower(): v[0] for k, v in parse_qs(urlsplit(url).query).items() if v}


def content_key(url: str) -> str:
    """Stable visited key: wrapper paths and query ordering do not add pages."""
    p = _params(url)
    cid = p.get("course_id", p.get("courseid", ""))
    content = p.get("content_id", p.get("contentid", ""))
    return f"{cid}:{content}" if content else safe_public_url(url)


def _stable_id(url: str) -> str:
    return hashlib.sha256(safe_public_url(url).encode()).hexdigest()[:24]


def _date(value: str) -> str | None:
    """Preserve date-only precision; add +08:00 only when a time was supplied."""
    value = value.replace("\xa0", " ")
    m = re.search(r"(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})日?", value)
    parts = tuple(map(int, m.groups())) if m else None
    if not parts:
        m = re.search(r"\b([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", value)
        if m and m[1].lower() in _MONTHS:
            parts = (int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
        else:
            m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\.?[,]?\s+(20\d{2})\b", value)
            if m and m[2].lower() in _MONTHS:
                parts = (int(m[3]), _MONTHS[m[2].lower()], int(m[1]))
    if not parts or not m:
        return None
    suffix = value[m.end():]
    tm = re.search(r"(上午|下午|AM|PM)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|上午|下午)?", suffix, re.I)
    try:
        if not tm:
            return datetime(*parts).date().isoformat()
        hour = int(tm[2])
        period = (tm[1] or tm[5] or "").lower()
        if period in {"pm", "下午"} and hour < 12:
            hour += 12
        if period in {"am", "上午"} and hour == 12:
            hour = 0
        return datetime(*parts, hour, int(tm[3]), int(tm[4] or 0), tzinfo=_TZ).isoformat()
    except ValueError:
        return None


def _due(node: Tag | BeautifulSoup) -> tuple[str | None, str | None]:
    # A labelled row is preferable to an unrelated publication date in the item.
    for label in node.find_all(string=re.compile(r"截止|到期|due\s*date|due\s*:", re.I)):
        parent = label.parent
        if not parent or parent.name in {"script", "style"}:
            continue
        label_text = normalize_name(str(label))
        if not re.match(r"^(?:(?:作业)?(?:截止|到期)(?:日期|时间)?|due(?:\s+date|\s+time)?)"
                        r"(?:\s*[:：]|\s*$|\s+(?=20\d{2}[年/.-]))", label_text, re.I):
            continue
        if label.find_parent(lambda element: element.get("id") in {
            "assignmentInstructions", "instructions", "description"
        } or "vtbegenerated" in element.get("class", [])):
            continue
        candidates = [_text(parent)]
        if parent.name in {"dt", "th", "label", "span", "strong", "b"} or "metaLabel" in parent.get("class", []):
            # Only inspect the adjacent value, not an entire surrounding section
            # that may contain a publication or example date elsewhere.
            for sibling in parent.next_siblings:
                raw = _text(sibling) if isinstance(sibling, Tag) else normalize_name(str(sibling))
                if raw:
                    candidates.append(raw)
                    break
        for raw in candidates:
            if len(raw) > 250:
                continue
            parsed = _date(raw)
            if parsed:
                return parsed, raw
    return None, None


def is_login_page(html: str, url: str = "") -> bool:
    path = urlsplit(url).path.lower()
    soup = _soup(html)
    return bool(
        path.startswith(("/nginx_auth", "/cas/login", "/webapps/login"))
        or soup.select_one('input[type="password"], form#login, #login-page-flowkey')
        or (soup.select_one('a[href*="/cas/login"]') and not soup.select_one('#courseList, #myCourseList'))
    )


def is_authenticated_portal(html: str, url: str = "") -> bool:
    if is_login_page(html, url):
        return False
    soup = _soup(html)
    return bool(soup.select_one(
        'a[href*="action=logout"], a[href*="/logout"], #global-nav-link, '
        '#global-nav, #topTabs, #courseList, #myCourseList, [id="module:_4_1"]'
    ) or parse_courses(html, url))


def parse_courses(html: str, base_url: str) -> list[Course]:
    courses: dict[str, Course] = {}
    for a in _soup(html).select("a[href]"):
        url = _url(base_url, a.get("href"))
        p = _params(url)
        cid = p.get("course_id") or (p.get("id") if p.get("type", "").lower() == "course" else None)
        name = _text(a)
        if not cid or not re.fullmatch(r"_\d+_\d+", cid) or not name:
            continue
        term = re.search(r"\b(20\d{2}(?:FA|SP|SU|WI))\b", name, re.I)
        courses.setdefault(cid, Course(id=cid, name=name, url=url,
                                       term=term[1].upper() if term else None))
    return list(courses.values())


def _folder_url(url: str) -> bool:
    return bool(re.search(r"listContent(?:Editable)?\.jsp|contentWrapper", url, re.I)
                and _params(url).get("course_id"))


def parse_course_menu(html: str, page_url: str) -> tuple[list[str], str | None]:
    folders: dict[str, str] = {}
    announcements = []
    for a in _soup(html).select("a[href], iframe[src], frame[src]"):
        url = _url(page_url, a.get("href") or a.get("src"))
        if _folder_url(url):
            folders.setdefault(content_key(url), url)
        # Course home modules expose edit/settings links containing
        # "bbannouncements" inside forwardUrl. Only follow actual reader pages.
        path = urlsplit(url).path.rstrip("/").lower()
        params = _params(url)
        if (path.endswith(("/announcement", "/announcements", "/announcement.jsp"))
            and params.get("method", "search").lower() not in {"edit", "save", "delete", "create"}):
            announcements.append((params.get("viewchoice") == "3", url))
    announcement = max(announcements, key=lambda item: item[0])[1] if announcements else None
    return list(folders.values()), announcement


def _is_file(url: str) -> bool:
    return bool(_FILE_EXT.search(url) or "bbcswebdav" in url.lower() or "@xid" in url.lower()
                or re.search(r"/webapps/assignment/download(?:$|[/?])", url, re.I))


def _file_name(a: Tag, url: str) -> str:
    params = _params(url)
    if params.get("filename"):
        return params["filename"]
    text = _text(a)
    if text and not re.fullmatch(r"下载|download|点击下载", text, re.I):
        return text
    return urlsplit(url).path.rsplit("/", 1)[-1] or text or "attachment"


def _kind(url: str) -> str | None:
    path = urlsplit(url).path.lower()
    if re.search(r"/webapps/assignment/download(?:$|/)", path):
        return None
    if "/webapps/assessment/" in path or "assessment" in _params(url).get("type", "").lower():
        return "test"
    if "/webapps/assignment/" in path or "uploadassignment" in path:
        return "assignment"
    return None


def _entry(a: Tag) -> Tag:
    return a.find_parent("li") or a.find_parent(class_=re.compile(r"contentItem|item clearfix")) or a.parent or a


def parse_content(html: str, page_url: str, course_id: str) -> ContentPage:
    result = ContentPage()
    files, assignments, folders = {}, {}, {}
    soup = _soup(html)
    root = soup.select_one('#content_listContainer, #contentList, .contentList') or soup
    for a in root.select("a[href], iframe[src], frame[src]"):
        raw_url = a.get("href") or a.get("src") or ""
        if not raw_url or raw_url.startswith("#"):
            continue
        url = _url(page_url, raw_url)
        if not url:
            continue
        p = _params(url)
        if p.get("course_id") and p["course_id"] != course_id:
            continue
        name = _text(a) or urlsplit(url).path.rsplit("/", 1)[-1]
        entry = _entry(a)
        kind = _kind(url)
        if kind:
            aid = p.get("content_id") or p.get("assessment_id") or p.get("id") or _stable_id(url)
            due, due_text = _due(entry)
            assignments.setdefault(aid, Assignment(id=aid, course_id=course_id, name=name,
                                                  url=url, kind=kind, due_at=due, due_text=due_text))
        elif _folder_url(url):
            folders.setdefault(content_key(url), url)
        elif _is_file(url):
            fid = _stable_id(url)
            files.setdefault(fid, CourseFile(id=fid, course_id=course_id, name=_file_name(a, url), url=url,
                                             source_url=safe_public_url(page_url), date=_date(_text(entry))))
    result.files = list(files.values())
    result.assignments = list(assignments.values())
    result.folders = list(folders.values())
    return result


def _row_id(node: Tag) -> str | None:
    for item in node.select("a[href], a[onclick]"):
        candidates = [item.get("href", ""), item.get("onclick", "")]
        for candidate in candidates:
            match = re.search(r"(?:content_id|contentId)=([^&'\"\s)]+)", candidate)
            if match:
                return match[1]
    return node.get("data-content-id")


def _grade_state(row: Tag) -> dict:
    activity = _text(row.select_one(".activity .activityType"))
    grade = _text(row.select_one(".grade > .grade"))
    points = _text(row.select_one(".pointsPossible"))
    classes = " ".join(row.get("class", []))
    draft = (re.search(r"draft|in_progress", classes, re.I)
             or re.search(r"草稿|进行中|in progress|draft", activity, re.I)
             or row.select_one('[src*="in_progress"]'))
    submitted = (row.select_one('a[onclick*="showHistory"], a[href*="showHistory"], [src*="needs_grading"]')
                 or "submitted_item_row" in classes or re.fullmatch(r"已提交|Submitted", activity, re.I))
    graded = "graded_item_row" in classes or re.fullmatch(r"已评分|Graded", activity, re.I)
    if draft:
        return {"status": "draft", "grade": None}
    if (submitted or graded) and re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", grade):
        return {"status": "graded", "grade": normalize_name(f"{grade} {points}")}
    if submitted:
        return {"status": "submitted", "grade": None}
    if (re.fullmatch(r"未提交|Not submitted", activity, re.I)
        or ("upcoming_item_row" in classes and re.fullmatch(r"尚未评分|Not graded", activity, re.I)
            and grade == "-" and not row.select_one(".gradeStatus img, .gradeStatus .tooltip"))):
        return {"status": "not_submitted", "grade": None}
    return {"status": "unknown", "grade": None}


def parse_grades(html: str) -> dict[str, dict]:
    entries = []
    for row in _soup(html).select("#grades_wrapper > .row, #grades_wrapper > li"):
        if "calculatedRow" in row.get("class", []):
            continue
        category = _text(row.select_one(".itemCat"))
        if category and not re.fullmatch(r"作业|Assignment", category, re.I):
            continue
        cell = row.select_one(".gradable")
        if cell is None:
            continue
        title = _soup(str(cell))
        for element in title.select(".activityType, .itemCat, .eval-links"):
            element.decompose()
        entries.append((_row_id(row), normalize_name(_text(title)), _grade_state(row)))
    counts = Counter(name for _, name, _ in entries)
    ids = Counter(cid for cid, _, _ in entries if cid)
    result = {}
    for cid, name, state in entries:
        if cid and ids[cid] == 1:
            result[cid] = state
        if name and counts[name] == 1:
            result[name] = state
    return result


def _field(node: BeautifulSoup | Tag, names: tuple[str, ...]) -> str | None:
    for name in names:
        el = node.find(attrs={"name": name}) or node.find(id=name)
        if el:
            value = el.get("value") if el.name == "input" else _text(el)
            if value:
                return str(value)
    return None


def _selector(element: Tag | None, tag: str) -> str | None:
    if element is None:
        return None
    for attr in ("id", "name"):
        if element.get(attr):
            value = str(element[attr]).replace("\\", "\\\\").replace('"', '\\"')
            return f'{tag}[{attr}="{value}"]'
    return None


def submission_form(html: str, url: str) -> dict:
    """Describe a standard assignment form; all values remain internal to the service."""
    soup = _soup(html)
    form = next((f for f in soup.select("form") if f.select_one('input[type="file"]')), None)
    context = form or soup
    action = _url(url, form.get("action") if form else url)
    params = {**_params(url), **_params(action)}
    course_id = _field(context, ("course_id", "courseId")) or params.get("course_id")
    assignment_id = _field(context, ("content_id", "contentId", "assignment_id", "assignmentId")) or params.get("content_id") or params.get("assignment_id")
    attempt_id = _field(soup, ("attempt_id", "attemptId", "attemptIdString")) or params.get("attempt_id")
    files = context.select('input[type="file"]') if form else []
    submits = context.select('input[type="submit"], button[type="submit"], button:not([type])') if form else []
    submit = next((el for el in submits if not el.has_attr("disabled") and re.search(
        r"submit|提交", " ".join((str(el.get("value", "")), str(el.get("name", "")), _text(el))), re.I
    ) and not re.search(r"save|draft|cancel|保存|草稿|取消", " ".join((str(el.get("value", "")), _text(el))), re.I)), None)
    comment = None
    if form:
        for selector in ('textarea[name*="comment" i]', 'textarea[id*="comment" i]',
                         'textarea[name*="feedback" i]', 'textarea[id*="feedback" i]'):
            comment = context.select_one(selector)
            if comment is not None and not comment.has_attr("disabled") and not comment.has_attr("readonly"):
                break
            comment = None
    is_test = _kind(url) == "test" or _kind(action) == "test"
    is_group = bool(_field(context, ("group_id", "groupId")) or "groupassignment" in action.lower())
    history = []
    for a in soup.select("a[href]"):
        href = _url(url, a.get("href"))
        if (href != safe_public_url(url)
            and re.search(r"showHistory|reviewAssignment|submissionHistory|viewAttempts", href, re.I)):
            history.append(href)
    existing = []
    for section in soup.select('[id], [class]'):
        signature = str(section.get("id", "")) + " " + " ".join(section.get("class", []))
        if re.search(r"submissionFiles|submittedFiles|uploadedFiles|submissionAttachments|attemptFiles|receipt", signature, re.I):
            existing.extend(_file_name(a, _url(url, a.get("href"))) for a in section.select("a[href]") if _is_file(_url(url, a.get("href"))))
    # Classic history pages can use labelled definition lists instead of IDs.
    for label in soup.find_all(string=re.compile(r"submitted\s*files|已提交的文件|提交的附件", re.I)):
        if label.parent and label.parent.parent:
            existing.extend(_file_name(a, _url(url, a.get("href"))) for a in label.parent.parent.select("a[href]") if _is_file(_url(url, a.get("href"))))
    # USTC Classic history pages expose attempt identity in attachment download
    # URLs rather than in hidden form fields or a standalone receipt number.
    submitted_files = {}
    for a in soup.select('a[href*="/webapps/assignment/download"]'):
        href = _url(url, a.get("href"))
        parsed_href = urlsplit(href)
        page_origin = urlsplit(url)
        if ((parsed_href.scheme, parsed_href.netloc) != (page_origin.scheme, page_origin.netloc)
            or parsed_href.path.rstrip("/") != "/webapps/assignment/download"):
            continue
        download_params = _params(href)
        download_attempt = download_params.get("attempt_id")
        file_id = download_params.get("file_id")
        name = download_params.get("filename") or _text(a)
        if (not download_attempt or not file_id or not name
            or re.fullmatch(r"下载|download|点击下载", name, re.I)
            or (course_id and download_params.get("course_id") not in {None, course_id})):
            continue
        attempt_id = attempt_id or download_attempt
        existing.append(name)
        submitted_files.setdefault((download_attempt, file_id), {
            "name": name, "url": href, "attempt_id": download_attempt, "file_id": file_id,
        })
    receipt = _field(soup, ("confirmationNumber", "receiptId", "submissionReceiptId"))
    text = _text(soup)
    if not receipt:
        match = re.search(r"(?:confirmation\s*(?:number|id)|确认号|回执编号)\s*[:：#]?\s*([A-Za-z0-9_-]{4,})", text, re.I)
        receipt = match[1] if match else None
    title = _text(soup.select_one("#pageTitleText"))
    has_receipt = bool(receipt or re.search(r"successfully submitted|submission (?:was )?received|提交成功|已成功提交", text, re.I)
                       or (attempt_id and existing and re.search(r"复查提交历史记录|review submission history", title, re.I)))
    new_attempts = []
    for control in soup.select('input[type="submit"], input[type="button"], button, a[href]'):
        label = normalize_name(str(control.get("value") or _text(control)))
        if (control.has_attr("disabled") or control.get("aria-disabled") == "true"
            or not re.fullmatch(r"开始新的|开始新尝试|Start New(?: Attempt)?", label, re.I)
            or control.find_parent(class_="vtbegenerated")):
            continue
        target = str(control.get("href") or "")
        if not target:
            location = re.fullmatch(
                r"\s*(?:document|window)\.location(?:\.href)?\s*=\s*(['\"])([^'\"]+)\1\s*;?\s*",
                str(control.get("onclick") or ""),
            )
            target = location[2] if location else ""
        target = _url(url, target) if target else ""
        destination, origin = urlsplit(target), urlsplit(url)
        query = _params(target)
        selector = _selector(control, control.name)
        if (selector and (destination.scheme, destination.netloc) == (origin.scheme, origin.netloc)
            and destination.path == "/webapps/assignment/uploadAssignment"
            and query.get("action", "").casefold() == "newattempt"
            and query.get("course_id") == course_id and query.get("content_id") == assignment_id
            and not query.get("group_id")):
            new_attempts.append((selector, target))
    new_attempt = new_attempts[0] if len(new_attempts) == 1 else (None, None)
    return {
        "action": action, "form_id": form.get("id") if form else None,
        "file_input": _selector(files[0], "input") if files else None,
        "file_inputs": [_selector(el, "input") for el in files],
        "comment_input": _selector(comment, "textarea"),
        "submit_name": submit.get("name") if submit else None,
        "submit_value": submit.get("value", _text(submit)) if submit else None,
        "submit_selector": _selector(submit, submit.name) if submit else None,
        "hidden_fields": {el["name"]: el.get("value", "") for el in context.select('input[type="hidden"][name]')} if form else {},
        "can_submit": bool(form and files and submit is not None and course_id and assignment_id and not is_test and not is_group),
        "can_start_new_attempt": bool(has_receipt and attempt_id and new_attempt[0] and not is_test and not is_group),
        "new_attempt_selector": new_attempt[0], "new_attempt_url": new_attempt[1],
        "course_id": course_id, "assignment_id": assignment_id,
        "history_links": list(dict.fromkeys(history)), "existing_files": list(dict.fromkeys(existing)),
        "submitted_files": list(submitted_files.values()),
        "receipt_id": receipt, "has_receipt": has_receipt, "attempt_id": attempt_id,
        "is_test": is_test, "is_group": is_group,
    }


def parse_assignment_detail(html: str, url: str, course_id: str,
                            assignment_id: str, name: str = "") -> Assignment:
    soup = _soup(html)
    expected = {"course_id": course_id, "courseid": course_id,
                "content_id": assignment_id, "contentid": assignment_id,
                "assignment_id": assignment_id, "assignmentid": assignment_id}
    # Never attach another target's grade to the caller's requested assignment.
    # Missing identity fields are not invented: legacy history pages can expose
    # their IDs only in the actual URL, and some snapshots have neither.
    for field in soup.select('input[name], input[id]'):
        key = str(field.get("name") or field.get("id", "")).lower()
        value = str(field.get("value", ""))
        if key in expected and value and value != expected[key]:
            raise BBError("target_mismatch", "作业页面中的课程或作业 ID 与请求目标不一致，未读取成绩。")
    for key, value in _params(url).items():
        if key in expected and value != expected[key]:
            raise BBError("target_mismatch", "作业页面地址中的课程或作业 ID 与请求目标不一致，未读取成绩。")
    title = _text(soup.select_one("#assignmentTitle, #assignmentName, h1#pageTitleText, #pageTitleText"))
    requirements = soup.select_one('#assignmentInstructions, #instructions, #description, .vtbegenerated')
    due, due_text = _due(soup)
    info = submission_form(html, url)
    status, grade = "unknown", None
    if not is_login_page(html, url):
        grade = _field(soup, ("aggregateGrade",))
        if grade is not None and re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", grade):
            status = "graded"
            suffix = _text(soup.select_one("#aggregateGrade_pointsPossible"))
            grade = normalize_name(f"{grade} {suffix}")
        else:
            grade = None
            state_text = _text(soup.select_one('#attemptStatus, .attemptStatus, .submissionStatus, #submissionStatus, #attemptInfo, .attemptInfo'))
            if re.search(r"草稿|saved (?:as )?draft|attempt in progress|尝试正在进行", state_text, re.I):
                status = "draft"
            elif info["has_receipt"] or re.search(r"复查提交历史记录|review submission history", title, re.I):
                status = "submitted"
            elif info["can_submit"] and re.search(r"上载作业|上传作业|upload assignment", title, re.I):
                status = "not_submitted"
    content = parse_content(str(requirements or soup), url, course_id)
    return Assignment(id=assignment_id, course_id=course_id, name=name or title or assignment_id,
                      url=safe_public_url(url), kind=_kind(url) or "assignment", due_at=due,
                      due_text=due_text, status=status, grade=grade,
                      requirements=_text(requirements, "\n"), attachments=content.files)


def parse_announcements(html: str, url: str, course_id: str) -> list[Announcement]:
    soup = _soup(html)
    blocks = soup.select('#announcementList > li, #announcementList > .announcement, .announcementList > li, li.announcement')
    if not blocks:
        blocks = soup.select(".announcement")
    result = []
    for index, block in enumerate(blocks):
        # The USTC course reader prepends pinned platform announcements. Do not
        # attribute those "Posted to: Everyone" messages to every scanned course.
        info = block.select_one(".announcementInfo")
        if info:
            system_message = False
            for label in info.select("span"):
                if re.fullmatch(r"发布至\s*[:：]?|Posted\s+to\s*[:：]?", _text(label), re.I):
                    audience = normalize_name("".join(str(sibling) for sibling in label.next_siblings))
                    if re.fullmatch(r"每个人|所有人|所有用户|Everyone|All users", audience, re.I):
                        system_message = True
            if system_message:
                continue
        heading = block.select_one("h3, h2, .announcementTitle, .title")
        title = _text(heading)
        if not title:
            continue
        body = block.select_one(".vtbegenerated, .announcementBody, .announcementText, .details")
        identifier = str(block.get("id") or block.get("data-announcement-id")
                         or hashlib.sha256(f"{safe_public_url(url)}:{index}:{title}".encode()).hexdigest()[:24])
        a = heading.select_one("a[href]") if heading else None
        source = _url(url, a.get("href")) if a else safe_public_url(url)
        text = _text(body, "\n")
        images = []
        if body:
            for image in body.select("img[src]"):
                image_url = _url(url, image.get("src"))
                if image_url:
                    images.append(f"[图片：{image.get('alt') or '公告图片'}]({image_url})")
        text = "\n".join([part for part in [text, *images] if part])
        result.append(Announcement(id=identifier, course_id=course_id, title=title,
                                   body=text, url=source, date=_date(_text(block))))
    return result
