# S.S.S

上海交通大学复习资料采集工具。Python 命令行后端，汇集 Canvas、传承交大和选课社区的学习内容。

## 输出直接阅读

同步完成后，打开课程目录的 **`index.html`** 浏览，或从 **`README.md`** 开始让 agent 阅读。HTML 页之间链接到 HTML，Markdown 页之间链接到 Markdown。资料目录不再生成课程清单 JSON、文件元数据 JSON、下载 receipt 或字幕 JSON/TXT。

```text
data/2026-2027 Fall/EE3316-电气工程基础/
  index.html                      # 人阅读的入口
  README.md                       # agent 阅读的入口
  canvas/
    93532-徐潇源/
      index.html / README.md
      files/                      # 课程文件、可读文件索引
      assignments/
        HW2-1-426150/
          question/               # 题面正文、题目附件
            index.html / README.md
            13858939-HW2-1new.docx
          solution/               # 我提交的附件、在线正文或链接
            index.html / README.md
            13913405-电气工程基础hw2-1.pdf
      subtitles/                  # 仅 SRT
    93538-宋依群/...
  share/
    25-26秋电工基1期末考试题目回忆-54277/
      index.html / README.md
      2025-2026电气工程基础期末考题回忆.pdf
  reviews/
    own-teachers/                 # 本班教师评价正文
      index.html / README.md
    other-teachers/               # 其他教师评价正文
    unknown-teachers/
```

- Canvas 文件夹带教师姓名和空间 ID；学生和旁听身份的同课程空间可以一同采集。本班教师默认只取学生身份空间的教师。
- 作业页面中直接书写的题目会保留段落、表格、列表、数学文本和图片链接，转成 HTML/Markdown。附件题的附件归入 `question`，本人提交的文件和在线正文归入 `solution`；未提交时不创建空的 `solution`。
- 文件内容按 SHA-256 去重，跨空间、课件、作业和传承资料的相同文件只保留一份；其他位置的阅读页会链接到保留的文件。HTML 和 Markdown 是特意保留的两种阅读视图，不当作重复文件删除。
- 字幕只输出 SRT，录播列表链接放在教师目录的阅读页中。
- 评论只整理正文、教师姓名和原评价链接，不导出分数、用户 ID、时间戳、投票等接口字段。
- 所有教师的传承资料集中放在 `share/` 下，子目录使用原上传标题，不再分教师；原始文件名仍保留。成功解压后展示解压内容，原压缩包留在私有缓存；无法解压时保留原包并报告原因。中文 GBK ZIP 文件名会尝试正确解码。

## 安装

Python 3.11.8 或更新版本。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

下面命令中的 `python` 应使用此虚拟环境，或替换成 `.\.venv\Scripts\python`。安装后也可使用 `sss` 命令。

RAR 通常需要 PATH 中有 `unrar` 或 `unar`。ZIP、TAR、7Z 不需要外部命令。加密包和分卷包不会猜密码或自动拼接。

## 本地认证

复制 `credentials.example.json` 到 `.local/credentials.json` 并填写本地凭据。这些 JSON 是工具输入配置，不是导出的学习资料：

```json
{
  "canvas_token": "",
  "share_token": "",
  "reviews_cookie": "jcourse_session=..."
}
```

- Canvas：`oc.sjtu.edu.cn` 账户设置中的 Token。
- 传承交大：登录 `share.dyweb.sjtu.cn` 后，在开发者工具 Application → Local Storage 读取 `token`。API 使用 `Auth` 请求头，Google Cookie 不能代替。
- 选课社区：`course.sjtu.plus` 的 `jcourse_session`；可附带同域 `csrftoken`、`acw_tc`、`cdn_sec_tc`，其他域 Cookie 不使用。

