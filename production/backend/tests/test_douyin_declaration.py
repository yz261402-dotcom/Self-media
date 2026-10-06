"""AI 声明选择与保存的真实本地 DOM 回归，不连接真实网站。"""
import pytest

from app.publishing import adapters


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.abort())
        page.set_default_timeout(500)
        yield page
        browser.close()


def declaration(page, *, opened=False, selected=False, save='save'):
    page.set_content('''<button id="declaration" onclick="document.querySelector('#modal').hidden=false">请选择自主声明</button>
      <p>作者声明：内容由AI生成</p>
      <section id="modal" hidden>
        <h3>对作品内容添加声明</h3>
        <label><input type="radio" name="kind" id="ai" onchange="document.querySelector('#declaration').textContent='内容由AI生成'">内容由AI生成</label>
        <label><input type="radio" name="kind">内容为个人观点或见解</label>
        <button>取消</button><button id="confirm">确定</button>
      </section><button id="publish" onclick="window.published=true">发布</button>''')
    if selected:
        page.locator('#ai').evaluate("e=>{e.checked=true;document.querySelector('#declaration').textContent='内容由AI生成'}")
    if opened:
        page.locator('#modal').evaluate('e=>e.hidden=false')
    if save == 'save':
        page.locator('#confirm').evaluate("e=>e.onclick=()=>{window.saved=true;document.querySelector('#modal').hidden=true}")
    elif save == 'wrong_value':
        page.locator('#confirm').evaluate("e=>e.onclick=()=>{document.querySelector('#modal').hidden=true;document.querySelector('#declaration').textContent='内容为个人观点或见解'}")


def test_already_selected_open_modal_is_saved(page):
    declaration(page, opened=True, selected=True)

    adapters.DouyinPage(page, timeout_ms=500)._set_ai_label()

    assert page.locator('#modal').is_hidden()
    assert page.evaluate('window.saved===true')
    assert page.locator('#declaration').inner_text() == '内容由AI生成'
    assert page.evaluate('window.published!==true')


def test_fresh_declaration_selects_and_saves_before_return(page):
    declaration(page)

    adapters.DouyinPage(page, timeout_ms=500)._set_ai_label()

    assert page.locator('#ai').is_checked()
    assert page.locator('#modal').is_hidden()
    assert page.evaluate('window.saved===true')


@pytest.mark.parametrize('hidden_radio', [False, True])
def test_radio_label_with_noninteractive_text_selects_control_and_saves(page, hidden_radio):
    declaration(page)
    page.locator('#ai').evaluate('''input => {
        const label = input.parentElement;
        for (const child of [...label.childNodes]) {
            if (child.nodeType === Node.TEXT_NODE) child.remove();
        }
        const span = document.createElement('span');
        span.textContent = '内容由AI生成';
        span.style.pointerEvents = 'none';
        label.appendChild(span);
    }''')
    if hidden_radio:
        page.locator('#ai').evaluate("input=>input.style.display='none'")

    adapters.DouyinPage(page, timeout_ms=500)._set_ai_label()

    assert page.locator('#ai').is_checked()
    assert page.locator('#modal').is_hidden()
    assert page.locator('#declaration').inner_text() == '内容由AI生成'
    assert page.evaluate('window.saved===true && window.published!==true')


@pytest.mark.parametrize('save', ['no_effect', 'wrong_value'])
def test_declaration_save_without_closed_modal_and_correct_value_is_blocked(page, save):
    declaration(page, opened=True, selected=True, save=save)

    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=500)._set_ai_label()

    assert error.value.code in {'DOUYIN_AI_LABEL_REQUIRED', 'DOUYIN_AI_LABEL_FAILED'}
    assert error.value.detail['operation'] in {'confirm', 'verify'}
    assert page.evaluate('window.published!==true')


def test_missing_declaration_control_cannot_silently_succeed(page):
    page.set_content('<p>作者声明：内容由AI生成</p><button>发布</button>')

    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=500)._set_ai_label()

    assert error.value.code == 'DOUYIN_AI_LABEL_REQUIRED'


def test_checked_checkbox_remains_a_supported_declaration(page):
    page.set_content('<label><input type="checkbox">内容由AI生成</label>')

    adapters.DouyinPage(page, timeout_ms=500)._set_ai_label()

    assert page.get_by_role('checkbox').is_checked()


def test_selection_errors_report_operation_without_exception_body(page, monkeypatch):
    declaration(page, opened=True)
    from playwright.sync_api import Locator

    def fail_action(self, **kwargs):
        raise RuntimeError('token=private-token https://example.invalid/?secret=private-query')

    monkeypatch.setattr(Locator, 'check', fail_action)
    monkeypatch.setattr(Locator, 'click', fail_action)
    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=500)._set_ai_label()

    assert error.value.code == 'DOUYIN_AI_LABEL_FAILED'
    assert error.value.detail['operation'] == 'select'
    assert error.value.detail['cause_type'] == 'RuntimeError'
    assert error.value.detail['reason'] == 'other'
    assert isinstance(error.value.detail['source_line'], int)
    assert 'private-' not in str(error.value.detail)
    assert '选择失败' in error.value.message
    assert 'private-' not in error.value.message
