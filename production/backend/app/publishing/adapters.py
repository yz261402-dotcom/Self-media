"""发布能力边界；专用浏览器只操作可见页面，不调用逆向接口。"""
from __future__ import annotations

import atexit
import importlib.util
import os
import queue
import re
import threading
import time
from urllib.parse import urlsplit
from concurrent.futures import Future
from pathlib import Path
from typing import Protocol

CREATOR_URL = 'https://creator.douyin.com/creator-micro/content/upload'


def _diagnostic_fields(values):
    """诊断只允许固定分类与代码位置，不传递异常正文。"""
    if not isinstance(values, dict):
        return {}
    safe = {}
    for key, pattern in {
        'operation': r'(?:locate|open|select|confirm|verify)',
        'cause_type': r'[A-Za-z_][A-Za-z0-9_]{0,79}',
        'api_method': r'(?:Locator|Page)\.(?:count|click|check|is_checked|wait_for|evaluate|evaluate_all|inner_text|is_visible)',
        'reason': r'(?:strict_mode|invalid_selector|xpath|parser|not_attached|pointer_intercepted|timeout|other)',
    }.items():
        value = values.get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            safe[key] = value
    line = values.get('source_line')
    if type(line) is int and 0 < line < 100000:
        safe['source_line'] = line
    return safe


