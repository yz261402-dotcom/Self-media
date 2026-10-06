"""封面上传后的页面等待：使用隔离本地 DOM，不访问抖音。"""

import pytest

from app.publishing import adapters


@pytest.fixture()
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        page.set_default_timeout(1500)
        yield page
        browser.close()


@pytest.fixture()
def cover(tmp_path):
    path = tmp_path / "cover.png"
    path.write_bytes(b"local-cover-fixture")
    return path


def crop_page(page, *, delay_ms=0, closes=True):
    page.set_content('<input type="file" accept="image/png"><button id="publish">发布</button>')
    page.locator('input[type="file"]').evaluate("""(input, options) => {
        input.addEventListener('change', () => setTimeout(() => {
            const panel = document.createElement('section');
            panel.id = 'crop';
            panel.innerHTML = '<h3>设置封面</h3><img alt="裁剪图预览" width="40" height="40">'
                + '<button>保存</button>';
            panel.querySelector('button').onclick = () => {
                window.coverSaveClicks = (window.coverSaveClicks || 0) + 1;
                if (options.closes) panel.remove();
            };
            document.body.appendChild(panel);
        }, options.delay));
        document.querySelector('#publish').onclick = () => window.publishClicked = true;
    }""", {"delay": delay_ms, "closes": closes})


def test_crop_appearing_after_three_seconds_is_saved(page, cover):
    crop_page(page, delay_ms=3600)

    adapters.DouyinPage(page, timeout_ms=5000)._set_cover(cover)

    assert page.evaluate("window.coverSaveClicks") == 1
    assert page.locator("#crop").count() == 0
    assert page.evaluate("window.publishClicked || false") is False


def test_existing_saved_marker_does_not_skip_delayed_new_crop(page, cover):
    crop_page(page, delay_ms=200)
    page.locator("body").evaluate("element => element.insertAdjacentHTML('beforeend', '<p>封面已设置</p>')")

    adapters.DouyinPage(page, timeout_ms=1500)._set_cover(cover)

    assert page.evaluate("window.coverSaveClicks || 0") == 1
    assert page.locator("#crop").count() == 0
    assert page.evaluate("window.publishClicked || false") is False


def test_crop_that_stays_open_reports_verification_timeout(page, cover):
    crop_page(page, closes=False)

    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=300)._set_cover(cover)

    assert error.value.code == "DOUYIN_COVER_FAILED"
    assert error.value.detail == {"operation": "verify_crop", "error_type": "TimeoutError"}
    assert page.evaluate("window.coverSaveClicks") == 1
    assert page.evaluate("window.publishClicked || false") is False


def test_missing_cover_result_reports_wait_timeout(page, cover):
    page.set_content('<input type="file" accept="image/png">')

    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=200)._set_cover(cover)

    assert error.value.code == "DOUYIN_COVER_TIMEOUT"
    assert error.value.detail == {"operation": "wait_result"}


def test_delayed_supported_dialog_is_saved(page, cover):
    page.set_content('<input type="file" accept="image/png">')
    page.locator('input').evaluate("""input => input.addEventListener('change', () => {
        setTimeout(() => {
            const dialog = document.createElement('div');
            dialog.role = 'dialog';
            dialog.innerHTML = '<h3>设置封面</h3><button>完成</button>';
            dialog.querySelector('button').onclick = () => {
                window.coverSaveClicks = (window.coverSaveClicks || 0) + 1;
                dialog.remove();
            };
            document.body.appendChild(dialog);
        }, 200);
    })""")

    adapters.DouyinPage(page, timeout_ms=1500)._set_cover(cover)

    assert page.evaluate("window.coverSaveClicks") == 1
    assert page.get_by_role("dialog").count() == 0


def test_direct_upload_waits_for_saved_result(page, cover):
    page.set_content('<input type="file" accept="image/png"><p id="status"></p>')
    page.locator('input').evaluate("""input => input.addEventListener('change', () => {
        setTimeout(() => document.querySelector('#status').textContent = '封面已设置', 200);
    })""")

    adapters.DouyinPage(page, timeout_ms=1500)._set_cover(cover)

    assert page.locator("#status").inner_text() == "封面已设置"
