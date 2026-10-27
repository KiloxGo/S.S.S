import re
import unicodedata
from decimal import Decimal, InvalidOperation

from requests.adapters import HTTPAdapter

from .http import Http, SourceError

SHARE_BASE = "https://api-v2.share.dyweb.sjtu.cn"
REVIEWS_BASE = "https://course.sjtu.plus"


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))


def same_course(candidate, plan):
    return normalized(candidate.get("code")).upper() in {normalized(c).upper() for c in [plan["code"], *plan.get("aliases", [])]}


def free_material(material):
    # Missing, boolean, malformed, and negative prices must never authorize acquisition.
    price = material.get("points")
    if price is None or isinstance(price, bool):
        return False
    try:
        return Decimal(str(price)) == 0
    except InvalidOperation:
        return False


def teacher_bucket(teachers, own):
    names = {normalized(t.get("name") or t.get("display_name")) for t in teachers if t}
    names.discard("")
    if not names or not own:
        return "unknown-teachers"
    return "own-teachers" if names & {normalized(n) for n in own} else "other-teachers"


def paginated(http, path, params=None):
    items, seen = [], set()
    for page in range(1, 10001):
        data = http.json("GET", path, params={**(params or {}), "page": page, "page_size": 100})
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise SourceError("Community API returned an incompatible paginated response")
        for item in data["items"]:
            key = item.get("id")
            if key is None or key in seen:
                raise SourceError("Community pagination repeated or omitted an ID")
            seen.add(key)
            items.append(item)
        total = data.get("total")
        if total is not None and len(items) >= int(total):
            return items
        if not data["items"]:
            if total is not None and len(items) < int(total):
                raise SourceError("Community pagination ended before total was reached")
            return items
        if total is None and len(data["items"]) < int(data.get("page_size", 100)):
            return items
    raise SourceError("Community pagination limit reached")


class Reviews:
    def __init__(self, cookie=""):
        cookies = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
        allowed = {k: v for k, v in cookies.items() if k in ("jcourse_session", "csrftoken", "acw_tc", "cdn_sec_tc")}
        self.http = Http(REVIEWS_BASE, cookies=allowed)

    def courses(self, plan):
        found = {}
        for code in [plan["code"], *plan.get("aliases", [])]:
            for course in paginated(self.http, "/api/course/", {"q": code}):
                if same_course(course, plan):
                    found[course["id"]] = course
        for cid in plan.get("reviews_ids", []):
            course = self.http.json("GET", f"/api/course/{int(cid)}")
            found[course["id"]] = course
        return list(found.values())

    def detail(self, cid):
        return self.http.json("GET", f"/api/course/{cid}")

    def reviews(self, cid):
        return paginated(self.http, f"/api/course/{cid}/review", {"order_by": "created_at"})


class Share:
    def __init__(self, token):
        if not token:
            raise SourceError("传承交大 requires share_token (site localStorage token / Auth header); Google cookies cannot authenticate this site")
        self.http = Http(SHARE_BASE, {"Auth": token})
        # Although the official API mutates state via GET, acquisition must never auto-retry.
        self.http.session.mount(SHARE_BASE + "/api/v1/material/purchase/", HTTPAdapter(max_retries=0))

    def call(self, method, path, **kwargs):
        data = self.http.json(method, SHARE_BASE + path, **kwargs)
        if not isinstance(data, dict) or data.get("success") is not True:
            raise SourceError("传承交大 API failed; check authentication and access")
        return data.get("data")

    def courses(self, plan):
        found = {}
        for code in [plan["code"], *plan.get("aliases", [])]:
            data = self.call("POST", "/api/v1/course/search", json={"keyword": code})
            if not isinstance(data, list):
                raise SourceError("传承交大 search schema changed")
            for course in data:
                if same_course(course, plan):
                    found[course["id"]] = course
        for cid in plan.get("share_ids", []):
            course = self.call("GET", f"/api/v1/course/get/{int(cid)}")
            found[course["id"]] = course
        # Follow the site's explicit old/new course relationships, including renamed codes.
        queue = list(found)
        while queue:
            cid = queue.pop(0)
            detail = self.call("GET", f"/api/v1/course/get/{int(cid)}")
            found[cid] = detail
            related = [detail.get("latest_course"), *(detail.get("deprecated_courses") or [])]
            for other in related:
                if other and other["id"] not in found:
                    found[other["id"]] = other
                    queue.append(other["id"])
            if len(found) > 100:
                raise SourceError("Excessive course relationships")
        return list(found.values())

    def materials(self, cid):
        data = self.call("GET", "/api/v1/material", params={"course_id": cid})
        if not isinstance(data, dict) or not {"classes", "unarchived"} & data.keys():
            raise SourceError("传承交大 material list schema changed")
        classes, unarchived = data.get("classes") or [], data.get("unarchived") or []
        if not isinstance(classes, list) or not isinstance(unarchived, list):
            raise SourceError("传承交大 material list schema changed")
        materials = [{**m, "class": m.get("class")} for m in unarchived]
        for group in classes:
            if group.get("materials") is None and group.get("material_count", 0) == 0:
                continue
            if not isinstance(group.get("materials"), list):
                raise SourceError("传承交大 omitted a class material list")
            for material in group["materials"]:
                inherited = {k: v for k, v in group.items() if k != "materials"}
                merged = {**inherited, **(material.get("class") or {})}
                merged["teacher"] = merged.get("teacher") or inherited.get("teacher")
                materials.append({**material, "class": merged})
        return materials

    def classes(self, cid):
        return self.call("GET", f"/api/v1/course/get/{cid}/classes")

    def material(self, mid):
        return self.call("GET", f"/api/v1/material/get/{mid}")

    def acquire_free(self, mid):
        # Re-fetch immediately before the state-changing GET used by the official frontend.
        detail = self.material(mid)
        if not isinstance(detail, dict) or not free_material(detail) or any(detail.get(k) for k in ("block", "blocked", "is_blocked")):
            raise SourceError("Refused acquisition: current points price is not explicitly zero")
        if not detail.get("has_purchased") and not detail.get("is_mine"):
            self.call("GET", f"/api/v1/material/purchase/{mid}")
        url = self.call("GET", f"/api/v1/material/download/{mid}")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise SourceError("传承交大 returned an invalid download URL")
        return detail, url
