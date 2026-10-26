import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .http import Http, SourceError

CANVAS_BASE = "https://oc.sjtu.edu.cn"
VIDEO_BASE = "https://v.sjtu.edu.cn/jy-application-resourcemanage"
VIDEO_UI = "https://v.sjtu.edu.cn/jy-application-resourcemanage-ui/"


def course_code(course):
    match = re.search(r"(?:^|-)([A-Z]{2,}\d[A-Z0-9]*)(?:-|$)", course.get("course_code") or "")
    return match[1] if match else f"canvas-{course['id']}"


def current_term(course, now):
    term = course.get("term") or {}
    if not term.get("start_at") or not term.get("end_at"):
        return False
    start = datetime.fromisoformat(term["start_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(term["end_at"].replace("Z", "+00:00"))
    return start <= now < end


def group_courses(courses, *, term=None, now=None):
    now = now or datetime.now(timezone.utc)
    groups = {}
    for c in courses:
        if not c.get("name") or c.get("access_restricted_by_date"):
            continue
        roles = {e.get("type") for e in c.get("enrollments", [])}
        if roles and not roles & {"student", "observer"}:
            continue
        t = c.get("term") or {}
        if term is not None:
            if str(t.get("id")) != str(term) and t.get("name") != term:
                continue
        elif not current_term(c, now):
            continue
        code = course_code(c)
        key = (t.get("id"), code)
        g = groups.setdefault(key, {"code": code, "name": c["name"], "term": {"id": t.get("id"), "name": t.get("name")},
                                    "canvas_ids": [], "teachers": [], "canvas_spaces": []})
        g["canvas_ids"].append(c["id"])
        names = [t.get("display_name") or t.get("name") for t in c.get("teachers") or []]
        g["canvas_spaces"].append({"id": c["id"], "roles": sorted(r for r in roles if r), "teachers": [n for n in names if n]})
        for teacher in c.get("teachers") or []:
            name = teacher.get("display_name") or teacher.get("name")
            if name and name not in g["teachers"] and (not roles or "student" in roles):
                g["teachers"].append(name)
    return sorted(groups.values(), key=lambda g: g["code"])


def api_data(value):
    if not isinstance(value, dict) or str(value.get("status", 200)) != "200":
        raise SourceError("Video API reported a failure")
    data = value.get("data") if value.get("data") is not None else value.get("result")
    if data is None:
        raise SourceError("Video API returned no data")
    return data


def srt(lines):
    def stamp(ms):
        ms = max(0, int(ms))
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    result = []
    for i, line in enumerate(lines):
        start = int(line.get("bg", 0))
        end = int(line.get("ed") or (lines[i + 1]["bg"] if i + 1 < len(lines) else start + 2000))
        if end <= start:
            end = start + 1000
        result.append(f"{i + 1}\n{stamp(start)} --> {stamp(end)}\n{line.get('res', '')}\n")
    return "\n".join(result)


class Canvas:
    def __init__(self, token):
        if not token:
            raise SourceError("Set SSS_CANVAS_TOKEN or canvas_token in local credentials")
        self.http = Http(CANVAS_BASE, {"Authorization": "Bearer " + token})

    def courses(self):
        return list(self.http.pages("/api/v1/courses", {"include[]": ["teachers", "term"], "per_page": 100}))

    def items(self, cid, kind, **params):
        return list(self.http.pages(f"/api/v1/courses/{cid}/{kind}", {"per_page": 100, **params}))

    def file(self, fid):
        return self.http.json("GET", f"/api/v1/files/{fid}")

    def linked_files(self, description):
        ids = set()
        for tag in BeautifulSoup(description or "", "html.parser").select("[href], [src]"):
            url = urljoin(CANVAS_BASE, tag.get("href") or tag.get("src") or "")
            if urlsplit(url).hostname != urlsplit(CANVAS_BASE).hostname:
                continue
            match = re.search(r"/(?:api/v1/)?files/(\d+)(?:/|$)", urlsplit(url).path)
            if match:
                ids.add(int(match[1]))
        return sorted(ids)

    def video_token(self, cid):
        launch = f"{CANVAS_BASE}/courses/{cid}/external_tools/8329"
        session = self.http.json("GET", "/login/session_token", params={"return_to": launch})
        session_url = session.get("session_url", "")
        if urlsplit(session_url).hostname != "oc.sjtu.edu.cn":
            raise SourceError("Canvas returned an unexpected web-session host")
        response = self.http.request("GET", session_url)
        # The token API bootstraps a web cookie, then Canvas and the provider exchange LTI forms.
        for _ in range(6):
            fragment = urlsplit(response.url).fragment
            params = parse_qs(fragment.partition("?")[2] or fragment)
            token = params.get("jwt_token", [None])[0]
            if token:
                response.close()
                return token
            doc = BeautifulSoup(response.text, "html.parser")
            form = doc.find("form")
            if not form:
                response.close()
                raise SourceError("Video LTI launch failed: no token/form; web access may be unavailable")
            action = urljoin(response.url, form.get("action", ""))
            if urlsplit(action).hostname not in ("oc.sjtu.edu.cn", "v.sjtu.edu.cn") or urlsplit(action).scheme != "https":
                response.close()
                raise SourceError("Unexpected LTI form destination")
            fields = {i["name"]: i.get("value", "") for i in form.select("input[name]")}
            response.close()
            response = self.http.request("POST", action, data=fields)
        raise SourceError("Video LTI form limit reached")

    def videos(self, cid):
        token = self.video_token(cid)
        http = Http(VIDEO_BASE, {"jwt-token": token, "Referer": VIDEO_UI})
        context = api_data(http.json("GET", VIDEO_BASE + "/lms/launch-context"))
        teaching_id = (context.get("canvasRecord") or {}).get("teachingClassId")
        if not teaching_id:
            raise SourceError("Missing teaching class in video launch context")
        records, seen = [], set()
        for page in range(1, 1001):
            data = api_data(http.json("GET", VIDEO_BASE + "/v1/subject_vod_list_new", params={
                "page.pageIndex": page, "page.pageSize": 100, "teclIds": teaching_id,
                "page.orders[0].asc": "true", "page.orders[0].field": "courBeginTime", "schoolOpenStatusFlag": "false"}))
            if not isinstance(data.get("records"), list):
                raise SourceError("Video list lacks records")
            batch = data["records"]
            for record in batch:
                vid = str(record.get("id") or record.get("courseId") or record.get("courId") or "")
                if not vid or vid in seen:
                    raise SourceError("Missing or repeated video ID during pagination")
                seen.add(vid)
                records.append(record)
            if not batch or len(records) >= int(data.get("total", 10**12)) or len(batch) < 100:
                return http, records
        raise SourceError("Video pagination limit reached")

    @staticmethod
    def subtitles(http, vid):
        response = http.json("GET", VIDEO_BASE + f"/v1/course/ai/translate/{vid}", params={"useOriginal": "true"})
        if isinstance(response, dict) and str(response.get("status", 200)) == "200" and response.get("data") is None and response.get("result") is None:
            return {"beforeAssemblyList": [], "afterAssemblyList": [], "availability": "unavailable"}
        data = api_data(response)
        if not isinstance(data, dict):
            raise SourceError("Invalid subtitle response")
        return data
