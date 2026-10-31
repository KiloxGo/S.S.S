# S.S.S

**SJTU Study Speedrun** — 上海交通大学课程资料整合与学习辅助工具。

S.S.S 旨在简化课程资料的收集、整理与使用流程，让同学们将更多时间花在学习本身，而不是寻找资料上。

## Features

- **多源整合**：从 Canvas、交大云盘及课程评价平台获取课程相关资料。
- **自动整理**：按学期与课程归档文件，建立统一、可检索的资料目录。
- **AI 辅助学习**：生成面向 AI Agent 的课程索引与学习指引，便于检索、总结和复习。
- **本地优先**：资料与平台凭据保存在本地，支持 Windows 和 macOS 桌面应用。

## Getting Started

可从 [GitHub Actions](https://github.com/KiloxGo/S.S.S/actions/workflows/desktop.yml) 获取可用的构建产物，或从源码运行（需要 Python 3.11.8+ 和 Node.js）：

```bash
git clone https://github.com/KiloxGo/S.S.S.git
cd S.S.S

python -m pip install -e ".[desktop]"
npm --prefix web ci
npm --prefix web run build

sss-desktop
```

首次运行需要配置相应平台的访问凭据。

## Documentation

- [课程目录规范](docs/schema.md)
- [AI 学习指引](docs/study-prompts.md)
- [桌面应用与构建说明](docs/desktop-packaging.md)

---

*Made for SJTU students. Not affiliated with Shanghai Jiao Tong University.*