环境变量 `SSS_CANVAS_TOKEN`、`SSS_SHARE_TOKEN`、`SSS_REVIEWS_COOKIE` 可覆盖文件。`--config` 放在子命令之前。凭据不会写进阅读页，签名 URL 的凭据参数不保留，跨域重定向会剥离认证头。下载的原始学习文件不作凭据内容改写。

## 采集课程

```powershell
python -m sss doctor
python -m sss courses
python -m sss courses --all
python -m sss courses --term 30
python -m sss plan --course EE3316 --out .local/EE3316.json
python -m sss sync --plan .local/EE3316.json
python -m sss sync --plan .local/EE3316.json --sources canvas,subtitles
python -m sss sync --course EE3316 --output D:\Study
```

本学期按 Canvas 的学期起止日期筛选。同学期、同课程号的空间自动归并；也可用任一 Canvas ID 选中整个归并组。

映射格式见 [`examples/course.json`](examples/course.json)。`canvas_ids` 指定空间，`canvas_spaces` 保存各空间教师和身份，`teachers` 指定用于评论分类的本班教师。`aliases` 用于历史课程号，`share_ids`、`reviews_ids` 可明确指定社区课程条目。已有映射不会被覆盖。

`plan` 会将同名的其他课程号列入 `review_candidates`，供核对后填入 `aliases`。例如 EE3316 的历史参考评价在 EE3310「电气工程基础（1）」下，可以填写 `"aliases": ["EE3310"]`；不会自动合并 ECE3600J 或 EE3311「电气工程基础（2）」。传承交大自身提供的旧新课程关联会自动遍历。

退出码 `0` 表示选中来源完成，`2` 表示有未取得内容或配置问题。阅读入口底部用可读文字列出状态与原因。某个来源失败后仍继续其他来源；只刷新部分来源时保留其他来源的阅读内容。

## 缓存、迁移与边界

下载缓存、校验信息和内部同步状态存入输出目录旁 `.local/state/<课程路径哈希>/` 下的 SQLite 和内容缓存，资料文件旁没有 receipt。缓存命中仍校验 SHA-256；字节数不符的下载不会替换完整文件。

首次更新旧格式目录时，先将旧 receipt 导入缓存，再生成新格式。旧目录完整备份到私有状态目录的 `legacy/`，不会继续显示在课程目录。后续更新先在临时目录生成，再替换课程目录，上一版保存在 `previous/`。`.local/`、`data/`、`.env*`、虚拟环境均被 Git 忽略。不要同时向同一个课程目录同步。

只获取账号可访问的内容。Canvas 标记为 `hidden_for_user` 的附件可能只是不出现在文件导航中，仍允许从作业链接访问；真正 `locked_for_user` 的文件不下载。仅覆盖当前 Canvas 8329 LTI 对接的 `v.sjtu.edu.cn` 字幕服务，不下载视频本体或提交作业。

传承交大只获取明确为 0 积分的资料，并在购买前重新检查价格；购买请求不自动重试。接口没有可核实的原子价格上限参数，仍无法消除两次请求之间服务端改价的竞态。默认每个文件限 4 GiB；每个根压缩包连同嵌套内容限 2 GiB、10,000 项、3 层。解压拒绝越界路径、链接和大小写冲突。

目录细节见 [`docs/schema.md`](docs/schema.md)。

## 验证

```powershell
python -m pip install pytest
python -m pytest -q
```

测试覆盖课程归并、网页题面与附件题、题目/提交隔离、HTML/Markdown 链接、跨空间内容去重、SRT-only 导出、私有缓存与旧目录迁移、来源独立刷新、中文 ZIP 解码、安全解压、0 积分获取、完整性检查和凭据隔离。

接口参考 [SJTU-Canvas-Helper](https://github.com/Okabe-Rintarou-0/SJTU-Canvas-Helper) 的课程、网页会话和字幕 LTI 流程，以及 [传承交大](https://share.dyweb.sjtu.cn/) / [选课社区](https://course.sjtu.plus/) 的前端调用。
