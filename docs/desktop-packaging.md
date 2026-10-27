# 桌面应用与便携分发

桌面版复用 React 页面与 Python 采集逻辑，通过 pywebview 打开系统原生窗口，PyInstaller 将解释器、依赖、静态页面与学习模板一起打包。使用者解压后双击运行，无需安装 Python、Node.js 或启动终端。

## 使用

| 分发包 | 启动入口 |
| --- | --- |
| `SSS-<版本>-windows-x64.zip` | 解压完整目录，双击 `SSS/SSS.exe` |
| `SSS-<版本>-macos-arm64.zip` | Apple Silicon Mac：双击 `SSS.app` |
| `SSS-<版本>-macos-x64.zip` | Intel Mac：双击 `SSS.app` |

Windows 需要 Edge WebView2 Runtime，旧系统可能需要安装 [微软官方运行时](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)。macOS 使用系统 WKWebView。支持系统下限也受构建时 Python 和依赖的最低系统版本约束；在目标系统实测后再声明兼容范围。

主页直接显示课程列表。“平台配置”填写三个平台凭据；同步、打开课程文件夹、复制学习提示词的操作与本地网页版相同。Canvas 用于获取个人课程列表，其他两个平台提供补充资料。

当前构建没有配置 Windows 代码签名或 Apple Developer 签名与公证，系统可能提示无法验证发布者。公开发布前需完成相应签名流程；本仓库不会自动上传或发布安装包。

## 数据位置与迁移

应用旁有 `portable.flag` 且目录可写时，实际数据放在旁边的 `SSS-Data/`；Windows 的标记位于 `SSS.exe` 同级，macOS 位于 `SSS.app` 同级。应用目录不可写或找不到标记时回退到用户目录：

- Windows：`%LOCALAPPDATA%/SSS/`
- macOS：`~/Library/Application Support/S.S.S/`

macOS 从下载隔离位置启动时可能回退到用户目录。以“平台配置”中显示的实际路径为准，可直接点击“打开”。

数据目录包含 `.env`、`.local/` 和 `data/`，分别用于平台凭据、状态与缓存、课程资料。启动器只读取所选数据目录中的配置文件；环境变量 `SSS_*` 仍可覆盖配置。开发仓库的 `.env`、缓存与课程不会被打进分发包。

迁移时退出应用，连同应用和 `SSS-Data` 一起移动；已经回退到用户目录时需一并迁移该目录。升级时保留原数据目录，仅替换程序文件。给其他人分发原始 ZIP，避免把自己的 `SSS-Data` 打包进去。

同一数据目录只允许一个桌面实例。关闭窗口时，正在同步会先询问；确认后等待当前任务安全结束，未开始的排队任务取消。诊断日志保存在 `.local/app.log`，文件会轮换。

## 从源码运行与构建

在目标操作系统原生构建。按 [PyInstaller 的工作方式](https://pyinstaller.org/en/stable/operating-mode.html)，macOS 构建机不直接生成 Windows 可执行程序。

```bash
python -m pip install -e ".[build,test]"
npm --prefix web ci
npm --prefix web run build
python -m sss.desktop --data-dir .local/desktop-dev

python scripts/build_desktop.py --skip-web-build
```

省略 `--skip-web-build` 会先安装前端锁定依赖并构建。Windows 构建机需安装完整 7-Zip（包含 `7z.exe`、`7z.dll`、`License.txt`）；构建脚本会携带解码器和许可，接收者不需另装解压软件。macOS 使用系统 bsdtar 解压 RAR。ZIP、TAR、7Z 在应用内处理。

输出在 `dist/`：应用目录、ZIP 和 SHA-256 校验文件。已存在的输出不会被覆盖，应先移走旧构建。包内附有 `使用说明.txt`、第三方组件许可与 `build-check.json`。后者记录打包程序自检结果，检查本地接口、JS/CSS、学习模板、ZIP/7Z 解压及全新数据目录的凭据隔离。

在有图形会话的构建机可追加 `--window-test`，用打包后的原生窗口自动打开配置、复制提示词并验证系统剪贴板，测试结束后恢复原剪贴板。此测试会短暂显示应用窗口。

## CI 构建

[desktop.yml](../.github/workflows/desktop.yml) 提供手动触发的构建任务，分别在 Windows x64、macOS arm64、macOS Intel 上运行 Python 测试、前端构建、打包与无窗口自检，再上传 ZIP 和 SHA-256 为 Actions artifacts。不会自动创建 Release。图形窗口、实际登录与同步仍需各平台实机验证。
