"""双比例封面编辑器的真实本地 DOM 回归；所有网络请求禁止。"""

import base64

import pytest

from app.publishing import adapters, cover_assets


@pytest.fixture()
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        page.set_default_timeout(2000)
        yield page
        browser.close()


@pytest.fixture()
def assets(tmp_path, monkeypatch):
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGAAAAABJRU5ErkJggg==")
    source = tmp_path / "source.png"
    source.write_bytes(png)
    outputs = {kind: tmp_path / (kind + ".png") for kind in ("portrait", "landscape")}
    for output in outputs.values():
        output.write_bytes(png)
    monkeypatch.setattr(cover_assets, "prepare_cover_assets", lambda path: outputs)
    return source


def modern_editor(page, *, application="both", uploader=True, preview_updates=True,
                  canvas_delay_ms=0, dialog_role=True):
    page.set_content('''
        <input id="legacy" type="file" accept="image/png" hidden>
        <div id="portrait" class="coverControl-fixture">
          <div class="background-fixture filter-fixture"><img width="30" height="40"></div>
          <button>选择封面</button><span>竖封面3:4</span>
        </div>
        <div id="landscape" class="coverControl-fixture">
          <div class="background-fixture filter-fixture"><img width="40" height="30"></div>
          <button>选择封面</button><span>横封面4:3</span>
        </div>
        <button id="publish">发布</button>
    ''')
    page.evaluate("""options => {
        const old = document.createElement('canvas'); old.width = old.height = 2;
        old.getContext('2d').fillRect(0, 0, 2, 2);
        document.querySelectorAll('.coverControl-fixture img').forEach(img => img.src = old.toDataURL());
        window.uploads = []; window.legacyUploads = 0; window.referenceUploads = 0;
        window.openedCard = null; window.publishClicks = 0; window.completed = false;
        document.querySelector('#publish').onclick = () => window.publishClicks++;
        document.querySelector('#legacy').onchange = () => {
            window.legacyUploads++;
            const crop = document.createElement('section');
            crop.innerHTML = '<h3>设置封面</h3><img alt="裁剪图预览" width="20" height="20"><button>保存</button>';
            crop.querySelector('button').onclick = () => crop.remove();
            document.body.appendChild(crop);
        };
        for (const kind of ['portrait', 'landscape']) {
            document.querySelector('#' + kind).onclick = () => openEditor(kind);
        }
        function openEditor(kind) {
            window.openedCard = kind;
            let current = kind;
            const pending = {};
            const dialog = document.createElement('div');
            if (options.dialogRole) dialog.setAttribute('role', 'dialog');
            dialog.innerHTML = `<section><h3>生成参考图</h3><div>
                <input id="reference" type="file" class="semi-upload-hidden-input" accept="image/png">
                <input type="file" class="semi-upload-hidden-input-replace" accept="image/png">
                </div></section>
                ${options.uploader ? `<section><h3>上传封面</h3><div class="semi-upload upload-fixture">
                <span>点击上传文件或拖拽文件到这里</span>
                <input id="cover-file" type="file" class="semi-upload-hidden-input" accept="image/png,image/bmp,image/webp,image/tif">
                <input id="cover-replace" type="file" class="semi-upload-hidden-input-replace" accept="image/png,image/bmp,image/webp,image/tif">
                </div></section>` : ''}
                <p id="ratio">竖封面预览（3:4）</p>
                <canvas class="cloudImage-fixture lower-canvas" width="20" height="20"></canvas>
                <canvas class="cloudImage-fixture lower-canvas" hidden></canvas>
                <canvas class="lower-canvas" width="5" height="5"></canvas>
                <button id="switch">设置横封面</button><button id="done" disabled>完成</button>`;
            dialog.querySelector('#reference').onchange = () => window.referenceUploads++;
            if (options.uploader) {
                dialog.querySelector('#cover-replace').onchange = () => window.referenceUploads++;
                dialog.querySelector('#cover-file').onchange = event => {
                    const captured = current;
                    const file = event.target.files[0];
                    window.uploads.push([captured, file.name]);
                    const reader = new FileReader();
                    reader.onload = () => setTimeout(() => {
                        pending[captured] = reader.result;
                        if (options.previewUpdates) {
                            const canvas = dialog.querySelector('canvas.cloudImage-fixture');
                            const context = canvas.getContext('2d');
                            context.fillStyle = captured === 'portrait' ? '#ff0000' : '#0000ff';
                            context.fillRect(0, 0, 20, 20);
                        }
                        dialog.querySelector('#done').disabled = !(pending.portrait && pending.landscape);
                    }, 150);
                    reader.readAsDataURL(file);
                };
            }
            dialog.querySelector('#switch').onclick = () => {
                current = 'landscape';
                dialog.querySelector('#ratio').textContent = '横封面预览（4:3）';
                dialog.querySelector('canvas.cloudImage-fixture').getContext('2d').clearRect(0, 0, 20, 20);
            };
            dialog.querySelector('#done').onclick = () => {
                window.completed = true;
                dialog.remove();
                setTimeout(() => {
                    for (const target of ['portrait', 'landscape']) {
                        if (options.application === 'none' || options.application === 'portrait_only' && target === 'landscape') continue;
                        const card = document.querySelector('#' + target);
                        card.querySelector('button').remove();
                        card.querySelector('img').parentElement.className = 'background-fixture';
                        if (options.application !== 'unchanged') {
                            card.querySelector('img').src = options.application === 'unloaded' ? 'data:image/png;base64,invalid' : pending[target];
                        }
                    }
                }, 200);
            };
            document.body.appendChild(dialog);
            if (options.canvasDelay) {
                const main = dialog.querySelector('canvas.cloudImage-fixture');
                main.hidden = true;
                setTimeout(() => main.hidden = false, options.canvasDelay);
            }
        }
    }""", {"application": application, "uploader": uploader, "previewUpdates": preview_updates,
           "canvasDelay": canvas_delay_ms, "dialogRole": dialog_role})


