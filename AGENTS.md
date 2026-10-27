# S.S.S 工作说明

本仓库是课程资料采集工具。先区分用户是在开发工具，还是使用已下载的资料学习课程。

## 学习课程

定位 `data/<学期>/<课程号>-<课程名>/`，先读该课程的 `AGENTS.md`（Claude 也可从 `CLAUDE.md` 进入），再读课程 `README.md` 与 `SCHEMA.md`。这些课程级文件说明来源、证据标准、字幕检索与学习产出。

没有生成入口时，先读项目的 `docs/schema.md`、`docs/study-prompts.md` 和 `sss/study_resources/agent-guide.md`；模板中的占位符不是课程事实。根据用户指定的课程和实际材料工作。

查考核或课程安排时关注字幕中的考试、成绩、分数、实验、各种安排、考试重点、签到、考试范围、考试提示、考试技巧、作业注意点、出勤及相关说法。阅读上下文，给文件与时间戳，区分本学期本班要求、往届经验和待核实事项。个人指南是参考材料，不能自动当作官方要求。

学习产物放在课程的 `notes/`，保留原始课件、本人提交和已有个人笔记。学习任务无需访问 `.env`、凭据、私有缓存或 SQLite。不要将用户个人指南全文复制到仓库的通用模板。

## 开发工具

- Python 采集与导出在 `sss/`，本地页面在 `web/`，学习模板在 `sss/study_resources/`。
- 课程目录规范以 `sss/study_resources/schema.md` 为准；`docs/schema.md` 是入口。同步时将完整规范复制为课程根目录的 `SCHEMA.md`。
- 修改导出逻辑时维护 HTML/Markdown 链接、题目/本人提交语义、来源独立刷新、私有凭据隔离，并保留 `notes/` 与手写 Agent 说明。
- 涉及对应功能时运行 Python 测试 `.venv/bin/python -m pytest -q`，前端构建 `npm --prefix web run build`，浏览器交互测试 `npm --prefix web test`。按改动范围选择，不重复无关检查。
