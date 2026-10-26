import argparse
import json
import sys
from pathlib import Path

from .canvas import Canvas, group_courses
from .communities import Reviews, Share, paginated, normalized
from .config import credential_values, load_config
from .http import SourceError
from .storage import Store
from .sync import Sync, validate_plan


def select_course(config, selector, term):
    courses = group_courses(Canvas(config.get("canvas_token")).courses(), term=term)
    matches = [c for c in courses if c["code"].casefold() == selector.casefold() or selector in [str(i) for i in c["canvas_ids"]]]
    if len(matches) != 1:
        raise SourceError("Course selection must match one current-term group; use courses or specify --term")
    result = {"schema_version": 1, **matches[0], "aliases": [], "share_ids": [], "reviews_ids": []}
    try:
        api = Reviews(config.get("reviews_cookie", ""))
        candidates = paginated(api.http, "/api/course/", {"q": result["name"]})
        result["review_candidates"] = [{k: c.get(k) for k in ("id", "code", "name", "main_teacher", "rating")} for c in candidates if normalized(c.get("code")) != normalized(result["code"])]
    except SourceError:
        result["mapping_note"] = "Community name search unavailable; inspect historical aliases before collecting reviews."
    return result


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="S.S.S — SJTU 复习资料命令行工具")
    parser.add_argument("--config", type=Path, default=Path(".local/credentials.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    courses = commands.add_parser("courses", help="列出本学期课程，同课程号合并多个 Canvas 空间")
    courses.add_argument("--term", help="Canvas 学期 ID 或完整学期名；默认按学期起止日期筛选")
    courses.add_argument("--all", action="store_true", help="列出全部学期的课程")
    plan = commands.add_parser("plan", help="创建可编辑课程映射")
    plan.add_argument("--course", required=True, help="课程号或 Canvas ID")
    plan.add_argument("--term")
    plan.add_argument("--out", type=Path, required=True)
    sync = commands.add_parser("sync", help="下载课程资料；失败项写入 manifest，重跑可恢复")
    choice = sync.add_mutually_exclusive_group(required=True)
    choice.add_argument("--plan", type=Path)
    choice.add_argument("--course")
    sync.add_argument("--term")
    sync.add_argument("--output", type=Path, default=Path("data"))
    sync.add_argument("--sources", default="canvas,subtitles,share,reviews")
    commands.add_parser("doctor", help="检查三个平台的认证；不回显凭据")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            failures = 0
            checks = {
                "canvas": lambda: Canvas(config.get("canvas_token")).http.json("GET", "/api/v1/users/self/profile"),
                "share": lambda: Share(config.get("share_token")).call("GET", "/api/v1/user/profile"),
                "reviews": lambda: Reviews(config.get("reviews_cookie", "")).http.json("GET", "/api/auth/me"),
            }
            for name, fn in checks.items():
                try:
                    if not fn():
                        raise SourceError("Not authenticated")
                    print(f"{name}: OK")
                except SourceError as exc:
                    print(f"{name}: {exc}")
                    failures += 1
            return 2 if failures else 0
        if args.command == "courses":
            raw = Canvas(config.get("canvas_token")).courses()
            if args.all:
                terms = sorted({str(c["term"]["id"]) for c in raw if c.get("term")})
                items = [g for term in terms for g in group_courses(raw, term=term)]
            else:
                items = group_courses(raw, term=args.term)
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0
        if args.command == "plan":
            if args.out.exists():
                raise SourceError("Plan file exists; choose a new path or edit the existing mapping")
            result = select_course(config, args.course, args.term)
            Store(args.out.parent, credential_values(config)).json(args.out, result)
            print(f"Plan saved: {args.out.resolve()}")
            return 0
        sources = args.sources.split(",")
        if not sources or len(sources) != len(set(sources)) or set(sources) - {"canvas", "subtitles", "share", "reviews"}:
            raise SourceError("--sources accepts unique names: canvas,subtitles,share,reviews")
        mapping = validate_plan(json.loads(args.plan.read_text(encoding="utf-8"))) if args.plan else select_course(config, args.course, args.term)
        job = Sync(config, mapping, args.output)
        report = job.run(sources)
        print(f"{report['status']}: {job.root / 'manifest.json'}")
        for error in report["errors"]:
            print(f"  {error['scope']}: {error['message']}")
        return 2 if report["errors"] else 0
    except SourceError as exc:
        print(f"sss: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"sss: local configuration or processing error ({type(exc).__name__})", file=sys.stderr)
        return 2
