# S.S.S

上海交通大学复习资料采集工具。Python 命令行后端，先把 Canvas、传承交大、选课社区的资料收集流程跑通。

## 能做什么

- 根据 Canvas 学期起止日期列出本学期的学生与旁听课程；相同课程号、相同学期的多个 Canvas 空间合并为一个课程目录。
- 下载有权限访问的 Canvas 文件，保留文件夹层级；保存全部可见作业的元数据、HTML/纯文本描述、描述中的 Canvas 附件以及本人提交的附件。
- 使用 Canvas Token 换取网页会话，完成录播平台 LTI 登录，遍历录播并导出字幕 JSON、SRT、TXT。没有生成字幕的录播单独记录，不会伪造字幕。
- 传承交大按课程及其旧版关联遍历所有教师和未归档资料；仅获取明确为 **0 积分** 的资料，需要时自动执行免费购买。收费、价格未知、已屏蔽资料跳过并统计。
- 选课社区下载所有匹配课程条目的全部评价分页，按本班教师、其他教师、教师未知分别输出 JSON 和 Markdown；跨学期历史评价一并保留。
- 自动解压 ZIP、TAR、TAR.GZ/BZ2/XZ、7Z、RAR，并递归处理嵌套压缩包。原包保留；拒绝越界路径、链接、特殊文件和大小写冲突，限制总解压大小、数量和层数。
- 下载采用临时文件和 SHA-256 校验。重跑跳过校验通过且更新时间未变的文件；每个来源、每个失败项写入 `manifest.json`，一处失败后继续其他来源。

## 安装

Python 3.11.8 或更新版本。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

以下示例使用 `python -m sss`，请先激活虚拟环境，或将 `python` 替换为 `.\.venv\Scripts\python`。安装后也有 `sss` 命令。

RAR 的压缩文件通常还需要 `unrar` 或 `unar`，放入 PATH；缺少解码器时保留原包并报告提取失败。ZIP、TAR、7Z 无需外部命令。加密包和分卷包不会猜测密码或自动拼接，失败会记录在清单中。

## 凭据

复制 `credentials.example.json` 为 `.local/credentials.json`，在本地填入：

```json
{
  "canvas_token": "",
  "share_token": "",
  "reviews_cookie": "jcourse_session=..."
}
```

- Canvas：在 `oc.sjtu.edu.cn` 的账户设置生成 Token。
- 传承交大：登录 `share.dyweb.sjtu.cn` 后，在开发者工具 Application → Local Storage 读取 `token`。当前 API 使用 `Auth` 请求头。Google 域 Cookie 不能登录此站。
- 选课社区：只需 `course.sjtu.plus` 的 `jcourse_session`；可以附带同域 `csrftoken`、`acw_tc`、`cdn_sec_tc`。其他域 Cookie 不会使用。

也可通过 `SSS_CANVAS_TOKEN`、`SSS_SHARE_TOKEN`、`SSS_REVIEWS_COOKIE` 环境变量覆盖文件；`--config` 必须放在子命令之前：

```powershell
python -m sss --config .local/credentials.json doctor
```

`.local/`、`.env*`、`data/`、虚拟环境已被 Git 忽略。凭据文件是本机明文文件，请保存在自己控制的目录。日志和导出的元数据不保留凭据、签名 URL 参数或 LTI 会话；下载的原始学习资料保持原样。程序只在对应 API 来源发送认证头，跨来源重定向会剥离认证头。

## 从课程到完整目录

```powershell
# 默认列出本学期。--all 列出全部学期；--term 30 可指定学期。
python -m sss courses

# 课程号或任一 Canvas ID 均可选择整个合并组。
python -m sss plan --course EE3316 --out .local/EE3316.json

# 核对/编辑映射后，收集全部四个来源。
python -m sss sync --plan .local/EE3316.json

# 也可以不保存映射直接收集，或只选择部分来源。
python -m sss sync --course EE3316 --sources canvas,subtitles,reviews
python -m sss sync --plan .local/EE3316.json --output D:\Study
```

课程映射格式见 [`examples/course.json`](examples/course.json)。