def test_modern_editor_selects_card_and_uploads_each_ratio_to_cover_input(page, assets):
    modern_editor(page)

    adapters.DouyinPage(page, timeout_ms=2000)._set_cover(assets)

    assert page.evaluate("window.openedCard") == "portrait"
    assert page.evaluate("window.uploads") == [["portrait", "portrait.png"], ["landscape", "landscape.png"]]
    assert page.evaluate("window.legacyUploads + window.referenceUploads + window.publishClicks") == 0
    assert page.evaluate("window.completed") is True
    assert page.get_by_text("选择封面", exact=True).count() == 0
    assert page.locator('.coverControl-fixture img').evaluate_all("images => images.every(img => img.complete && img.naturalWidth > 0)")


def test_ratio_label_can_appear_before_main_canvas_is_ready(page, assets):
    modern_editor(page, canvas_delay_ms=350)

    adapters.DouyinPage(page, timeout_ms=2000)._set_cover(assets)

    assert page.evaluate("window.uploads") == [["portrait", "portrait.png"], ["landscape", "landscape.png"]]
    assert page.get_by_text("选择封面", exact=True).count() == 0
    assert page.evaluate("window.legacyUploads + window.referenceUploads + window.publishClicks") == 0


@pytest.mark.parametrize("application", ["none", "portrait_only", "unchanged", "unloaded"])
def test_closing_editor_without_both_new_loaded_cards_is_not_success(page, assets, application):
    modern_editor(page, application=application)

    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=800)._set_cover(assets)

    assert error.value.detail["operation"] == "verify_cards"
    assert page.evaluate("window.completed") is True
    assert page.evaluate("window.legacyUploads + window.referenceUploads + window.publishClicks") == 0


def test_modern_editor_never_falls_back_to_reference_or_legacy_input(page, assets):
    modern_editor(page, uploader=False)

    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=800)._set_cover(assets)

    assert error.value.detail["operation"] == "locate_upload"
    assert page.evaluate("window.legacyUploads + window.referenceUploads + window.publishClicks") == 0


def test_unchanged_main_canvas_blocks_switch_and_completion(page, assets):
    modern_editor(page, preview_updates=False)

    with pytest.raises(adapters.AdapterError) as error:
        adapters.DouyinPage(page, timeout_ms=800)._set_cover(assets)

    assert error.value.detail["operation"] == "verify_portrait_preview"
    assert page.evaluate("window.uploads") == [["portrait", "portrait.png"]]
    assert page.evaluate("window.completed === false && window.publishClicks === 0") is True


def test_modern_editor_can_set_each_ratio_in_separate_dialog(page, assets):
    page.set_content('''
        <div id="portrait" class="coverControl-fixture">
          <div class="background-fixture filter-fixture"><img width="30" height="40"></div>
          <button>选择封面</button><span>竖封面3:4</span>
        </div>
        <div id="landscape" class="coverControl-fixture">
          <div class="background-fixture filter-fixture"><img width="40" height="30"></div>
          <button>选择封面</button><span>横封面4:3</span>
        </div>
    ''')
    page.evaluate("""() => {
        const old = document.createElement('canvas'); old.width = old.height = 2;
        old.getContext('2d').fillRect(0, 0, 2, 2);
        document.querySelectorAll('.coverControl-fixture img').forEach(img => img.src = old.toDataURL());
        window.uploads = [];
        for (const kind of ['portrait', 'landscape']) {
            document.querySelector('#' + kind).onclick = () => {
                const dialog = document.createElement('div');
                dialog.setAttribute('role', 'dialog');
                dialog.innerHTML = `<span>设置${kind === 'portrait' ? '竖' : '横'}封面</span>
                    <div class="upload-fixture"><span>点击上传文件或拖拽文件到这里</span>
                    <input type="file" accept="image/png"></div>
                    <canvas class="cloudImage-fixture lower-canvas" width="20" height="20"></canvas>
                    <button>完成</button>`;
                const input = dialog.querySelector('input');
                input.onchange = event => {
                    const file = event.target.files[0];
                    window.uploads.push([kind, file.name]);
                    const canvas = dialog.querySelector('canvas');
                    canvas.getContext('2d').fillStyle = kind === 'portrait' ? '#f00' : '#00f';
                    canvas.getContext('2d').fillRect(0, 0, 20, 20);
                };
                dialog.querySelector('button').onclick = () => {
                    const card = document.querySelector('#' + kind);
                    card.querySelector('button').remove();
                    card.querySelector('img').parentElement.className = 'background-fixture';
                    card.querySelector('img').src = dialog.querySelector('canvas').toDataURL();
                    dialog.remove();
                };
                document.body.appendChild(dialog);
            };
        }
    }""")

    adapters.DouyinPage(page, timeout_ms=2000)._set_cover(assets)

    assert page.evaluate("window.uploads") == [["portrait", "portrait.png"], ["landscape", "landscape.png"]]
    assert page.get_by_text("选择封面", exact=True).count() == 0


def test_modern_editor_without_aria_dialog_role_is_supported(page, assets):
    """抖音弹层可能没有 role=dialog，但仍有稳定的编辑器控件结构。"""
    modern_editor(page, dialog_role=False)

    adapters.DouyinPage(page, timeout_ms=2000)._set_cover(assets)

    assert page.evaluate("window.uploads") == [["portrait", "portrait.png"], ["landscape", "landscape.png"]]
    assert page.get_by_text("选择封面", exact=True).count() == 0
