# 目录与清单 schema v1

课程根目录：`<output>/<term.name 或 term.id>/<code>-<name>/`。Windows 非法字符和保留名称会转义；长名称截断并加哈希；下载文件及文件夹保留远端 ID。

`course.json` 为本次课程映射快照，包含 `schema_version`、`code`、`name`、`term`、`canvas_ids`、`teachers`，以及可选 `aliases`、`share_ids`、`reviews_ids`、`review_candidates`。

`manifest.json` 字段：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 当前为整数 1 |
| `course` | 本次使用的课程映射 |
| `started_at` / `finished_at` | UTC ISO 8601 时间 |
| `status` | `running` / `complete` / `partial` |
| `sources` | 各选中来源的 `status` 与计数 `summary` |
| `artifacts` | 本次已下载或缓存命中的文件 |
| `errors` | `{scope, message}`；scope 精确到来源、空间或资料 ID |

artifact 包含课程根目录相对 `path`、字节数 `size`、`sha256`、服务端 `updated`、`status`（`downloaded` / `cached`），以及成功解压后的相对路径列表 `extracted`。下载成功但解压失败时，artifact 和 error 同时保留，原包可用。

每次重跑清单代表本次结果；本地旧文件不自动删除。文件的 `.receipt.json` 保存校验和与更新时间。中断时 `manifest.json` 保持 `running`，已完成下载仍可在下次重跑时复用；`.part` 不会被当成完整文件。

Canvas 文件：`canvas/<canvas_id>/files/<folder_id-name>/.../<file_id-filename>`。作业：`assignments/<assignment_id-name>/`。文件 API 中所有可见条目均保存在 `files.json`，锁定文件也有元数据和清单错误记录。HTML 描述中的链接已去掉签名参数，需要重新下载时通过远程 ID 获取新链接。

字幕：`canvas/<canvas_id>/subtitles/recordings.json` 保存录播列表。每条录播保存原始字幕 JSON；有文本才生成 `.srt` 和 `.txt`。JSON 中 `availability: unavailable` 或空字幕列表表示平台未提供字幕。平台错误与权限问题写入 `errors`。

传承交大：`share/<class_id-teacher>/<material_id>/<file_name>`，并保存 `material.json`。`<course_id>-materials.json` 包含所有教师和未归档资料条目，收费/屏蔽条目也保留供核对。课程关联会遍历站点显式提供的 `latest_course` / `deprecated_courses`。

选课社区：`reviews/courses.json` 保存匹配条目。每个教师分类目录包含 `<id>-course.json`、`<id>-reviews.json`、汇总 `all.json` 和可读 `README.md`。每条汇总保留原评价、来源课程 ID 和教师名单，原评价中的时间、评分等字段不丢弃。

解压：原文件旁的 `<filename>.extracted/` 存放内容，`.sss-extracted.json` 存放原包 SHA-256。嵌套解压使用同样命名。ZIP 格式的 Office 文件（`.docx` / `.pptx` / `.xlsx`）保持文档本身，不视为用户压缩包。

所有元数据写入前移除凭据字段、已配置的凭据值及 URL query/fragment；下载的原文件不改写。本地凭据和采集结果均默认排除在 Git 外。
