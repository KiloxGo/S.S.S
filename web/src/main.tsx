import { StrictMode, useEffect, useRef, useState, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import {
  ArrowRight, BookOpen, Check, CheckCircle2, ChevronRight, CircleAlert,
  Eye, EyeOff, FolderOpen, KeyRound, LoaderCircle,
  RotateCw, Search, Settings2, ShieldCheck, X,
} from 'lucide-react'
import './style.css'
import { StudyPrompts } from './StudyPrompts'

type Source = 'canvas' | 'share' | 'reviews'
type Credential = 'canvas_token' | 'share_token' | 'reviews_cookie'
type Settings = Record<Source, { configured: boolean; managed: boolean; status?: string; message?: string }>
type Summary = Record<string, number>
type SyncState = {
  status?: string; active_source?: string; finished_at?: string; files_received?: number;
  sources?: Record<string, { status: string; summary: Summary | null }>;
  errors?: { scope: string; message: string }[];
}
type Course = {
  key: string; code: string; name: string; teachers: string[];
  term: { id: number; name: string }; local: boolean; sync: SyncState;
}
type Snapshot = { courses: Course[]; output: string }
type State = Snapshot & { settings: Settings; desktop?: { data_dir: string } | null }

const platforms: { source: Source; field: Credential; name: string; subtitle: string; hint: string }[] = [
  { source: 'canvas', field: 'canvas_token', name: 'Canvas', subtitle: '课程 · 作业 · 录播字幕', hint: '粘贴 API Token' },
  { source: 'share', field: 'share_token', name: '传承交大', subtitle: '复习资料 · 历年试题', hint: '粘贴 Local Storage 中的 token' },
  { source: 'reviews', field: 'reviews_cookie', name: '选课社区', subtitle: '课程评价 · 教师评价', hint: '粘贴 jcourse_session 原始值' },
]
const stages: Record<string, string> = {
  canvas: '课件与作业', subtitles: '录播字幕', share: '传承资料', reviews: '课程评价',
}
const blankCredentials = (): Record<Credential, string | null> => ({ canvas_token: null, share_token: null, reviews_cookie: null })
const active = (sync: SyncState) => sync.status === 'running' || sync.status === 'queued'

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      ...init, headers: { 'Content-Type': 'application/json', ...init?.headers },
    })
  } catch {
    throw new Error('连接不到本地服务，请确认 S.S.S 仍在运行。')
  }
  if (!response.ok) {
    const data = await response.json().catch(() => null)
    throw new Error(typeof data?.detail === 'string' ? data.detail : '请求失败，请稍后重试。')
  }
  return response.json()
}

function timeLabel(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
}

function summaryLabel(sync: SyncState) {
  if (sync.status === 'queued') return '等待前面的课程完成后开始'
  if (sync.status === 'running') {
    const source = stages[sync.active_source ?? ''] ?? '准备资料'
    return `${source}${sync.files_received ? ` · 已取得 ${sync.files_received} 个文件` : '同步中…'}`
  }
  const parts: string[] = []
  const canvas = sync.sources?.canvas?.summary
  const subtitles = sync.sources?.subtitles?.summary
  const share = sync.sources?.share?.summary
  const reviews = sync.sources?.reviews?.summary
  if (canvas?.files) parts.push(`${canvas.files} 个文件`)
  if (canvas?.assignments) parts.push(`${canvas.assignments} 项作业`)
  if (subtitles?.subtitles) parts.push(`${subtitles.subtitles} 份字幕`)
  if (share?.downloaded) parts.push(`${share.downloaded} 份资料`)
  if (reviews?.reviews) parts.push(`${reviews.reviews} 条评价`)
  return parts.join(' · ') || (sync.status === 'failed' ? '同步中断，可点击右侧按钮重试' : '同步后，资料会保存在本地')
}

function errorSourceLabel(scope: string) {
  const [source, , step] = scope.split('/')
  if (source === 'canvas') {
    if (step === 'files' || step === 'file') return 'Canvas · 课程文件'
    if (step === 'folders') return 'Canvas · 文件目录'
    if (step === 'assignments' || step === 'assignment') return 'Canvas · 作业'
    return 'Canvas'
  }
  if (source === 'subtitles') return step === 'launch' ? 'Canvas · 录播平台登录' : 'Canvas · 录播字幕'
  if (source === 'share') return '传承交大 · 资料'
  if (source === 'reviews') return '选课社区 · 评价'
  return '同步任务'
}

