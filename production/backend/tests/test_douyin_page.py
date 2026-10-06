"""用真实浏览器的本地页面验证发布前保护；不访问抖音，不公开作品。"""
from pathlib import Path
import pytest

from app.publishing import adapters


@pytest.fixture
def page(monkeypatch):
    monkeypatch.setenv('AVATAR_STUDIO_DOUYIN_UPLOAD_TIMEOUT_MS', '1500')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.set_default_timeout(1500)
        yield page
        browser.close()


def editor(page, *, cover=True):
    page.set_content('''<p>抖音号：test_account_27</p>
      <input type="file" accept="video/*" aria-label="上传视频">
      <input placeholder="填写作品标题，为作品获得更多流量">
      <div contenteditable="true" data-placeholder="添加作品简介"></div>
      <p>上传成功</p><label><input type="checkbox" checked>内容由AI生成</label>''' + ('''<input type="file" accept="image/*" aria-label="上传封面"
        onchange="document.querySelector('#cover-status').textContent='封面已设置'">
      <p id="cover-status"></p>''' if cover else '') + '''
      <button onclick="window.clicked=(window.clicked||0)+1;document.querySelector('#result').textContent='发布成功'">发布</button>
      <p id="result"></p>''')


def media(tmp_path):
    video, cover = tmp_path / 'video.mp4', tmp_path / 'cover.jpg'
    video.write_bytes(b'fixture')
    cover.write_bytes(b'fixture')
    return video, cover


def test_prepare_uploads_and_fills_but_never_publishes(page, tmp_path):
    editor(page)
    video, cover = media(tmp_path)
    driver = adapters.DouyinPage(page, timeout_ms=1500)
    result = driver.prepare(task_id='p1', video_path=video, cover_path=cover,
                            title='认识 RAG', description='查资料再作答', tags=['AI', '#RAG'])
    assert result['state'] == 'ready_to_confirm'
    assert result['account_id'] == 'test_account_27'
    assert page.locator('input[accept="video/*"]').evaluate('(e)=>e.files[0].name') == 'video.mp4'
    caption = page.locator('[contenteditable]').inner_text()
    assert '#AI' in caption and '#RAG' in caption and 'AI 生成' in caption
    assert page.evaluate('window.clicked||0') == 0


def test_submit_checks_same_account_and_unmodified_form(page, tmp_path):
    editor(page)
    video, cover = media(tmp_path)
    driver = adapters.DouyinPage(page, timeout_ms=1500)
    driver.prepare(task_id='p1', video_path=video, cover_path=cover, title='标题', description='简介', tags=[])
    with pytest.raises(adapters.AdapterError) as err:
        driver.submit(task_id='p1', expected_account_id='wrong_account')
    assert err.value.code == 'DOUYIN_ACCOUNT_CHANGED'
    page.locator('input[placeholder]').fill('浏览器里改过的标题')
    with pytest.raises(adapters.AdapterError) as err:
        driver.submit(task_id='p1', expected_account_id='test_account_27')
    assert err.value.code == 'DOUYIN_FORM_CHANGED'
    assert page.evaluate('window.clicked||0') == 0


def test_submit_ignores_rich_text_editor_extra_paragraph_breaks(page, tmp_path):
    editor(page)
    video, cover = media(tmp_path)
    driver = adapters.DouyinPage(page, timeout_ms=1500)
    driver.prepare(task_id='p1', video_path=video, cover_path=cover,
                   title='标题', description='简介', tags=['AI'])
    page.locator('[contenteditable]').evaluate('''e => {
      e.innerHTML = '<div>简介</div><div><br></div><div>#AI</div><div><br></div>' +
        '<div>本片人物与声音由 AI 生成</div>';
    }''')
    result = driver.submit(task_id='p1', expected_account_id='test_account_27')
    assert result['state'] == 'submitted'
    assert page.evaluate('window.clicked') == 1


