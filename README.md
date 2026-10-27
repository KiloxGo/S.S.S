## 凭据与边界

三个平台凭据怎么填

- `SSS_CANVAS_TOKEN`：`oc.sjtu.edu.cn` 账户设置中的 API Token，只填原值，不加 `Bearer `。
- `SSS_SHARE_TOKEN`：登录 `share.dyweb.sjtu.cn` 后，在开发者工具 Application → Local Storage 读取 `token`，去掉 JSON 外层双引号。
- `SSS_REVIEWS_COOKIE`：直接粘贴 `course.sjtu.plus` 的 `jcourse_session` 原始值，程序会自动补上 Cookie 名。

## 文档

| 文档 | 说明 |
| --- | --- |
| [`docs/schema.md`](docs/schema.md) | 输出目录与文件规范 |
| [`docs/study-prompts.md`](docs/study-prompts.md) | 学习提示词与 Agent 入口 |
| [`docs/desktop-packaging.md`](docs/desktop-packaging.md) | Windows / macOS 桌面打包与便携数据 |
| [`.env.example`](.env.example) | 三个平台的凭据模板 |
| [`examples/course.json`](examples/course.json) | 课程映射示例 |
| [`AGENTS.md`](AGENTS.md) | 仓库开发与学习约定 |

## 开发与验证

```bash
python -m pip install -e ".[web,test]"
python -m pytest -q

npm --prefix web run build
npm --prefix web test
```
