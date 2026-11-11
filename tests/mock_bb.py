"""A deliberately small real HTTP Blackboard stand-in for browser submission QA.

All identities, receipts and file contents in these tests are fictional. The
server binds only to a random loopback port and never contacts USTC.
"""

import html
import threading
import time
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit


@dataclass
class State:
    behavior: str = "success"
    posts: list[dict] = field(default_factory=list)
    submitted: bool = False
    accepted: dict | None = None
    requirements: str = "Submit a single fictional report."
    enabled: bool = True
    form_course_id: str | None = None
    form_assignment_id: str | None = None
    browser_changed: bool = False
    comment_available: bool = True
    download_requests: list[dict] = field(default_factory=list)
    allow_new_attempt: bool = False
    new_attempts: int = 0
    history_delay_reads: int = 0
    history_reads: int = 0
    posted: threading.Event = field(default_factory=threading.Event)


class MockBlackboard:
    def __init__(self):
        self.state = State()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send_html(self, content, code=200):
                body = content.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    # Timeout tests intentionally leave a response unread.
                    pass

            def do_GET(self):
                part = urlsplit(self.path)
                params = parse_qs(part.query)
                if part.path == "/webapps/assignment/download":
                    owner.state.download_requests.append({key: values[0] for key, values in params.items()})
                    accepted = owner.state.accepted
                    if not accepted or not accepted["files"]:
                        self.send_html("<h1>Not found</h1>", 404)
                        return
                    attachment = accepted["files"][0]
                    body = attachment["content"]
                    if owner.state.behavior == "wrong_attachment_bytes":
                        body = b"Wrong server attachment content with the same filename."
                    elif owner.state.behavior == "old_attempt_same_name":
                        body = b"OLD UNRELATED REPORT from a different attempt."
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(attachment["name"]))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if part.path != "/webapps/assignment/uploadAssignment":
                    self.send_html("<h1>Not found</h1>", 404)
                    return
                course_id = params.get("course_id", ["_1_1"])[0]
                assignment_id = params.get("content_id", ["_2_1"])[0]
                if params.get("action") == ["newAttempt"]:
                    if not owner.state.allow_new_attempt or not owner.state.enabled:
                        self.send_html("<h1>Attempt unavailable</h1>", 403)
                        return
                    owner.state.new_attempts += 1
                    if owner.state.behavior != "new_attempt_old_history":
                        owner.state.submitted = False
                if owner.state.submitted and owner.state.accepted:
                    owner.state.history_reads += 1
                    if owner.state.history_reads <= owner.state.history_delay_reads:
                        self.send_html(owner.form(course_id, assignment_id))
                    else:
                        self.send_html(owner.receipt(course_id, assignment_id))
                else:
                    browser = "Chrome/" in self.headers.get("User-Agent", "")
                    self.send_html(owner.form(course_id, assignment_id, browser=browser))

            def do_POST(self):
                part = urlsplit(self.path)
                if part.path != "/webapps/assignment/uploadAssignment":
                    self.send_html("<h1>Not found</h1>", 404)
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                message = BytesParser(policy=policy.default).parsebytes(
                    ("Content-Type: " + self.headers.get("Content-Type", "")
                     + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body
                )
                fields = {}
                files = []
                for item in message.iter_parts():
                    name = item.get_param("name", header="content-disposition")
                    filename = item.get_filename()
                    if filename:
                        files.append({"name": filename, "content": item.get_payload(decode=True)})
                    else:
                        fields[name] = item.get_content()
                posted = {"fields": fields, "files": files}
                owner.state.posts.append(posted)
                owner.state.posted.set()
                if owner.state.behavior == "reject":
                    self.send_html('<h1>Submission rejected</h1><div class="error">File rejected by the platform.</div>', 403)
                    return
                if owner.state.behavior != "timeout_unknown":
                    owner.state.submitted = True
                    owner.state.accepted = posted
                if owner.state.behavior.startswith("timeout"):
                    time.sleep(2)
                self.send_response(303)
                self.send_header("Location", "/webapps/assignment/uploadAssignment?" + urlencode({
                    "course_id": fields.get("course_id", "_1_1"),
                    "content_id": fields.get("content_id", "_2_1"), "action": "showHistory",
                }))
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def url(self, course_id="_1_1", assignment_id="_2_1"):
        return self.base_url + "/webapps/assignment/uploadAssignment?" + urlencode({
            "course_id": course_id, "content_id": assignment_id,
        })

    def form(self, course_id, assignment_id, *, browser=False):
        course_id = self.state.form_course_id or course_id
        assignment_id = self.state.form_assignment_id or assignment_id
        instructions = self.state.requirements
        if browser and self.state.browser_changed:
            instructions += " Changed only in the browser."
        submit = '<input type="submit" name="bottom_Submit" value="Submit">' if self.state.enabled else ""
        initial_attempt = "_10_1" if self.state.behavior in {
            "success_same_attempt", "timeout_saved", "old_attempt_same_name", "old_attempt_identical_bytes",
        } else ""
        if self.state.new_attempts:
            initial_attempt = "_21_1"
        comment = '<textarea name="student_commentstext" id="student_commentstext"></textarea>' if self.state.comment_available else ""
        return f'''<!doctype html><html><head><meta charset="utf-8"></head><body>
        <h1 id="pageTitleText">Upload Assignment: Fictional report</h1>
        <div class="vtbegenerated">{html.escape(instructions)}</div>
        <p>Due Date: November 30, 2026 11:59 PM</p>
        <form id="uploadAssignmentFormId" method="post" enctype="multipart/form-data"
          action="/webapps/assignment/uploadAssignment?action=submit">
        <input type="hidden" name="course_id" value="{html.escape(course_id)}">
        <input type="hidden" name="content_id" value="{html.escape(assignment_id)}">
        <input type="hidden" name="attempt_id" value="{initial_attempt}">
        <input type="file" name="newFile" id="newFile_chooseLocalFile">
        {comment}
        {submit}</form></body></html>'''

    def receipt(self, course_id, assignment_id):
        posted = self.state.accepted
        filename = posted["files"][0]["name"] if posted and posted["files"] else "missing"
        attempt_id = "_20_1"
        if self.state.new_attempts and self.state.behavior not in {"new_attempt_old_history", "resubmit_old_receipt"}:
            attempt_id = "_21_1"
        elif self.state.behavior in {"success_same_attempt", "timeout_saved"}:
            attempt_id = "_10_1"
        elif self.state.behavior in {"old_attempt_same_name", "old_attempt_identical_bytes"}:
            attempt_id = "_9_1"
        confirmation = "" if self.state.behavior == "success_same_attempt" else '<span id="confirmationNumber">MOCK-RECEIPT-001</span>'
        href = "/webapps/assignment/download?" + urlencode({
            "course_id": course_id, "attempt_id": attempt_id, "file_id": "_30_1", "fileName": filename,
        })
        new_attempt = ""
        if self.state.allow_new_attempt and self.state.enabled:
            target = "/webapps/assignment/uploadAssignment?" + urlencode({
                "action": "newAttempt", "course_id": course_id, "content_id": assignment_id,
            })
            new_attempt = f'''<input type="submit" name="bottom_StartNew" value="Start New"
                onclick="document.location='{html.escape(target)}';">'''
        return f'''<!doctype html><html><head><meta charset="utf-8"></head><body>
        <h1 id="pageTitleText">Review Submission History: Fictional report</h1>
        <div class="vtbegenerated">{html.escape(self.state.requirements)}</div>
        <p>Due Date: November 30, 2026 11:59 PM</p>
        <input type="hidden" name="course_id" value="{html.escape(course_id)}">
        <input type="hidden" name="content_id" value="{html.escape(assignment_id)}">
        <input type="hidden" name="attempt_id" value="{attempt_id}">
        <div id="submittedFiles"><a href="{html.escape(href)}">{html.escape(filename)}</a></div>
        {confirmation}
        {new_attempt}
        <p>Submission received.</p></body></html>'''

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