def test_submit_runs_once_and_does_not_invent_public_link(page, tmp_path):
    editor(page)
    video, cover = media(tmp_path)
    driver = adapters.DouyinPage(page, timeout_ms=1500)
    driver.prepare(task_id='p1', video_path=video, cover_path=cover, title='标题', description='简介', tags=[])
    result = driver.submit(task_id='p1', expected_account_id='test_account_27')
    assert result['state'] == 'submitted'
    assert result['receipt'] == {'kind': 'ui_ack', 'text': '发布成功'}
    assert result.get('url') is None  # 管理页或 about:blank 不是作品链接
    with pytest.raises(adapters.AdapterError):
        driver.submit(task_id='p1', expected_account_id='test_account_27')
    assert page.evaluate('window.clicked') == 1


def test_missing_cover_blocks_without_pressing_publish(page, tmp_path):
    editor(page, cover=False)
    video, cover = media(tmp_path)
    with pytest.raises(adapters.AdapterError) as err:
        adapters.DouyinPage(page, timeout_ms=1500).prepare(
            task_id='p1', video_path=video, cover_path=cover, title='标题', description='简介', tags=[])
    assert err.value.code == 'DOUYIN_COVER_REQUIRED'
    assert page.evaluate('window.clicked||0') == 0


def test_login_marker_file_is_not_authentication(tmp_path):
    (tmp_path / 'logged_in.json').write_text('{}')
    (tmp_path / 'storage_state.json').write_text('{}')
    result = adapters.PlaywrightDouyinAdapter().status(tmp_path)
    assert result['state'] != 'logged_in'


def test_captcha_is_manual_and_no_upload(page, tmp_path):
    editor(page)
    page.locator('body').evaluate("e=>e.insertAdjacentHTML('afterbegin','<div>请完成安全验证</div>')")
    video, cover = media(tmp_path)
    with pytest.raises(adapters.AdapterError) as err:
        adapters.DouyinPage(page, timeout_ms=1500).prepare(
            task_id='p1', video_path=video, cover_path=cover, title='标题', description='简介', tags=[])
    assert err.value.code == 'DOUYIN_MANUAL_VERIFICATION_REQUIRED'
    assert page.locator('input[accept="video/*"]').evaluate('(e)=>e.files.length') == 0


def test_caption_cannot_impersonate_login_or_change_target_account(page):
    page.set_content('''<div contenteditable="true">先扫码登录。抖音号：wrong_account</div>
        <p>抖音号：correct_account</p>''')
    assert adapters.DouyinPage(page).identity()['account_id'] == 'correct_account'


def test_page_diagnostics_only_describes_controls_not_secret_values(page):
    page.set_content('<input type="hidden" value="secret-token"><input type="password" value="password"><input placeholder="作品标题" value="private-title"><button>发布</button>')
    observation = adapters.DouyinPage(page).inspect_controls()
    assert 'secret-token' not in str(observation) and 'password' not in str(observation)
    assert 'private-title' not in str(observation)
    assert any(i['placeholder'] == '作品标题' for i in observation['inputs'])


def test_hidden_dormant_cover_and_old_upload_message_do_not_confuse_preparation(page, tmp_path):
    editor(page)
    page.locator('body').evaluate("e=>e.insertAdjacentHTML('afterbegin','<section style=\"display:none\"><p>上传成功</p><input type=\"file\" accept=\"image/*\"></section>')")
    video, cover = media(tmp_path)
    result = adapters.DouyinPage(page, timeout_ms=1500).prepare(
        task_id='p1', video_path=video, cover_path=cover, title='标题', description='简介', tags=[])
    assert result['state'] == 'ready_to_confirm'
    assert page.evaluate('window.clicked||0') == 0