function friendlyError(message: string, scope?: string) {
  if (message.includes('NeedFirstVolume')) return '分卷压缩包缺少首卷，原始压缩包已保留。'
  const status = message.match(/HTTP (401|403)\b/)?.[1]
  if (status && scope) {
    if (/^canvas\/\d+\/files$/.test(scope)) return `课程文件列表不可访问（HTTP ${status}）。可能与本课程文件权限或 Token 授权范围有关，请先在 Canvas 检查“文件”入口。`
    if (scope.startsWith('canvas/')) return `当前内容访问被拒绝（HTTP ${status}）。请检查内容是否开放、账号权限及 Canvas Token；这不一定表示登录已过期。`
    if (scope.startsWith('subtitles/')) return `录播平台访问被拒绝（HTTP ${status}）。请确认本课程录播可访问后重试；录播会话由 Canvas 自动获取。`
    if (status === '401' && scope.split('/')[0] === 'share') return '登录验证未通过（HTTP 401），请在平台配置中更新传承交大的 Token 后重试。'
    if (status === '401' && scope.split('/')[0] === 'reviews') return '登录验证未通过（HTTP 401），请在平台配置中更新选课社区的 jcourse_session 后重试。'
  }
  if (message.includes('HTTP 401')) return '登录验证未通过（HTTP 401），请检查凭据是否有效及是否具备访问权限。'
  if (message.includes('HTTP 403')) return '当前账号无法访问部分内容，或需要重新登录。'
  return message
}

