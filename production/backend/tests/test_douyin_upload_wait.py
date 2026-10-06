"""抖音上传完成判定：只使用本地 DOM，不访问真实创作者中心。"""

import pytest

from app.publishing import adapters


@pytest.fixture()
def page(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_DOUYIN_UPLOAD_TIMEOUT_MS", "150")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.set_default_timeout(1500)
        yield page
        browser.close()


def test_completed_upload_ignores_hidden_progress_descendant(page):
    page.set_content("""
        <section class="upload-card">
          <h2>预览视频</h2>
          <button>重新上传</button>
          <div class="status-label">上传进度</div>
          <div style="display:none">上传中 100%</div>
        </section>
    """)

    adapters.DouyinPage(page, timeout_ms=150)._wait_uploaded()


def test_completed_upload_ignores_actual_post_editor_footer(page):
    page.set_content("""
        <section><h2>预览视频</h2><button>重新上传</button></section>
        <footer>点击发布后，如作品还在上传中，请勿关闭页面，等待上传发布完成。</footer>
    """)

    adapters.DouyinPage(page, timeout_ms=150)._wait_uploaded()


@pytest.mark.parametrize("progress", [
    "上传中", "上传中…", "上传中...", "正在上传", "正在上传 72%",
    "上传中 72%", "视频上传中 72%", "上传进度：72%", "上传进度：72.5%",
])
def test_upload_progress_still_waits_for_completion(page, progress):
    page.set_content("""
        <section class="upload-card">
          <h2>预览视频</h2>
          <button>重新上传</button>
          <div id="progress"></div>
        </section>
        <footer>点击发布后，如作品还在上传中，请勿关闭页面，等待上传发布完成。</footer>
    """)
    page.locator("#progress").evaluate("(element, value) => element.textContent = value", progress)

    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=150)._wait_uploaded()
    assert error.value.code == "DOUYIN_UPLOAD_TIMEOUT"


def test_footer_without_completion_marker_still_times_out(page):
    page.set_content("""
        <footer>点击发布后，如作品还在上传中，请勿关闭页面，等待上传发布完成。</footer>
    """)

    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=150)._wait_uploaded()
    assert error.value.code == "DOUYIN_UPLOAD_TIMEOUT"


def test_upload_failure_still_blocks_completion(page):
    page.set_content("""
        <section class="upload-card">
          <h2>预览视频</h2>
          <button>重新上传</button>
          <div>上传失败，请重新上传</div>
        </section>
        <footer>点击发布后，如作品还在上传中，请勿关闭页面，等待上传发布完成。</footer>
    """)

    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=150)._wait_uploaded()
    assert error.value.code == "DOUYIN_UPLOAD_FAILED"