class AdapterError(RuntimeError):
    def __init__(self, code, message, *, retryable=False, state='failed', detail=None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.state = code, message, retryable, state
        self.detail = detail or {}


class PublishAdapter(Protocol):
    name: str
    def login(self, session_dir: Path) -> dict: ...
    def status(self, session_dir: Path) -> dict: ...
    def prepare(self, **kwargs) -> dict: ...
    def submit(self, **kwargs) -> dict: ...


class MockDouyinAdapter:
    name = 'mock'
    is_mock = True
    def login(self, session_dir):
        return self.status(session_dir)
    def status(self, session_dir):
        return {'state': 'logged_in', 'adapter': self.name, 'is_mock': True,
                'account_id': 'mock-account', 'account_label': '模拟账号（不会发布）'}
    def prepare(self, **kwargs):
        return {**self.status(None), 'state': 'ready_to_confirm'}
    def submit(self, **kwargs):
        return {'state': 'submitted', 'provider_ref': 'mock_' + kwargs.get('task_id', ''),
                'url': None, 'is_mock': True}


def caption_text(description: str, tags: list[str]) -> str:
    hashtags = list(dict.fromkeys('#' + str(t).strip().lstrip('#').replace(' ', '')
                                for t in tags if str(t).strip().lstrip('#')))
    return '\n'.join(x for x in (description.strip(), ' '.join(hashtags), '本片人物与声音由 AI 生成') if x)


def _normalize_caption(value: str) -> str:
    """Normalize harmless rich-text formatting changes before comparing captions."""
    text = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\u00a0', ' ')
    text = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    return re.sub(r'\n{2,}', '\n', text).strip()


class DouyinPage:
    """必要控件必须明确且唯一；缺失即停止，不猜测页面的第一个输入框。"""
    TITLE = ('input[placeholder*="作品标题"]', 'input[placeholder*="填写标题"]', 'input[placeholder="请输入标题"]')
    CAPTION = ('[contenteditable="true"][data-placeholder*="简介"]', '[contenteditable="true"][data-placeholder*="描述"]',
               '.zone-container[contenteditable="true"]', '[contenteditable="true"][role="textbox"]', 'textarea[placeholder*="作品描述"]')
    VIDEO = 'input[type="file"][accept*="video"], input[type="file"][accept*="mp4"]'
    IMAGE = 'input[type="file"][accept*="image"], input[type="file"][accept*="jpg"], input[type="file"][accept*="png"]'

    def __init__(self, page, timeout_ms=30000):
        self.page, self.timeout = page, timeout_ms
        self.prepared = None
        self.attempted = set()
        self.phase = 'idle'
        self.network_issues = []
        page.on('requestfailed', lambda request: self._network_issue(request.url, request.failure))
        page.on('response', lambda response: self._network_issue(response.url, 'HTTP ' + str(response.status)) if response.status >= 400 else None)

    def _network_issue(self, url, message):
        self.network_issues.append({'host': urlsplit(url).hostname, 'error': str(message)[:100]})
        self.network_issues = self.network_issues[-15:]

    def _text(self):
        # 投稿正文是不可信内容，不得把它的“扫码登录/抖音号”当作平台账号信息。
        return self.page.locator('body').evaluate('''body => {
            const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
            const text = []; let node;
            while (node = walker.nextNode()) {
                const e = node.parentElement;
                if (!e || e.closest('input, textarea, [contenteditable], form, script, style')) continue;
                const style = getComputedStyle(e);
                if (!e.getClientRects().length || style.visibility === 'hidden' || style.display === 'none') continue;
                if (node.textContent.trim()) text.push(node.textContent.trim());
            }
            return text.join('\\n');
        }''', timeout=self.timeout)

    def inspect_controls(self):
        if '/content/post/video' in self.page.url:
            evidence = Path(__file__).resolve().parents[2] / 'runtime/publish/last-page.png'
            evidence.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(evidence), full_page=True, timeout=15000)
        # 发布页的封面编辑器在不同版本中不一定声明 ARIA dialog。保留有限的
        # 可见结构信息，便于诊断弹层锚点变化；不返回输入值、Cookie 或正文。
        cover_structure = self.page.locator('body').evaluate('''body => {
            const terms = ['设置封面', '选择封面', '竖封面3:4', '横封面4:3', '上传封面',
                '完成', '取消', 'AI封面', '封面检测'];
            const visible = e => {
                const s = getComputedStyle(e);
                return !!e.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
            };
            return [...body.querySelectorAll('*')].filter(e => visible(e)).map(e => {
                const text = (e.childElementCount === 0 ? e.textContent : '').trim();
                const hit = text && terms.some(term => text === term || text.includes(term));
                const role = e.getAttribute('role');
                const cls = typeof e.className === 'string' ? e.className : '';
                const modalish = role === 'dialog' || /modal|dialog|drawer|portal|cover/i.test(cls);
                if (!hit && !modalish) return null;
                return {tag: e.tagName, role, class_name: cls.slice(0, 160),
                    text: text.slice(0, 80), modalish};
            }).filter(Boolean).slice(-80);
        }''', timeout=self.timeout)
        return {'url': self.page.url.split('?')[0], 'title': self.page.title(),
                'phase': self.phase, 'network_issues': self.network_issues, 'interface_text': self._text()[:6000],
                'inputs': self.page.locator('input:not([type="hidden"]):not([type="password"]), textarea').evaluate_all(
                    "items => items.map(e => ({type:e.type,placeholder:e.placeholder,accept:e.accept||'',aria_label:e.getAttribute('aria-label')||'',class_name:e.className})).slice(0,40)"),
                'editors': self.page.locator('[contenteditable="true"]').evaluate_all(
                    "items => items.map(e => ({tag:e.tagName,role:e.getAttribute('role'),placeholder:e.getAttribute('data-placeholder'),class_name:e.className})).slice(0,20)"),
                'buttons': self.page.get_by_role('button').filter(visible=True).all_text_contents()[:50],
                 'dialogs': self.page.get_by_role('dialog').filter(visible=True).all_text_contents()[:5],
                 'cover_structure': cover_structure,
                'image_controls': self.page.locator('img').evaluate_all(
                    "items=>items.slice(0,20).map(e=>({alt:e.alt,class_name:e.className,parent_class:e.parentElement.className}))")}

    def _human_check(self):
        body = self._text()
        if re.search(r'请完成.{0,8}验证|拖动滑块|安全验证|点击.{0,8}验证码', body):
            raise AdapterError('DOUYIN_MANUAL_VERIFICATION_REQUIRED', '请在专用浏览器中完成安全验证，再继续准备。', state='awaiting_login', retryable=True)
        if re.search(r'扫码登录|扫描二维码登录|登录后使用|手机号登录', body):
            raise AdapterError('DOUYIN_LOGIN_REQUIRED', '请在已打开的专用浏览器中扫码登录抖音。', state='awaiting_login', retryable=True)

    def identity(self, *, allow_home=True):
        self._human_check()
        matches = set(re.findall(r'抖音号\s*[:：]\s*([A-Za-z0-9_.-]+)', self._text()))
        if len(matches) == 1:
            account_id = matches.pop()
            return {'account_id': account_id, 'account_label': '抖音号：' + account_id}
        links = self.page.locator('a[href*="douyin.com/user/"]')
        for index in range(links.count()):
            link = links.nth(index)
            if not link.is_visible():
                continue
            name, href = link.inner_text().strip(), link.get_attribute('href') or ''
            if name and re.match(r'^https://(?:www\.)?douyin\.com/user/[A-Za-z0-9_-]+(?:\?|$)', href):
                return {'account_id': href.split('?')[0], 'account_label': name}
        # 上传页通常只露出头像；同一专用会话的首页显示本人账号。只读新标签，不离开待发表单。
        if allow_home and self.page.url.startswith('https://creator.douyin.com/'):
            account_page = self.page.context.new_page()
            try:
                account_page.goto('https://creator.douyin.com/creator-micro/home', wait_until='domcontentloaded', timeout=self.timeout)
                account_page.get_by_text(re.compile(r'抖音号')).first.wait_for(state='visible', timeout=self.timeout)
                return DouyinPage(account_page, self.timeout).identity(allow_home=False)
            except AdapterError:
                raise
            except Exception:
                pass
            finally:
                account_page.close()
        raise AdapterError('DOUYIN_ACCOUNT_UNVERIFIED', '未能读取当前抖音账号，请在专用浏览器展开账号资料，显示抖音号后重试。', state='awaiting_login', retryable=True)

    def _field(self, selectors, code):
        for selector in selectors:
            visible = self.page.locator(selector).filter(visible=True)
            if visible.count() == 1:
                return visible
        raise AdapterError(code, '当前页面的必要控件未识别，请在专用浏览器检查页面后重试。', retryable=True)

    def prepare(self, *, task_id, video_path, cover_path, title, description, tags, scheduled_at=None):
        if scheduled_at:
            raise AdapterError('SCHEDULE_NOT_SUPPORTED', '首版只支持立即投稿，定时发布尚未接入。')
        self.prepared = None
        self.phase = 'checking_account'
        if task_id in self.attempted:
            raise AdapterError('DOUYIN_ALREADY_ATTEMPTED', '该任务已执行过发布点击，请先核验结果。', state='needs_verification')
        self._human_check()
        identity = self.identity()
        if not Path(video_path).is_file() or not Path(cover_path).is_file():
            raise AdapterError('PUBLISH_MEDIA_MISSING', '视频或封面文件不存在。')
        title = str(title).strip()
        caption = caption_text(description, tags)
        if not title or len(title) > 30 or len(caption) > 1000:
            raise AdapterError('DOUYIN_TEXT_LIMIT', '请将标题控制在 1–30 字，简介、话题及 AI 声明合计不超过 1000 字。')
        uploads = self.page.locator(self.VIDEO)
        if uploads.count() != 1:
            raise AdapterError('DOUYIN_UPLOAD_NOT_FOUND', '未找到唯一视频上传控件，请登录后进入视频发布页。', retryable=True)
        self.phase = 'uploading_video'
        uploads.set_input_files(str(video_path), timeout=self.timeout)
        self._wait_uploaded()
        self._human_check()
        self.phase = 'filling_caption'
        self._field(self.TITLE, 'DOUYIN_TITLE_NOT_FOUND').fill(title, timeout=self.timeout)
        self._field(self.CAPTION, 'DOUYIN_CAPTION_NOT_FOUND').fill(caption, timeout=self.timeout)
        self.phase = 'setting_cover'
        self._set_cover(cover_path)
        self.phase = 'setting_declaration'
        self._set_ai_label()
        self._upload_error()
        button = self.page.get_by_role('button', name='发布', exact=True)
        if button.count() != 1 or not button.is_enabled():
            raise AdapterError('DOUYIN_FORM_NOT_READY', '发布表单尚未就绪，请在浏览器补齐页面提示的项目。', retryable=True)
        self.prepared = {'task_id': task_id, **identity, 'title': title, 'caption': caption}
        self.phase = 'ready_to_confirm'
        return {'state': 'ready_to_confirm', **identity, 'is_mock': False, 'message': '视频和封面已上传，文案已填写。请检查浏览器中的内容后确认发布。'}

    def _upload_error(self):
        failure = self.page.get_by_text(re.compile(r'上传失败|上传出错|视频格式不支持|视频上传错误')).filter(visible=True)
        if failure.count():
            raise AdapterError('DOUYIN_UPLOAD_FAILED', '抖音页面显示视频上传失败，请检查网络或平台提示后再继续准备。', retryable=True)

    def _wait_uploaded(self):
        deadline = time.monotonic() + max(self.timeout, int(os.getenv('AVATAR_STUDIO_DOUYIN_UPLOAD_TIMEOUT_MS', '300000'))) / 1000
        while time.monotonic() < deadline:
            self._upload_error()
            uploaded = self.page.get_by_text(re.compile(r'^(上传成功|重新上传|更换视频)$')).filter(visible=True)
            # 只识别整行进度，避免固定提醒“如作品还在上传中”阻塞已完成的视频。
            visible_text = self._text()
            progressing = any(re.fullmatch(
                r'(?:视频)?(?:上传中|正在上传)(?:\s*(?:…+|\.{3}|\d+(?:\.\d+)?\s*%))?'
                r'|上传进度\s*[:：]?\s*\d+(?:\.\d+)?\s*%', line.strip())
                for line in visible_text.splitlines())
            if uploaded.count() and not progressing:
                return
            self.page.wait_for_timeout(500)
        raise AdapterError('DOUYIN_UPLOAD_TIMEOUT', '尚未确认视频上传完成，请检查专用浏览器的上传状态。', retryable=True)

    def _cover_input(self):
        inputs = self.page.locator(self.IMAGE)
        if inputs.count() == 1:
            return [inputs]  # 原生 file 控件可隐藏；页面唯一时不需要猜测目标。
        return [inputs.nth(i) for i in range(inputs.count())
                if inputs.nth(i).evaluate('(e)=>e.parentElement.checkVisibility({checkVisibilityCSS:true})')]

    def _modern_cover_cards(self):
        labels = {kind: self.page.get_by_text(text, exact=True).filter(visible=True)
                  for kind, text in (('portrait', '竖封面3:4'), ('landscape', '横封面4:3'))}
        if not any(label.count() for label in labels.values()):
            return None
        cards = {}
        for kind, label in labels.items():
            if label.count() != 1:
                raise AdapterError('DOUYIN_COVER_REQUIRED', '未找到唯一的横竖封面卡片。', retryable=True,
                                   detail={'operation': 'locate_cards'})
            card = label.locator('xpath=ancestor::*[.//img][1]')
            if card.count() != 1:
                raise AdapterError('DOUYIN_COVER_REQUIRED', '未找到封面卡片的图片区域。', retryable=True,
                                   detail={'operation': 'locate_cards'})
            cards[kind] = card
        return cards

    def _set_modern_cover(self, cover_path, cards):
        import hashlib
        from .cover_assets import prepare_cover_assets

        operation = 'prepare_assets'
        messages = {'prepare_assets': '横竖封面图片准备失败。', 'open_editor': '双比例封面编辑器打开失败。',
                    'locate_upload': '未找到封面编辑器内唯一的上传封面入口。',
                    'locate_preview': '未找到封面编辑器内唯一的主画布。',
                    'upload_portrait': '竖封面文件导入失败。', 'upload_landscape': '横封面文件导入失败。',
                    'verify_portrait_preview': '竖封面导入后主画布未更新。',
                    'verify_landscape_preview': '横封面导入后主画布未更新。',
                    'switch_landscape': '封面编辑器未切换到横封面。',
                    'complete_editor': '双比例封面保存操作失败。',
                    'verify_cards': '未确认两张新封面均已应用到投稿表单。'}

        def required():
            raise AdapterError('DOUYIN_COVER_REQUIRED', messages[operation], retryable=True,
                               detail={'operation': operation})

        def wait_until(predicate):
            deadline = time.monotonic() + self.timeout / 1000
            while time.monotonic() < deadline:
                if predicate():
                    return
                self.page.wait_for_timeout(min(100, max(1, (deadline - time.monotonic()) * 1000)))
            raise AdapterError('DOUYIN_COVER_UNVERIFIED', messages[operation], retryable=True,
                               detail={'operation': operation})

        try:
            assets = prepare_cover_assets(Path(cover_path))
            original_sources = {kind: card.locator('img').evaluate_all(
                'images => images.map(image => image.currentSrc || image.src)') for kind, card in cards.items()}
            def open_editor(card):
                nonlocal operation
                operation = 'open_editor'
                choose = card.get_by_text('选择封面', exact=True).filter(visible=True)
                if choose.count() == 1:
                    choose.click(timeout=self.timeout)
                else:
                    card.click(timeout=self.timeout)

                def editor_root():
                    # 新旧页面有时声明 role=dialog，有时只是普通的遮罩节点。
                    dialog = self.page.get_by_role('dialog').filter(visible=True)
                    if dialog.count() == 1:
                        return dialog
                    hint = self.page.get_by_text('点击上传文件或拖拽文件到这里', exact=True).filter(visible=True)
                    if hint.count() != 1:
                        return None
                    root = hint.locator(
                        "xpath=ancestor::*[.//canvas[contains(@class,'cloudImage-')] "
                        "and .//button[normalize-space(.)='完成']][1]"
                    ).filter(visible=True)
                    return root if root.count() == 1 else None

                # “上传封面”区、主画布和“完成”按钮是编辑器自身的稳定结构，
                # 不依赖抖音是否给遮罩节点补 ARIA dialog 角色。
                scope = None
                deadline = time.monotonic() + self.timeout / 1000
                while time.monotonic() < deadline:
                    scope = editor_root()
                    if scope is not None:
                        return scope
                    self.page.wait_for_timeout(min(100, max(1, (deadline - time.monotonic()) * 1000)))
                raise AdapterError('DOUYIN_COVER_UNVERIFIED', messages[operation], retryable=True,
                                   detail={'operation': operation})

            def upload_current(dialog, kind, asset):
                nonlocal operation
                operation = 'locate_upload'
                hint = dialog.get_by_text('点击上传文件或拖拽文件到这里', exact=True)
                if hint.count() != 1:
                    required()
                upload = hint.locator("xpath=ancestor::*[.//input[@type='file']][1]")
                cover_input = upload.locator('input[type="file"]:not([class*="replace"])')
                if upload.count() != 1 or cover_input.count() != 1:
                    required()
                operation = 'locate_preview'
                canvas = dialog.locator('canvas.lower-canvas[class*="cloudImage-"]').filter(visible=True)
                wait_until(lambda: canvas.count() == 1)

                def canvas_digest():
                    # 指纹只用于当前操作的内存比较；不保存画布内容或原始图片地址。
                    return hashlib.sha256(canvas.evaluate('(element) => element.toDataURL()', timeout=self.timeout).encode()).hexdigest()

                previous = canvas_digest()
                operation = 'upload_' + kind
                cover_input.set_input_files(str(asset), timeout=self.timeout)
                operation = 'verify_' + kind + '_preview'
                wait_until(lambda: canvas.count() == 1 and canvas_digest() != previous)

            def complete_editor(dialog):
                nonlocal operation
                operation = 'complete_editor'
                done = dialog.get_by_role('button', name='完成', exact=True)
                if done.count() != 1:
                    required()
                done.click(timeout=self.timeout)
                operation = 'verify_dialog'
                wait_until(lambda: dialog.count() == 0 or not dialog.is_visible())

            first_dialog = open_editor(cards['portrait'])
            switch = first_dialog.get_by_role('button', name='设置横封面', exact=True)
            if switch.count() == 1:
                # 兼容旧版单弹窗编辑器：在同一个弹窗内切换横版。
                upload_current(first_dialog, 'portrait', assets['portrait'])
                operation = 'switch_landscape'
                switch.click(timeout=self.timeout)
                wait_until(lambda: first_dialog.count() == 1 and first_dialog.is_visible())
                upload_current(first_dialog, 'landscape', assets['landscape'])
                complete_editor(first_dialog)
            else:
                # 当前抖音页面为两个独立弹窗：分别点击竖、横封面卡片完成上传。
                upload_current(first_dialog, 'portrait', assets['portrait'])
                complete_editor(first_dialog)
                second_dialog = open_editor(cards['landscape'])
                upload_current(second_dialog, 'landscape', assets['landscape'])
                complete_editor(second_dialog)

            operation = 'verify_cards'

            def cards_applied():
                for kind, card in cards.items():
                    if card.get_by_text('选择封面', exact=True).filter(visible=True).count():
                        return False
                    images = card.locator('img').filter(visible=True)
                    if images.count() != 1:
                        return False
                    current = images.evaluate('''image => ({source:image.currentSrc || image.src,
                        loaded:image.complete && image.naturalWidth > 0 && image.naturalHeight > 0,
                        filtered:[...image.parentElement.classList].some(name => name.startsWith('filter-'))})''')
                    if not current['loaded'] or current['filtered'] or current['source'] in original_sources[kind]:
                        return False
                return True

            wait_until(cards_applied)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError('DOUYIN_COVER_FAILED', messages[operation], retryable=True,
                               detail={'operation': operation, 'error_type': type(exc).__name__}) from exc

    def _set_cover(self, cover_path):
        cards = self._modern_cover_cards()
        if cards is not None:
            return self._set_modern_cover(cover_path, cards)
        operation = 'locate'
        messages = {'locate': '封面上传控件识别失败。', 'open': '封面设置界面打开失败。',
                    'upload': '封面文件上传失败。', 'wait_result': '等待封面裁剪界面或保存结果失败。',
                    'inspect_crop': '封面裁剪弹窗结构已变化，请检查浏览器。',
                    'save_crop': '封面裁剪保存操作失败。', 'verify_crop': '封面裁剪保存后未能确认弹窗关闭。',
                    'save_dialog': '封面设置确认操作失败。', 'verify_dialog': '封面设置确认后未能确认弹窗关闭。'}
        try:
            inputs = self._cover_input()
            if not inputs:
                trigger = self.page.get_by_text(re.compile(r'^(设置封面|选择封面|编辑封面)$')).filter(visible=True)
                if trigger.count() != 1:
                    raise AdapterError('DOUYIN_COVER_REQUIRED', '未识别封面设置控件，请在专用浏览器检查后重试。', retryable=True)
                operation = 'open'
                trigger.click(timeout=self.timeout)
                local = self.page.get_by_text(re.compile(r'^(上传封面|本地上传|自定义封面)$'))
                if local.count() == 1:
                    local.click(timeout=self.timeout)
                inputs = self._cover_input()
            if len(inputs) != 1:
                raise AdapterError('DOUYIN_COVER_REQUIRED', '封面上传入口不唯一或尚未出现，请人工检查封面弹窗。', retryable=True)
            saved = self.page.get_by_text(re.compile(r'封面已设置|更换封面|编辑封面')).filter(visible=True)
            had_saved_marker = bool(saved.count())
            operation = 'upload'
            inputs[0].set_input_files(str(cover_path), timeout=self.timeout)
            operation = 'wait_result'
            deadline = time.monotonic() + self.timeout / 1000
            crop = self.page.locator('img[alt="裁剪图预览"]').filter(visible=True)
            dialogs = self.page.get_by_role('dialog').filter(visible=True)
            # 裁剪界面可能在上传数秒后才出现；持续观察全部已知状态，不能提前选定其他分支。
            while time.monotonic() < deadline:
                if crop.count():
                    operation = 'inspect_crop'
                    panel = crop.locator("xpath=ancestor::*[.//button[normalize-space(.)='保存']][1]")
                    save = panel.get_by_role('button', name='保存', exact=True)
                    if crop.count() != 1 or panel.count() != 1 or save.count() != 1 or '设置封面' not in panel.inner_text(timeout=self.timeout):
                        raise AdapterError('DOUYIN_COVER_REQUIRED', messages[operation], retryable=True,
                                           detail={'operation': operation})
                    operation = 'save_crop'
                    save.click(timeout=self.timeout)
                    operation = 'verify_crop'
                    crop.wait_for(state='hidden', timeout=self.timeout)
                    return
                if dialogs.count() == 1:
                    done = dialogs.get_by_role('button', name=re.compile(r'^(完成|确定|保存)$'))
                    if done.count() == 1:
                        operation = 'save_dialog'
                        done.click(timeout=self.timeout)
                        operation = 'verify_dialog'
                        dialogs.wait_for(state='hidden', timeout=self.timeout)
                        return
                if not had_saved_marker and saved.count():
                    return
                self.page.wait_for_timeout(min(100, max(1, (deadline - time.monotonic()) * 1000)))
            raise AdapterError('DOUYIN_COVER_TIMEOUT', '等待封面裁剪界面或保存结果超时。', retryable=True,
                               detail={'operation': operation})
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError('DOUYIN_COVER_FAILED', messages[operation], retryable=True,
                               detail={'operation': operation, 'error_type': type(exc).__name__}) from exc

    def _set_ai_label(self):
        operation = 'locate'
        messages = {'locate': 'AI 声明控件识别失败。', 'open': 'AI 声明打开失败。',
                    'select': 'AI 声明选择失败。', 'confirm': 'AI 声明确认失败。',
                    'verify': 'AI 声明保存结果核验失败。'}

        def required():
            raise AdapterError('DOUYIN_AI_LABEL_REQUIRED', messages[operation], retryable=True,
                               detail={'operation': operation})

        try:
            heading = self.page.get_by_text('对作品内容添加声明', exact=True).filter(visible=True)
            if heading.count() > 1:
                required()
            if not heading.count():
                option = self.page.get_by_role('checkbox', name=re.compile(r'AI生成|AI 生成|人工智能生成')).filter(visible=True)
                if option.count() == 1:
                    operation = 'select'
                    option.check(timeout=self.timeout)
                    operation = 'verify'
                    if not option.is_checked():
                        required()
                    return
                trigger = self.page.get_by_text('请选择自主声明', exact=True).filter(visible=True)
                if trigger.count() == 1:
                    operation = 'open'
                    trigger.click(timeout=self.timeout)
                    self.page.get_by_text('内容由AI生成', exact=True).filter(visible=True).first.wait_for(state='visible', timeout=self.timeout)
                elif self.page.get_by_text('内容由AI生成', exact=True).filter(visible=True).count() == 1:
                    return  # 已保存的主表单值；预览里的“作者声明：...”不作为此证据。
                else:
                    required()

            scope = self.page
            if heading.count() == 1:
                panel = heading.locator("xpath=ancestor::*[.//button[normalize-space(.)='确定']][1]")
                if panel.count() != 1:
                    required()
                scope = panel
            operation = 'select'
            radio = scope.get_by_role('radio', name='内容由AI生成', exact=True).filter(visible=True)
            if radio.count() == 1:
                radio.click(timeout=self.timeout)
            else:
                label = scope.get_by_text('内容由AI生成', exact=True).filter(visible=True)
                if label.count() != 1:
                    required()
                control = label.locator("xpath=ancestor-or-self::*[self::label or @role='radio'][1]").filter(visible=True)
                if control.count() == 1:
                    control.click(timeout=self.timeout)
                else:
                    option = scope.get_by_role('option', name='内容由AI生成', exact=True).filter(visible=True)
                    if option.count() != 1:
                        required()
                    option.click(timeout=self.timeout)
            if scope is not self.page:
                operation = 'confirm'
                confirm = panel.get_by_role('button', name='确定', exact=True)
                if confirm.count() != 1:
                    required()
                confirm.click(timeout=self.timeout)
                heading.wait_for(state='hidden', timeout=self.timeout)
            operation = 'verify'
            saved = self.page.get_by_text('内容由AI生成', exact=True).filter(visible=True)
            saved.first.wait_for(state='visible', timeout=self.timeout)
            if saved.count() != 1 or heading.count():
                required()
        except AdapterError:
            raise
        except Exception as exc:
            text = str(exc)
            reason = 'other'
            for category, pattern in (
                ('strict_mode', r'strict mode violation'),
                ('pointer_intercepted', r'intercepts pointer events'),
                ('timeout', r'timeout|timed out'),
                ('not_attached', r'not attached|detached'),
                ('invalid_selector', r'invalid selector|not a valid selector'),
                ('parser', r'parsing|unexpected token|selector parser'),
                ('xpath', r'invalid xpath|not a valid xpath|xpath expression could not be evaluated'),
            ):
                if re.search(pattern, text, re.I):
                    reason = category
                    break
            detail = {'operation': operation, 'cause_type': type(exc).__name__, 'reason': reason}
            method = re.match(r'((?:Locator|Page)\.[A-Za-z_]+):', text)
            if method:
                detail['api_method'] = method.group(1)
            trace = exc.__traceback__
            while trace:
                if Path(trace.tb_frame.f_code.co_filename).resolve() == Path(__file__).resolve():
                    detail['source_line'] = trace.tb_lineno
                trace = trace.tb_next
            raise AdapterError('DOUYIN_AI_LABEL_FAILED', messages[operation], retryable=True,
                               detail=_diagnostic_fields(detail)) from exc

    def submit(self, *, task_id, expected_account_id, **unused):
        if task_id in self.attempted:
            raise AdapterError('DOUYIN_ALREADY_ATTEMPTED', '已点击发布，禁止重复提交，请核验作品列表。', state='needs_verification')
        ready = self.prepared
        if not ready or ready['task_id'] != task_id:
            raise AdapterError('DOUYIN_PREPARATION_LOST', '浏览器准备状态已失效，请重新准备后确认。')
        identity = self.identity()
        self._upload_error()
        if identity['account_id'] != expected_account_id or identity['account_id'] != ready['account_id']:
            raise AdapterError('DOUYIN_ACCOUNT_CHANGED', '当前抖音账号已变化，发布已停止。')
        current_title = self._field(self.TITLE, 'DOUYIN_TITLE_NOT_FOUND').input_value()
        caption_field = self._field(self.CAPTION, 'DOUYIN_CAPTION_NOT_FOUND')
        current_caption = caption_field.input_value() if caption_field.evaluate('(e)=>e.tagName') == 'TEXTAREA' else caption_field.inner_text()
        if current_title != ready['title'] or _normalize_caption(current_caption) != _normalize_caption(ready['caption']):
            raise AdapterError('DOUYIN_FORM_CHANGED', '浏览器中的文案已变化，请重新准备并确认当前版本。')
        button = self.page.get_by_role('button', name='发布', exact=True)
        if button.count() != 1 or not button.is_enabled():
            raise AdapterError('DOUYIN_FORM_NOT_READY', '未找到唯一可用的发布按钮。')
        self.attempted.add(task_id)
        button.click(timeout=self.timeout)
        try:
            receipt = self.page.get_by_text(re.compile(r'^(发布成功|作品已发布|提交成功)$')).first
            receipt.wait_for(state='visible', timeout=self.timeout)
            receipt_text = receipt.inner_text().strip()
        except Exception as exc:
            raise AdapterError('DOUYIN_SUBMIT_UNVERIFIED', '已尝试提交，尚未取得明确回执。请检查抖音作品列表，勿重复发布。', state='needs_verification') from exc
        return {'state': 'submitted', 'provider_ref': None, 'url': None, 'is_mock': False,
                'receipt': {'kind': 'ui_ack', 'text': receipt_text},
                'message': '页面已显示提交成功，是否审核通过请以抖音作品列表为准。'}