def test_actual_style_crop_without_dialog_role_is_saved_before_confirmation(page, tmp_path):
    editor(page)
    page.locator('input[accept="image/*"]').evaluate('''e=>e.addEventListener('change',()=>{
      const crop=document.createElement('div');crop.id='crop';crop.innerHTML=`<h3>设置封面</h3><img alt="裁剪图预览" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGAAAAABJRU5ErkJggg=="><button onclick="this.parentElement.remove();window.saved=true">保存</button>`;document.body.appendChild(crop);
    })''')
    video, cover = media(tmp_path)
    result = adapters.DouyinPage(page, timeout_ms=1500).prepare(
        task_id='p1', video_path=video, cover_path=cover, title='标题', description='简介', tags=[])
    assert result['state'] == 'ready_to_confirm'
    assert page.evaluate('window.saved===true')
    assert page.evaluate('window.clicked||0') == 0


def test_platform_ai_declaration_is_selected_when_offered(page):
    page.set_content('''<button id="declaration" onclick="document.querySelector('#options').hidden=false">请选择自主声明</button>
        <div id="options" hidden><div role="option" onclick="document.querySelector('#declaration').textContent='内容由AI生成';this.parentElement.hidden=true">内容由AI生成</div></div>''')
    adapters.DouyinPage(page, timeout_ms=1500)._set_ai_label()
    assert page.locator('#declaration').inner_text() == '内容由AI生成'


def test_single_hidden_cover_input_is_valid_programmatic_upload(page, tmp_path):
    editor(page)
    page.locator('input[accept="image/*"]').evaluate("e=>{const p=document.createElement('div');p.hidden=true;e.replaceWith(p);p.appendChild(e)}")
    video, cover = media(tmp_path)
    result = adapters.DouyinPage(page, timeout_ms=1500).prepare(
        task_id='p1', video_path=video, cover_path=cover, title='标题', description='简介', tags=[])
    assert result['state'] == 'ready_to_confirm'


def test_service_accepts_real_browser_ui_receipt_without_fabricating_url(page, tmp_path, monkeypatch):
    from app import storage as st
    from app.publishing import service
    editor(page)
    video, cover = media(tmp_path)
    driver = adapters.DouyinPage(page, timeout_ms=1500)
    class LocalPageAdapter:
        name = 'fixture-browser'
        def prepare(self, session_dir, **kwargs):
            return driver.prepare(**kwargs)
        def submit(self, session_dir, **kwargs):
            return driver.submit(**kwargs)
    package = {'id': st.new_id('pkg'), 'manifest_digest': 'digest', 'title': '标题', 'description': '简介', 'tags': []}
    work_id = st.new_id('work')
    monkeypatch.setattr(service, 'resolve_publish_package', lambda _: (package, video, cover))
    monkeypatch.setattr(service, 'get_adapter', lambda _: LocalPageAdapter())
    monkeypatch.setattr(service, '_session_dir', lambda: tmp_path / 'profile')
    monkeypatch.setattr(service, '_spawn', lambda target, *args: target(*args))
    service.start_task(work_id)
    ready = service.get_task(work_id)
    assert ready['state'] == 'ready_to_confirm'
    assert page.evaluate('window.clicked||0') == 0
    service.confirm_task(work_id, task_id=ready['id'], confirmed=True, actor_id='user_test',
                         package_digest=ready['package_digest'], confirmation_digest=ready['confirmation_digest'])
    done = service.get_task(work_id)
    assert done['state'] == 'submitted'
    assert done['provider_url'] is None and done['receipt']['text'] == '发布成功'
    assert page.evaluate('window.clicked') == 1


def test_upload_failure_with_retry_text_is_never_ready(page, tmp_path):
    editor(page)
    page.get_by_text('上传成功', exact=True).evaluate("e=>e.textContent='上传失败，重新上传'")
    video, cover = media(tmp_path)
    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=1500).prepare(
            task_id='p1', video_path=video, cover_path=cover, title='标题', description='简介', tags=[])
    assert error.value.code == 'DOUYIN_UPLOAD_FAILED'
    assert page.evaluate('window.clicked||0') == 0