function App() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [desktop, setDesktop] = useState<State['desktop']>(null)
  const [credentials, setCredentials] = useState(blankCredentials)
  const [visible, setVisible] = useState<Partial<Record<Source, boolean>>>({})
  const [settingsOpen, setSettingsOpen] = useState(false)
  const settingsDialog = useRef<HTMLDialogElement>(null)
  const [courses, setCourses] = useState<Course[]>([])
  const [query, setQuery] = useState('')
  const [booting, setBooting] = useState(true)
  const [connecting, setConnecting] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [opening, setOpening] = useState<string | null>(null)
  const busy = courses.some(c => active(c.sync))

  function applySnapshot(data: Snapshot) {
    setCourses(data.courses)
  }

  useEffect(() => {
    const dialog = settingsDialog.current
    if (settingsOpen && dialog && !dialog.open) dialog.showModal()
    if (!settingsOpen && dialog?.open) dialog.close()
  }, [settingsOpen])

  useEffect(() => {
    let disposed = false
    async function boot() {
      try {
        const state = await api<State>('/state')
        if (disposed) return
        setSettings(state.settings)
        setDesktop(state.desktop)
        applySnapshot(state)
        if (!state.courses.length && state.settings.canvas.configured) {
          const data = await api<Snapshot>('/courses')
          if (!disposed) applySnapshot(data)
        }
      } catch (err) {
        if (!disposed) setError((err as Error).message)
      } finally {
        if (!disposed) setBooting(false)
      }
    }
    void boot()
    return () => { disposed = true }
  }, [])

  useEffect(() => {
    if (!busy) return
    let disposed = false
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const data = await api<State>('/state')
        if (!disposed) {
          applySnapshot(data)
          setSettings(data.settings)
        }
      } catch (err) {
        if (!disposed) setError((err as Error).message)
      } finally {
        if (!disposed) timer = setTimeout(poll, 1500)
      }
    }
    timer = setTimeout(poll, 700)
    return () => { disposed = true; clearTimeout(timer) }
  }, [busy])

  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(''), 4500)
    return () => clearTimeout(timer)
  }, [notice])

  async function connect(event: FormEvent) {
    event.preventDefault()
    setConnecting(true)
    setError('')
    try {
      const updates = Object.fromEntries(Object.entries(credentials).map(([key, value]) => [key, value?.trim() || null]))
      const data = await api<State>('/connect', { method: 'POST', body: JSON.stringify(updates) })
      setSettings(data.settings)
      applySnapshot(data)
      setCredentials(blankCredentials())
      if (data.settings.canvas.status === 'ok') {
        setNotice(`已加载 ${data.courses.length} 门课程`)
        if (!platforms.some(p => data.settings[p.source].status === 'error')) setSettingsOpen(false)
      } else {
        setError(data.settings.canvas.message || '请填写有效的 Canvas Token 后加载课程。')
      }
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setConnecting(false)
    }
  }

  async function syncCourse(course: Course) {
    setError('')
    let accepted = false
    setCourses(current => current.map(c => c.key === course.key ? { ...c, sync: { ...c.sync, status: 'queued' } } : c))
    try {
      await api(`/courses/${course.key}/sync`, { method: 'POST' })
      accepted = true
      const data = await api<State>('/state')
      applySnapshot(data)
      setNotice(`已开始同步「${course.name}」`)
    } catch (err) {
      setError((err as Error).message)
      if (!accepted) setCourses(current => current.map(c => c.key === course.key ? { ...c, sync: course.sync } : c))
    }
  }

  async function openCourse(course: Course) {
    setOpening(course.key)
    setError('')
    try {
      await api(`/courses/${course.key}/open`, { method: 'POST' })
      setNotice(`已打开「${course.name}」的资料文件夹`)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setOpening(null)
    }
  }

  const filtered = courses.filter(c => `${c.name} ${c.code} ${c.teachers.join(' ')}`.toLowerCase().includes(query.toLowerCase().trim()))
  const configured = settings ? platforms.filter(p => settings[p.source].configured).length : 0
  const term = courses[0]?.term.name
  const statuses: Record<string, string> = {
    complete: '已同步', partial: '已同步，有异常', failed: '同步失败', queued: '等待同步', running: '同步中',
  }
  const errorBanner = error && <div className="error-banner" role="alert"><CircleAlert size={17} /><span>{error}</span><button aria-label="关闭提示" onClick={() => setError('')}><X size={16} /></button></div>

  return (
    <div className="app-shell">
      <main>
        {!settingsOpen && errorBanner}

        <section className="courses-section" aria-labelledby="courses-title" aria-busy={booting || connecting}>
          <div className="courses-heading">
            <div><div className="course-title-line"><h1 id="courses-title">我的课程</h1><span className="count-label">{courses.length}</span>{busy && <span className="live-label"><span className="status-dot" />同步进行中</span>}</div>
              <p className="term-label">{term || '当前学期'}</p>
            </div>
            <div className="course-toolbar">
              {courses.length > 0 && <div className="search-wrap"><Search size={15} /><input aria-label="搜索课程" placeholder="搜索课程或教师" value={query} onChange={event => setQuery(event.target.value)} /></div>}
              <StudyPrompts courses={courses} nativeClipboard={!!desktop} />
              <button type="button" className="settings-button" onClick={() => setSettingsOpen(true)}><Settings2 size={16} />平台配置</button>
            </div>
          </div>

          <div className="course-list">
            {booting ? <div className="empty-state"><LoaderCircle size={27} className="spin" /><h3>正在加载课程…</h3><p>读取本机配置与当前学期课程</p></div> : courses.length === 0 ? (
              <div className="empty-state"><div className="empty-icon"><BookOpen size={28} strokeWidth={1.5} /></div><h3>{settings?.canvas.status === 'ok' ? '当前学期暂无课程' : '从连接 Canvas 开始'}</h3><p>{settings?.canvas.status === 'ok' ? '当前账号没有可访问的本学期课程。' : '点击右上角「平台配置」，填写凭据并加载课程。'}</p><span className="empty-note">传承交大与选课社区可稍后补充</span></div>
            ) : filtered.length === 0 ? <div className="empty-state search-empty"><Search size={25} /><h3>没有找到匹配的课程</h3><p>试试课程名称、课程号或教师姓名。</p></div> : (
              <>
                <div className="list-labels"><span>课程 / 教师</span><span>同步状态</span><span>操作</span></div>
                {filtered.map(course => {
                  const state = course.sync.status || 'idle'
                  const syncing = active(course.sync)
                  const errors = course.sync.errors ?? []
                  return (
                    <article className={`course-row ${syncing ? 'is-syncing' : ''}`} key={course.key}>
                      <div className="course-main">
                        <span className="course-icon"><BookOpen size={20} strokeWidth={1.5} /></span>
                        <div className="course-info"><h3>{course.name}</h3><p><span className="course-code">{course.code}</span>{course.teachers.join('、') || '教师未标注'}</p></div>
                      </div>
                      <div className="sync-info">
                        <span className={`sync-state ${state}`} title={state === 'partial' ? '同步已结束，部分内容下载或处理失败，可展开异常查看原因。' : undefined}>
                          {state === 'complete' ? <CheckCircle2 size={13} /> : state === 'partial' || state === 'failed' ? <CircleAlert size={13} /> : syncing ? <LoaderCircle size={13} className="spin" /> : <span className="idle-dot" />}
                          {statuses[state] || '尚未同步'}
                        </span>
                        {course.sync.finished_at && !syncing && <span className="sync-date">{timeLabel(course.sync.finished_at)}</span>}
                      </div>
                      <div className="course-actions">
                        <button className={`icon-button sync-button ${syncing ? 'active' : ''}`} aria-label={`同步${course.name}`} title={syncing ? '同步任务进行中' : '同步全部已配置来源，更新字幕与文件'} disabled={syncing || connecting}
                          onClick={() => void syncCourse(course)}><RotateCw size={18} strokeWidth={1.8} className={syncing ? 'spin' : ''} /></button>
                        <button className="icon-button" aria-label={`打开${course.name}文件夹`} title={course.local ? '打开本地资料文件夹' : '首次同步完成后可打开文件夹'} disabled={!course.local || opening === course.key}
                          onClick={() => void openCourse(course)}>{opening === course.key ? <LoaderCircle size={18} className="spin" /> : <FolderOpen size={18} strokeWidth={1.8} />}</button>
                      </div>
                      <div className="course-detail"><span>{summaryLabel(course.sync)}</span>
                        {errors.length > 0 && !syncing && <details><summary><ChevronRight size={12} />{errors.length} 项异常</summary><ul>{errors.map((item, i) => <li key={`${item.scope}-${i}`}><strong>{errorSourceLabel(item.scope)}：</strong>{friendlyError(item.message, item.scope)}</li>)}</ul></details>}
                      </div>
                    </article>
                  )
                })}
              </>
            )}
          </div>
        </section>
      </main>

      <dialog className="settings-dialog" ref={settingsDialog} aria-labelledby="connection-title"
        onCancel={event => { if (connecting) event.preventDefault(); else setSettingsOpen(false) }}
        onClose={() => setSettingsOpen(false)}
        onClick={event => { if (event.target === event.currentTarget && !connecting) setSettingsOpen(false) }}>
        <section className="connection-panel">
          <div className="section-heading">
            <div className="section-title"><KeyRound size={17} /><h2 id="connection-title">平台配置</h2></div>
            <div className="dialog-heading-actions"><span className="quiet-label">{configured ? `${configured} / 3 已配置` : '填写平台凭据'}</span><button type="button" className="dialog-close" aria-label="关闭平台配置" disabled={connecting} onClick={() => setSettingsOpen(false)}><X size={17} /></button></div>
          </div>
          {settingsOpen && errorBanner}
          <form onSubmit={connect}>
            <div className="platform-grid">
              {platforms.map(platform => {
                const state = settings?.[platform.source]
                const status = state?.status === 'ok' ? 'ok' : state?.status === 'error' ? 'error' : state?.configured ? 'saved' : 'missing'
                return (
                  <div className="platform" key={platform.source}>
                    <div className="platform-heading">
                      <label htmlFor={platform.field}>{platform.name}</label>
                      <span className={`platform-state ${status}`} title={state?.message}>
                        {status === 'ok' ? <><Check size={11} />已连接</> : status === 'error' ? <><CircleAlert size={11} />连接失败</> : status === 'saved' ? '已保存' : '未配置'}
                      </span>
                    </div>
                    <p className="platform-description">{platform.subtitle}</p>
                    <div className="input-wrap">
                      <input
                        id={platform.field} name={`sss-${platform.field}`}
                        type={visible[platform.source] ? 'text' : 'password'}
                        autoComplete="off" spellCheck={false}
                        value={credentials[platform.field] ?? ''}
                        disabled={booting || connecting || state?.managed}
                        placeholder={state?.managed ? '由环境变量提供' : state?.configured ? '已保存 · 留空保持当前凭据' : platform.hint}
                        onChange={event => setCredentials(current => ({ ...current, [platform.field]: event.target.value }))}
                      />
                      <button type="button" className="reveal-button" aria-label={`${visible[platform.source] ? '隐藏' : '显示'}${platform.name}凭据`}
                        aria-pressed={!!visible[platform.source]} onClick={() => setVisible(current => ({ ...current, [platform.source]: !current[platform.source] }))}>
                        {visible[platform.source] ? <EyeOff size={15} /> : <Eye size={15} />}
                      </button>
                    </div>
                    {status === 'error' && <p className="field-error">{friendlyError(state?.message || '请检查凭据后重试')}</p>}
                  </div>
                )
              })}
            </div>
            <div className="connection-footer">
              <p><ShieldCheck size={14} /><span>凭据仅保存在本机，已保存的内容不会回显。</span></p>
              <button className="connect-button" disabled={booting || connecting} type="submit">
                {connecting ? <><LoaderCircle className="spin" size={15} />连接中…</> : <><span>连接并加载课程</span><ArrowRight size={15} /></>}
              </button>
            </div>
            {desktop && <div className="desktop-storage"><div><span>数据目录</span><p>{desktop.data_dir}</p></div><button type="button" className="settings-button" onClick={() => void api('/desktop/open-data', { method: 'POST' }).catch(err => setError((err as Error).message))}><FolderOpen size={14} />打开</button></div>}
          </form>
        </section>
      </dialog>
      <div className={`toast ${notice ? 'visible' : ''}`} role="status" aria-live="polite">{notice && <><CheckCircle2 size={16} /><span>{notice}</span></>}</div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