class _BrowserHost:
    """同步 Playwright 对象只在同一常驻线程操作，扫码及填表后窗口保持打开。"""
    def __init__(self):
        self.commands = queue.Queue()
        self.driver = self.context = self.runtime = self.profile = None
        self.busy = False
        self.last_error = None
        self.thread = threading.Thread(target=self._run, name='douyin-browser', daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            fn, future = self.commands.get()
            self.busy = True
            try:
                future.set_result(fn())
            except Exception as exc:
                future.set_exception(exc)
            finally:
                self.busy = False

    def call(self, fn):
        future = Future()
        self.commands.put((fn, future))
        return future.result(timeout=660)

    def opened(self):
        return bool(self.driver and not self.driver.page.is_closed())

    def ensure(self, session_dir):
        if self.opened():
            if self.profile != str(session_dir.resolve()):
                raise AdapterError('DOUYIN_PROFILE_BUSY', '专用浏览器正在使用另一个会话目录。')
            return self.driver
        from playwright.sync_api import sync_playwright
        if self.runtime is None:
            self.runtime = sync_playwright().start()
        session_dir.mkdir(parents=True, exist_ok=True)
        self.context = self.runtime.chromium.launch_persistent_context(
            str(session_dir.resolve()), channel=os.getenv('AVATAR_STUDIO_DOUYIN_BROWSER_CHANNEL', 'chrome'),
            headless=False, viewport={'width': 1280, 'height': 900}, accept_downloads=False)
        page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.driver = DouyinPage(page)
        self.profile = str(session_dir.resolve())
        page.goto(CREATOR_URL, wait_until='domcontentloaded', timeout=45000)
        return self.driver

    def close(self):
        if self.context:
            self.context.close()
        if self.runtime:
            self.runtime.stop()
        self.context = self.runtime = self.driver = None


_host = None
_host_lock = threading.Lock()


def _get_host():
    global _host
    with _host_lock:
        if _host is None:
            _host = _BrowserHost()
        return _host


def close_browser():
    if _host and not _host.busy:
        _host.call(_host.close)


atexit.register(close_browser)


class PlaywrightDouyinAdapter:
    name = 'playwright'
    @staticmethod
    def _available():
        return importlib.util.find_spec('playwright') is not None

    def _perform(self, fn):
        if not self._available():
            raise AdapterError('PLAYWRIGHT_NOT_INSTALLED', '请安装 requirements-publish.txt 中的浏览器依赖。')
        try:
            return _get_host().call(fn)
        except AdapterError as exc:
            if _host:
                _host.last_error = {'type': type(exc).__name__, 'code': exc.code, **_diagnostic_fields(exc.detail)}
            raise
        except Exception as exc:
            if _host:
                _host.last_error = {'type': type(exc).__name__, 'code': 'DOUYIN_BROWSER_ERROR'}
            raise AdapterError('DOUYIN_BROWSER_ERROR', '浏览器操作未完成，请查看专用窗口后重新检查状态。',
                               retryable=True, detail={'type': type(exc).__name__}) from exc

    def _session_status(self, host):
        if not host.opened():
            return {'state': 'awaiting_login', 'code': 'DOUYIN_LOGIN_REQUIRED', 'message': '请点击“打开登录浏览器”，扫码后检查连接。', 'adapter': self.name}
        try:
            return {'state': 'logged_in', 'adapter': self.name, **host.driver.identity()}
        except AdapterError as exc:
            return {'state': exc.state, 'code': exc.code, 'message': exc.message, 'adapter': self.name}

    def status(self, session_dir):
        if not self._available():
            return {'state': 'unavailable', 'code': 'PLAYWRIGHT_NOT_INSTALLED', 'message': '发布浏览器依赖未安装。'}
        if _host is None:
            return {'state': 'awaiting_login', 'code': 'DOUYIN_LOGIN_REQUIRED', 'message': '请打开专用浏览器登录。'}
        if _host.busy:
            return {'state': 'busy', 'message': '发布浏览器正在处理任务。', 'adapter': self.name,
                    'phase': _host.driver.phase if _host.driver else 'opening'}
        return self._perform(lambda: self._session_status(_host))

    def inspect(self):
        host = _host

        def error_summary(error):
            if not isinstance(error, dict):
                return None
            kind, code = error.get('type'), error.get('code')
            return {'type': kind if isinstance(kind, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', kind) else 'Error',
                    'code': code if isinstance(code, str) and re.fullmatch(r'(?:DOUYIN|PLAYWRIGHT)_[A-Z0-9_]{1,80}', code) else 'DOUYIN_BROWSER_ERROR',
                    **_diagnostic_fields(error)}

        phase = host.driver.phase if host and host.driver else 'opening'
        if host is None or host.busy:
            return {'state': 'unavailable' if host is None else 'busy', 'phase': phase,
                    'last_error': error_summary(host.last_error) if host else None}
        try:
            return self._perform(lambda: {**(host.driver.inspect_controls() if host.opened() else {'state': 'closed', 'phase': phase}),
                                          'last_error': error_summary(host.last_error)})
        except AdapterError as exc:
            # 诊断本身失败也应可读；Playwright 异常正文可能包含 URL 查询及凭证。
            error = error_summary({'type': type(exc.__cause__ or exc).__name__, 'code': exc.code,
                                   **_diagnostic_fields(exc.detail)})
            host.last_error = error
            return {'state': 'unreadable', 'phase': phase, 'code': error['code'], 'last_error': error}

    def login(self, session_dir):
        def action():
            host = _get_host()
            driver = host.ensure(session_dir)
            driver.page.bring_to_front()
            login = driver.page.get_by_role('button', name='登录后使用丰富的作品体裁', exact=True)
            if login.count() == 1 and login.is_visible():
                login.click(timeout=10000)
            return self._session_status(host)
        return self._perform(action)

    def prepare(self, *, session_dir, **kwargs):
        def action():
            driver = _get_host().ensure(session_dir)
            driver.identity()
            driver.page.goto(CREATOR_URL, wait_until='domcontentloaded', timeout=45000)
            return driver.prepare(**kwargs)
        return self._perform(action)

    def submit(self, *, session_dir, **kwargs):
        def action():
            host = _get_host()
            if not host.opened():
                raise AdapterError('DOUYIN_PREPARATION_LOST', '专用浏览器已关闭，请重新准备。',
                                   detail={'submission_attempted': False})
            try:
                return host.driver.submit(**kwargs)
            except AdapterError as exc:
                exc.detail['submission_attempted'] = kwargs.get('task_id') in host.driver.attempted
                raise
            except Exception as exc:
                raise AdapterError('DOUYIN_SUBMIT_UNVERIFIED', '提交结果未知，请在作品列表核验，勿重复提交。', state='needs_verification') from exc
        return self._perform(action)


def adapter_for(platform):
    if platform != 'douyin':
        raise AdapterError('PLATFORM_UNSUPPORTED', '当前只支持抖音。')
    configured = os.getenv('AVATAR_STUDIO_PUBLISH_ADAPTER', '').lower().strip()
    if configured == 'mock' or (not configured and os.getenv('AVATAR_STUDIO_MODE', '').lower() == 'mock'):
        return MockDouyinAdapter()
    if configured not in ('', 'playwright'):
        raise AdapterError('PUBLISH_ADAPTER_INVALID', '发布适配器配置无效。')
    return PlaywrightDouyinAdapter()