`canvas_ids` 是要合并的空间，`teachers` 是本班教师名字，默认只取学生身份空间的教师并集；旁听空间一同下载，但其教师不会自动算作本班教师。`canvas_spaces` 保留每个空间的身份及教师，可据此核对或调整 `teachers`。`aliases` 用于历史课程号，`share_ids` / `reviews_ids` 可明确指定社区课程条目。跨站匹配以课程号为依据，不会凭模糊名称自动合并。评价共同授课的教师组中只要包含本班教师，就归入本班教师；没有可靠教师信息归入 `unknown-teachers`。

`plan` 同时按课程名称检索选课社区，把其他课程号写入 `review_candidates`，供核对后填入 `aliases`。例如已核实 EE3316 的历史参考评价在 **EE3310「电气工程基础（1）」** 下；需要这些评价时在映射中填写 `"aliases": ["EE3310"]`。同名的 ECE3600J、或 EE3311「电气工程基础（2）」不应未经核对自动加入。

已有映射不会被 `plan` 覆盖。未配置某来源时可以先用 `--sources` 跑通其他来源；全来源执行时缺少凭据会明确标为 `partial`。退出码 `0` 表示所选来源完整完成，`2` 表示有失败项或配置错误。

## 目录约定

详见 [`docs/schema.md`](docs/schema.md)。示意：

```text
data/
  2026-2027 Fall/
    EE3316-电气工程基础/
      course.json
      manifest.json
      canvas/
        93532/
          folders.json
          files.json
          files/<文件夹ID-名称>/<文件ID-名称>
          assignments/<作业ID-名称>/
            assignment.json
            description.html
            README.md
            attachments/
          subtitles/
            recordings.json
            <录播ID-时间>.json
            <录播ID-时间>.srt
            <录播ID-时间>.txt
        93538/...
      share/
        courses.json
        <课程ID>-materials.json
        <教学班ID-教师>/<资料ID>/<原文件名>
      reviews/
        courses.json
        own-teachers/
        other-teachers/
        unknown-teachers/
```

学期作为外层目录，防止同一课程跨学期覆盖。各来源保留远程 ID，避免不同教师或两个 Canvas 空间同名文件互相覆盖。每个下载文件旁有 `.receipt.json`，压缩包旁有 `<文件名>.extracted/`。

## 边界与验证

只采集账号能够访问的内容。未发布文件、过期会话、站点接口变化会记录原因。字幕仅覆盖当前 Canvas 8329 LTI 对接的 `v.sjtu.edu.cn` 录播服务；未实现旧 `courses.sjtu.edu.cn` 录播系统。程序不下载视频本体，不提交作业，仅发起已核实为 0 积分的购买请求。

传承交大的前端购买接口目前没有可核实的原子价格上限参数。客户端在购买前重新读取并核验 0 积分，但无法消除服务器在两次请求之间改价的竞态；严格要求零竞态时应等待服务端提供价格条件购买接口。购买接口不作自动重试。

默认单文件下载上限 4 GiB；每个根压缩包连同嵌套内容共限 2 GiB、10,000 项、3 层。文件更新后若已存在不同版本的解压目录，保留旧目录并报告冲突，请先将旧目录移走再重跑。工具不会删除服务端已撤回的本地文件；本次收集结果以清单为准。请避免同时向同一个课程目录运行两个同步进程。

测试：

```powershell
python -m pip install pytest
python -m pytest -q
```

测试覆盖课程归并/学期筛选、真实 ZIP/TAR/7Z 解压、恶意路径/链接/解压限额、0 积分检查、全部教师资料展开、评论分类/分页、下载校验与失败恢复、凭据脱敏及跨域认证头隔离。

接口参考 [`SJTU-Canvas-Helper`](https://github.com/Okabe-Rintarou-0/SJTU-Canvas-Helper) 的课程 API、网页会话和字幕 LTI 流程，以及 [传承交大](https://share.dyweb.sjtu.cn/) / [选课社区](https://course.sjtu.plus/) 当前前端调用。实现为独立 Python 后端，无前端。
