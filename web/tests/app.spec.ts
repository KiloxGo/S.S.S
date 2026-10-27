import { test, expect, type Page } from '@playwright/test'

const settings = {
  canvas: { configured: true, managed: false, status: 'ok', message: '已连接' },
  share: { configured: true, managed: false, status: 'ok', message: '已连接' },
  reviews: { configured: true, managed: false, status: 'ok', message: '已连接' },
}
const initialCourse = {
  key: '93540', code: 'EE3325', name: '高电压技术', teachers: ['吴建东'], local: true,
  term: { id: 30, name: '2026-2027 Fall' },
  sync: {
    status: 'partial', finished_at: '2026-10-27T12:00:00Z',
    sources: { canvas: { summary: { files: 14, assignments: 5 } }, subtitles: { summary: { subtitles: 22 } }, reviews: { summary: { reviews: 26 } } },
    errors: [{ scope: 'share', message: 'Archive extraction failed (NeedFirstVolume)' }],
  },
}

async function mockApi(page: Page, options: { empty?: boolean; openFail?: boolean; desktop?: boolean; errors?: {scope: string; message: string}[] } = {}) {
  let running = false
  let submitted: Record<string, string | null> | null = null
  let opened = false
  let empty = !!options.empty
  let nativeCopied = ''
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/desktop/copy')) {
      nativeCopied = route.request().postDataJSON().text
      await route.fulfill({ json: { copied: true } })
      return
    }
    if (path.endsWith('/desktop/open-data')) {
      opened = true
      await route.fulfill({ json: { opened: true } })
      return
    }
    if (path.endsWith('/prompts')) {
      const course = new URL(route.request().url()).searchParams.get('course')
      await route.fulfill({ json: { prompts: [{ id: 'course-guide', title: '课程学习指南', description: '教材、考核、重点与时间投入', text: `${course ? 'EE3325 高电压技术 吴建东 /Users/example/S.S.S/data/高电压技术' : '通用课程'}\n先读 AGENTS.md、README.md、SCHEMA.md。引用字幕文件与时间戳；区分本学期要求、往届经验和待核实项。` }] } })
      return
    }
    if (path.endsWith('/connect')) {
      submitted = route.request().postDataJSON()
      empty = false
    }
    if (path.endsWith('/sync')) {
      running = true
      await route.fulfill({ status: 202, json: { accepted: true } })
      return
    }
    if (path.endsWith('/open')) {
      opened = true
      await route.fulfill(options.openFail ? { status: 503, json: { detail: '无法打开文件管理器，请检查桌面环境。' } } : { json: { opened: true } })
      return
    }
    await route.fulfill({ json: {
      settings: empty ? Object.fromEntries(Object.keys(settings).map(key => [key, { configured: false, managed: false }])) : settings,
      output: '/Users/example/S.S.S/data',
      desktop: options.desktop ? { data_dir: '/Users/example/SSS-Data' } : null,
      courses: empty ? [] : [{ ...initialCourse, sync: { ...initialCourse.sync, ...(options.errors ? { errors: options.errors } : {}), ...(running ? { status: 'running', active_source: 'subtitles', errors: [] } : {}) } }],
    } })
  })
  return { submitted: () => submitted, opened: () => opened, nativeCopied: () => nativeCopied }
}

test('sync errors identify the platform and distinguish course access from expired login', async ({ page }) => {
  await mockApi(page, { errors: [
    { scope: 'canvas/93452/files', message: 'HTTP 401 (credentials missing, expired, or access denied)' },
    { scope: 'share/123', message: 'HTTP 401 (credentials missing, expired, or access denied)' },
    { scope: 'reviews', message: 'HTTP 401 (credentials missing, expired, or access denied)' },
    { scope: 'subtitles/93452/launch', message: 'HTTP 401 (credentials missing, expired, or access denied)' },
  ] })
  await page.goto('/')
  await page.getByText('4 项异常').click()
  const canvas = page.getByRole('listitem').filter({ hasText: 'Canvas · 课程文件' })
  await expect(canvas).toContainText('课程文件列表不可访问（HTTP 401）')
  await expect(canvas).toContainText('文件权限')
  await expect(canvas).not.toContainText('凭据已失效')
  await expect(page.getByRole('listitem').filter({ hasText: '传承交大 · 资料' })).toContainText('更新传承交大的 Token')
  await expect(page.getByRole('listitem').filter({ hasText: '选课社区 · 评价' })).toContainText('更新选课社区的 jcourse_session')
  await expect(page.getByRole('listitem').filter({ hasText: '录播平台登录' })).toContainText('录播会话由 Canvas 自动获取')
})

