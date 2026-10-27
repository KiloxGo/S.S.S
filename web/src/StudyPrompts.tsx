import { useEffect, useRef, useState } from 'react'
import { Check, Copy, LoaderCircle, MessageSquareText, X } from 'lucide-react'

type Course = { key: string; code: string; name: string; local: boolean }
type Prompt = { id: string; title: string; description: string; text: string }

export function StudyPrompts({ courses, nativeClipboard = false }: { courses: Course[]; nativeClipboard?: boolean }) {
  const [open, setOpen] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const [courseKey, setCourseKey] = useState('')
  const [prompts, setPrompts] = useState<Prompt[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState('')

  useEffect(() => {
    if (open && dialog.current && !dialog.current.open) dialog.current.showModal()
    if (!open && dialog.current?.open) dialog.current.close()
  }, [open])

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    setLoading(true)
    setError('')
    setCopied('')
    setPrompts([])
    async function load() {
      try {
        const response = await fetch(`/api/prompts${courseKey ? `?course=${encodeURIComponent(courseKey)}` : ''}`, { signal: controller.signal })
        if (!response.ok) throw new Error('提示词加载失败，请关闭后重试。')
        const result = await response.json()
        if (!controller.signal.aborted) setPrompts(result.prompts)
      } catch (err) {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : '提示词加载失败。')
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }
    void load()
    return () => controller.abort()
  }, [open, courseKey])

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(''), 2500)
    return () => clearTimeout(timer)
  }, [copied])

  function show() {
    if (!courses.some(c => c.key === courseKey)) setCourseKey(courses.find(c => c.local)?.key ?? courses[0]?.key ?? '')
    setOpen(true)
  }

  async function copy(prompt: Prompt) {
    try {
      if (nativeClipboard) {
        const response = await fetch('/api/desktop/copy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: prompt.text }) })
        if (!response.ok) throw new Error('Clipboard unavailable')
      } else {
        await navigator.clipboard.writeText(prompt.text)
      }
      setError('')
      setCopied(prompt.id)
    } catch {
      setError('无法自动复制，请展开完整提示词后手动复制。')
    }
  }

  const selected = courses.find(c => c.key === courseKey)
  return <>
    <button type="button" className="settings-button" onClick={show}><MessageSquareText size={16} />学习提示词</button>
    <dialog className="settings-dialog prompts-dialog" ref={dialog} aria-labelledby="prompts-title"
      onCancel={() => setOpen(false)} onClose={() => setOpen(false)}
      onClick={event => { if (event.target === event.currentTarget) setOpen(false) }}>
      <div className="section-heading"><div className="section-title"><MessageSquareText size={17} /><h2 id="prompts-title">学习提示词</h2></div><button type="button" className="dialog-close" aria-label="关闭学习提示词" onClick={() => setOpen(false)}><X size={17} /></button></div>
      <div className="prompts-content">
        <label className="prompt-course-label" htmlFor="prompt-course">用于哪门课</label>
        <select id="prompt-course" value={courseKey} onChange={event => setCourseKey(event.target.value)}>
          <option value="">通用模板</option>
          {courses.map(course => <option key={course.key} value={course.key}>{course.code} · {course.name}</option>)}
        </select>
        <p className="prompt-hint">在 AI 中打开或附上课程文件夹，再复制所需提示词。{selected && '复制内容已包含课程、教师、学期和本地目录。'}</p>
        {selected && !selected.local && <p className="prompt-hint">这门课尚无本地目录，请先同步资料。</p>}
        {error && <p role="alert" className="field-error">{error}</p>}
        {loading && <p className="prompt-loading"><LoaderCircle size={16} className="spin" />加载提示词…</p>}
        <div className="prompt-list">
          {prompts.map(prompt => <section className="prompt-item" key={`${courseKey}-${prompt.id}`}>
            <div className="prompt-item-heading"><div><h3>{prompt.title}</h3><p>{prompt.description}</p></div>
              <button className="settings-button" type="button" aria-label={`复制${prompt.title}`} onClick={() => void copy(prompt)}>{copied === prompt.id ? <Check size={14} /> : <Copy size={14} />}{copied === prompt.id ? '已复制' : '复制'}</button>
            </div>
            <details><summary>查看完整提示词</summary><textarea aria-label={`${prompt.title}完整提示词`} readOnly value={prompt.text} onFocus={event => event.currentTarget.select()} /></details>
          </section>)}
        </div>
        <p className="prompt-hint" role="status">{copied ? '提示词已复制，可粘贴到 AI 对话中。' : '长期学习约定见课程目录的 AGENTS.md / CLAUDE.md，目录说明见 SCHEMA.md。'}</p>
      </div>
    </dialog>
  </>
}
