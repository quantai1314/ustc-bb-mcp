"""Preview-bound single-file submission; uncertain writes are never retried."""

import asyncio
import hashlib
import json
import mimetypes
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .errors import BBError
from .models import Result
from .parsing import is_login_page, parse_assignment_detail, safe_public_url, submission_form
from .service import choose


def _snapshot(form: dict, detail) -> str:
    fields = ("course_id", "assignment_id", "attempt_id", "can_submit", "is_group",
              "file_input", "comment_input", "submit_selector", "receipt_id",
              "can_start_new_attempt", "new_attempt_selector", "new_attempt_url")
    value = {key: form.get(key) for key in fields}
    value.update(action=safe_public_url(form["action"]),
                 existing_files=sorted(form.get("existing_files", [])),
                 due_at=detail.due_at, status=detail.status,
                 requirements=detail.requirements)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Submitter:
    def __init__(self, service, *, clock=time.monotonic,
                 receipt_poll_delays=(0, 0.5, 1, 2, 3)):
        self.service = service
        self.session = service.session
        self.clock = clock
        self.pending: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self.receipt_poll_delays = receipt_poll_delays

    async def _target(self, course_query, assignment_query):
        course = await self.service.select_course(course_query)
        _, assignments, _, warnings = await self.service.scan(course)
        if warnings:
            raise BBError("incomplete_scan", "课程扫描不完整，暂不执行提交。请先解决查询失败。", details={"warnings": warnings})
        assignment = choose(assignments, assignment_query, "作业")
        if assignment.kind != "assignment":
            raise BBError("unsupported_assignment", "提交工具只支持标准文件作业，不启动测试或考试。")
        response = await self.session.get(assignment.url)
        form = submission_form(response.text, str(response.url))
        detail = parse_assignment_detail(response.text, str(response.url), course.id, assignment.id, assignment.name)
        self._validate(form, course.id, assignment.id, allow_new_attempt=True)
        return course, assignment, form, detail

    @staticmethod
    def _validate(form, course_id, assignment_id, *, allow_new_attempt=False):
        if form.get("is_group") or form.get("is_test"):
            raise BBError("unsupported_assignment", "首版只支持个人标准文件作业。")
        if form.get("course_id") != course_id or form.get("assignment_id") != assignment_id:
            raise BBError("target_mismatch", "提交页面与选定的课程或作业不一致，已停止。")
        if allow_new_attempt and form.get("can_start_new_attempt"):
            return
        if not form.get("can_submit") or not form.get("submit_selector"):
            raise BBError("submission_unavailable", "未发现可用的文件上传及提交表单；可能已截止、已交且不允许再次提交，或页面结构变化。")

    async def submit(self, course: str, assignment: str, file_path: str,
                     comment: str = "", confirm: bool = False, preview_token: str = "") -> Result:
        async with self.lock:
            if confirm:
                pending = self.pending.get(preview_token)
                if not pending or self.clock() >= pending["expires"]:
                    self.pending.pop(preview_token, None)
                    raise BBError("preview_required", "预览令牌无效或已超过 10 分钟，请重新预览。")
            path = Path(file_path).expanduser().resolve()
            if not path.is_file():
                raise BBError("file_not_found", "待提交文件不存在或不是普通文件。")
            limit = self.service.settings.max_upload_bytes
            try:
                size = path.stat().st_size
                if size > limit:
                    # The whole file is held in memory for hashing and upload.
                    raise BBError("file_too_large", f"待提交文件 {size} 字节，超过 {limit} 字节上限。",
                                  details={"bytes": size, "limit": limit})
                if size == 0:
                    raise BBError("file_empty", "待提交文件为空，未生成预览。")
                payload = await asyncio.to_thread(path.read_bytes)
            except OSError as exc:
                raise BBError("local_io_error", "无法读取待提交文件。") from exc
            checksum = hashlib.sha256(payload).hexdigest()
            selected_course, selected_assignment, form, detail = await self._target(course, assignment)
            binding = {"identity": self.session.identity, "course_id": selected_course.id,
                       "assignment_id": selected_assignment.id, "path": str(path),
                       "sha256": checksum, "comment": comment, "snapshot": _snapshot(form, detail)}
            preview = {
                "course": selected_course.model_dump(), "assignment": detail.model_dump(),
                "account_session": self.session.identity[:12], "file_path": str(path),
                "filename": path.name, "bytes": len(payload), "sha256": checksum,
                "comment": comment, "existing_files": form.get("existing_files", []),
                "existing_attempt_id": form.get("attempt_id"), "can_submit": True,
                "starts_new_attempt": bool(form.get("can_start_new_attempt") and not form.get("can_submit")),
                "submission_url": safe_public_url(form["action"]),
            }
            if not confirm:
                self.pending = {token: p for token, p in self.pending.items() if p["expires"] > self.clock()}
                token = secrets.token_urlsafe(32)
                self.pending[token] = {"binding": binding, "expires": self.clock() + 600}
                return Result(message="预览已生成，尚未上传或提交。核对目标和文件后，以相同参数、confirm=true 和预览令牌调用。",
                              data={**preview, "submitted": False, "preview_token": token, "expires_in_seconds": 600})
            if pending["binding"] != binding:
                self.pending.pop(preview_token, None)
                raise BBError("preview_changed", "账号会话、目标、文件、评语或提交条件已变化，请重新预览。")
            return await self._write(selected_course, selected_assignment, form, detail,
                                     path.name, payload, comment, preview_token, binding["identity"])

    async def _write(self, course, assignment, original_form, original_detail,
                     filename, payload, comment, token, expected_identity):
        from playwright.async_api import async_playwright

        wrote = False
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                state = await self.session.storage_state()
                if self.session.identity != expected_identity:
                    await browser.close()
                    raise BBError("preview_changed", "启动提交浏览器期间账号会话发生变化，请重新预览。")
                context = await browser.new_context(storage_state=state, locale="zh-CN", service_workers="block")
                try:
                    base = urlsplit(self.service.settings.base_url)

                    async def guard(route):
                        part = urlsplit(route.request.url)
                        if (part.scheme, part.netloc) != (base.scheme, base.netloc):
                            await route.abort()
                        elif route.request.method not in {"GET", "HEAD"} and not wrote:
                            await route.abort()
                        else:
                            await route.continue_()

                    await context.route("**/*", guard)
                    page = await context.new_page()
                    page.set_default_timeout(self.service.settings.timeout * 1000)
                    await page.goto(assignment.url, wait_until="domcontentloaded")
                    html = await page.content()
                    if is_login_page(html, page.url):
                        raise BBError("session_expired", "浏览器会话失效，请重新登录并预览。")
                    form = submission_form(html, page.url)
                    detail = parse_assignment_detail(html, page.url, course.id, assignment.id, assignment.name)
                    self._validate(form, course.id, assignment.id, allow_new_attempt=True)
                    if _snapshot(form, detail) != _snapshot(original_form, original_detail):
                        raise BBError("preview_changed", "浏览器中的提交条件已改变，请重新预览。")
                    action = urlsplit(form["action"])
                    if (action.scheme, action.netloc) != (base.scheme, base.netloc):
                        raise BBError("unsafe_target", "提交表单指向了其他站点，已停止。")
                    if not urlsplit(assignment.url).path.startswith("/webapps/assignment/") or not action.path.startswith("/webapps/assignment/"):
                        raise BBError("unsupported_assignment", "未识别到标准 Blackboard 作业表单。")
                    if form.get("can_start_new_attempt") and not form.get("can_submit"):
                        if self.session.identity != expected_identity:
                            raise BBError("preview_changed", "创建新尝试前账号会话发生变化，请重新预览。")
                        previous_attempt = form.get("attempt_id")
                        self.pending.pop(token, None)
                        wrote = True  # Blackboard creates attempts through a GET action.
                        await page.goto(form["new_attempt_url"], wait_until="domcontentloaded")
                        html = await page.content()
                        if is_login_page(html, page.url):
                            raise BBError("session_expired", "创建新尝试时会话失效，未上传文件。")
                        form = submission_form(html, page.url)
                        detail = parse_assignment_detail(html, page.url, course.id, assignment.id, assignment.name)
                        self._validate(form, course.id, assignment.id)
                        if (form.get("has_receipt") or form.get("attempt_id") == previous_attempt
                            or detail.requirements != original_detail.requirements
                            or (original_detail.due_at and detail.due_at != original_detail.due_at)):
                            raise BBError("preview_changed", "新尝试的标识或作业条件不符合预览，未上传文件。")
                        action = urlsplit(form["action"])
                        if ((action.scheme, action.netloc) != (base.scheme, base.netloc)
                            or not action.path.startswith("/webapps/assignment/")):
                            raise BBError("unsafe_target", "新尝试的提交表单不在当前作业站点，未上传文件。")
                        original_form = {**form, "previous_attempt_id": previous_attempt}
                    if comment:
                        comment_selector = form.get("comment_input")
                        if not comment_selector or await page.locator(comment_selector).count() != 1:
                            raise BBError("comment_unavailable", "未识别到唯一评语输入框，未上传文件。")
                        textarea = page.locator(comment_selector)
                        if not await textarea.is_visible():
                            editor_id = await textarea.get_attribute("id")
                            if not editor_id or await page.locator(f'iframe[id="{editor_id}_ifr"]').count() != 1:
                                raise BBError("comment_unavailable", "无法定位评语编辑器，未上传文件。")
                    if self.session.identity != expected_identity:
                        raise BBError("preview_changed", "上传前账号会话发生变化，请重新预览。")
                    self.pending.pop(token, None)  # A token authorizes at most one write attempt.
                    wrote = True  # Setting a file can itself start an AJAX upload.
                    await page.locator(form["file_input"]).set_input_files({
                        "name": filename, "mimeType": mimetypes.guess_type(filename)[0] or "application/octet-stream", "buffer": payload,
                    })
                    if comment:
                        comment_selector = form.get("comment_input")
                        if not comment_selector:
                            raise BBError("comment_unavailable", "未识别到评语输入框，未点击提交。")
                        textarea = page.locator(comment_selector)
                        if await textarea.is_visible():
                            await textarea.fill(comment)
                        else:
                            editor_id = await textarea.get_attribute("id")
                            if not editor_id:
                                raise BBError("comment_unavailable", "无法定位评语编辑器，未点击提交。")
                            await page.frame_locator(f'iframe[id="{editor_id}_ifr"]').locator("body").fill(comment)
                    # Wait for asynchronous upload widgets to finish before the final submit.
                    await page.wait_for_function("""() => !document.querySelector('[aria-busy="true"], .uploadInProgress, .uploading')""")
                    await page.locator(form["submit_selector"]).click()
                    await page.wait_for_load_state("domcontentloaded")
                    if self.session.identity != expected_identity:
                        return await self._uncertain(assignment, filename, original_form, payload, expected_identity)
                    await self.session.update_cookies({"cookies": await context.cookies(), "origins": []})
                    verify_identity = self.session.identity
                    receipt = await self._verify_receipt(page, course.id, assignment.id, filename, original_form, payload, verify_identity,
                                                         history_url=assignment.url)
                    if receipt:
                        return Result(message="作业已提交，已回读并核验提交记录。", data={"submitted": True, "course_id": course.id, "assignment_id": assignment.id, "filename": filename, **receipt})
                    return await self._uncertain(assignment, filename, original_form, payload, verify_identity)
                finally:
                    await context.close()
                    await browser.close()
        except BBError:
            if not wrote:
                raise
            return await self._uncertain(assignment, filename, original_form, payload, expected_identity)
        except Exception as exc:
            if wrote:
                return await self._uncertain(assignment, filename, original_form, payload, expected_identity)
            raise BBError("browser_unavailable", "提交浏览器未能启动或读取表单。请检查 Chromium 安装及网络，再重新预览。") from exc

    @staticmethod
    def _receipt(info, course_id, assignment_id, filename, before):
        correct_target = info.get("course_id") == course_id and info.get("assignment_id") == assignment_id
        expected_attempt = before.get("attempt_id")
        # A preallocated current attempt must complete as that very attempt. A
        # different, possibly old history row cannot prove this submission, even
        # when the old file happens to have exactly the same name and bytes.
        attempt_matches = bool(info.get("attempt_id")) and (
            info["attempt_id"] == expected_attempt if expected_attempt else True
        )
        attempt_matches = attempt_matches and info.get("attempt_id") != before.get("previous_attempt_id")
        return (correct_target and before.get("can_submit") and not before.get("has_receipt")
                and filename in info.get("existing_files", []) and info.get("has_receipt")
                and attempt_matches)

    async def _verified_info(self, info, course_id, assignment_id, filename, before, payload, identity):
        if self.session.identity != identity or not self._receipt(info, course_id, assignment_id, filename, before):
            return False
        expected_hash = hashlib.sha256(payload).hexdigest()
        for file in info.get("submitted_files", []):
            if file.get("name") != filename or file.get("attempt_id") != info.get("attempt_id"):
                continue
            try:
                response = await self.session.get(file["url"])
                if self.session.identity == identity and hashlib.sha256(response.content).hexdigest() == expected_hash:
                    return True
            except BBError:
                continue
        return False

    async def _verify_receipt(self, page, course_id, assignment_id, filename, before, payload, identity,
                              *, history_url):
        # Read actual history even when the immediate response is a generic success banner.
        info = submission_form(await page.content(), page.url)
        candidates = [history_url, page.url, *info.get("history_links", [])]
        # A post-submit page can still have action=newAttempt/submit in its URL.
        # Replaying such a GET is a write, never a receipt read.
        candidates = [url for url in dict.fromkeys(candidates)
                      if parse_qs(urlsplit(url).query).get("action", [""])[0].casefold()
                      in {"", "showhistory", "reviewassignment", "viewattempts"}]
        for delay in self.receipt_poll_delays:
            if self.session.identity != identity:
                return None
            if delay:
                await asyncio.sleep(delay)
            for url in candidates:
                try:
                    response = await self.session.get(url)
                    current = submission_form(response.text, str(response.url))
                    if await self._verified_info(current, course_id, assignment_id, filename, before, payload, identity):
                        return {"attempt_id": current.get("attempt_id"), "receipt_id": current.get("receipt_id"), "sha256": hashlib.sha256(payload).hexdigest(), "history_url": safe_public_url(str(response.url))}
                except BBError:
                    continue
        return None

    async def _uncertain(self, assignment, filename, before, payload, identity):
        evidence = {}
        try:
            if self.session.identity != identity:
                raise BBError("session_changed", "提交期间账号会话改变，未使用新账号查询回执。")
            response = await self.session.get(assignment.url)
            current = submission_form(response.text, str(response.url))
            if await self._verified_info(current, assignment.course_id, assignment.id, filename, before, payload, identity):
                return Result(message="提交响应未完成，但已回读历史并核验本次提交。", data={"submitted": True, "course_id": assignment.course_id, "assignment_id": assignment.id, "filename": filename, "attempt_id": current.get("attempt_id"), "history_url": safe_public_url(str(response.url))})
            if self.session.identity == identity:
                evidence = {key: current.get(key) for key in ("course_id", "assignment_id", "attempt_id", "existing_files", "has_receipt")}
        except BBError:
            pass
        return Result(ok=False, message="提交结果待确认，未自动重试。请在 BB 提交历史核对附件与回执。",
                      complete=False, data={"submitted": None, "history_url": safe_public_url(assignment.url), "evidence": evidence},
                      error={"code": "submission_unknown", "message": "提交可能已送达，请勿直接重复提交。"})