test('desktop shows its data directory and copies through the native clipboard', async ({ page }) => {
  const api = await mockApi(page, { desktop: true })
  await page.goto('/')
  await page.getByRole('button', { name: '平台配置', exact: true }).click()
  await expect(page.getByText('/Users/example/SSS-Data', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '打开', exact: true }).click()
  await expect.poll(api.opened).toBe(true)
  await page.getByRole('button', { name: '关闭平台配置' }).click()
  await page.getByRole('button', { name: '学习提示词', exact: true }).click()
  await page.getByRole('button', { name: '复制课程学习指南' }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText('已复制')
  expect(api.nativeCopied()).toContain('SCHEMA.md')
})

test('paste raw credentials, load a course, inspect partial result, and open folder', async ({ page }) => {
  const api = await mockApi(page, { empty: true })
  await page.goto('/')
  await expect(page.getByText('从连接 Canvas 开始')).toBeVisible()
  await page.getByRole('button', { name: '平台配置', exact: true }).click()
  await page.getByLabel('Canvas', { exact: true }).fill('fake-canvas-token')
  await page.getByLabel('传承交大', { exact: true }).fill('fake-share-token')
  await page.getByLabel('选课社区', { exact: true }).fill('raw-jcourse-session')
  await page.getByRole('button', { name: '连接并加载课程' }).click()
  await expect(page.getByRole('heading', { name: '高电压技术' })).toBeVisible()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  expect(api.submitted()?.reviews_cookie).toBe('raw-jcourse-session')
  await page.getByRole('button', { name: '平台配置', exact: true }).click()
  await expect(page.getByLabel('选课社区', { exact: true })).toHaveValue('')
  await page.getByRole('button', { name: '关闭平台配置' }).click()
  await page.getByText('1 项异常').click()
  await expect(page.getByText('分卷压缩包缺少首卷，原始压缩包已保留。')).toBeVisible()
  await page.getByRole('button', { name: '打开高电压技术文件夹' }).click()
  await expect(page.getByRole('status')).toContainText('已打开')
  expect(api.opened()).toBe(true)
})

test('sync is disabled while running and the task remains after refresh', async ({ page }) => {
  await mockApi(page)
  await page.goto('/')
  await page.getByRole('button', { name: '同步高电压技术', exact: true }).click()
  await expect(page.getByRole('button', { name: '同步高电压技术', exact: true })).toBeDisabled()
  await expect(page.getByText('录播字幕同步中…')).toBeVisible()
  await page.reload()
  await expect(page.getByRole('button', { name: '同步高电压技术', exact: true })).toBeDisabled()
  await expect(page.getByText('录播字幕同步中…')).toBeVisible()
})

test('clearing a draft credential preserves the saved value', async ({ page }) => {
  const api = await mockApi(page)
  await page.goto('/')
  await page.getByRole('button', { name: '平台配置', exact: true }).click()
  await page.getByLabel('Canvas', { exact: true }).fill('draft-token')
  await page.getByLabel('Canvas', { exact: true }).clear()
  await page.getByLabel('选课社区', { exact: true }).fill('   ')
  await page.getByRole('button', { name: '连接并加载课程' }).click()
  await expect(page.getByRole('status')).toContainText('已加载')
  expect(api.submitted()?.canvas_token).toBeNull()
  expect(api.submitted()?.reviews_cookie).toBeNull()
})

test('search and folder-opening errors have visible feedback', async ({ page }) => {
  await mockApi(page, { openFail: true })
  await page.goto('/')
  await page.getByRole('textbox', { name: '搜索课程' }).fill('不存在的课程')
  await expect(page.getByText('没有找到匹配的课程')).toBeVisible()
  await page.getByRole('textbox', { name: '搜索课程' }).fill('吴建东')
  await page.getByRole('button', { name: '打开高电压技术文件夹' }).click()
  await expect(page.getByRole('alert')).toContainText('无法打开文件管理器')
})

test('mobile page has no horizontal overflow and keeps both course actions', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockApi(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '高电压技术' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await expect(page.getByRole('button', { name: '同步高电压技术', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '打开高电压技术文件夹' })).toBeVisible()
  await page.getByRole('button', { name: '平台配置', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.getByLabel('Canvas', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('home shows only the course list and settings restores keyboard focus', async ({ page }) => {
  await mockApi(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '我的课程', exact: true })).toBeVisible()
  await expect(page.getByText('MY STUDY SPACE')).toHaveCount(0)
  await expect(page.getByText('好好学习，慢慢来。')).toHaveCount(0)
  await expect(page.getByLabel('Canvas', { exact: true })).not.toBeVisible()
  await expect(page.getByText('已同步，有异常')).toBeVisible()
  const settingsButton = page.getByRole('button', { name: '平台配置', exact: true })
  await settingsButton.click()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await expect(settingsButton).toBeFocused()
})

test('study prompts copy course context and can fall back to manual selection', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await mockApi(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '高电压技术' })).toBeVisible()
  await page.getByRole('button', { name: '学习提示词', exact: true }).click()
  await page.getByRole('button', { name: '复制课程学习指南' }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText('已复制')
  const copied = await page.evaluate(() => navigator.clipboard.readText())
  expect(copied).toContain('吴建东')
  expect(copied).toContain('SCHEMA.md')
  await page.getByLabel('用于哪门课').selectOption('')
  await page.getByText('查看完整提示词').click()
  await expect(page.getByRole('textbox', { name: '课程学习指南完整提示词' })).toContainText('通用课程')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).not.toBeVisible()
})
