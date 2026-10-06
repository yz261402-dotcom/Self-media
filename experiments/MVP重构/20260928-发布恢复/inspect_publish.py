"""本机封面排查：使用专用 profile 与已确认作品；明确禁止最终发布点击。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'production/backend'))
from app.publishing import adapters, service
from app import storage
from playwright.sync_api import sync_playwright

task = storage.get('publish_tasks', 'publish_9073988ae1f7')
if not task or task.get('attempt_started_at'):
    raise RuntimeError('Task missing or submission already attempted')
package, video, cover = service.resolve_publish_package(task['work_id'])

def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)

with sync_playwright() as runtime:
    context = runtime.chromium.launch_persistent_context(
        str(service.SESSION_DIR.resolve()), channel='chrome', headless=False,
        viewport={'width': 1280, 'height': 900}, accept_downloads=False)
    page = context.pages[0]
    driver = adapters.DouyinPage(page)
    page.goto(adapters.CREATOR_URL, wait_until='domcontentloaded', timeout=45000)
    emit({'ready': True, 'title': page.title(), 'url': page.url.split('?')[0]})
    for line in sys.stdin:
        try:
            command = json.loads(line)
            action = command['action']
            if action == 'close':
                emit({'closed': True})
                break
            if not page.url.startswith('https://creator.douyin.com/'):
                raise RuntimeError('Unexpected browser origin')
            if action == 'snapshot':
                emit(driver.inspect_controls())
            elif action == 'identity':
                emit(driver.identity())
            elif action == 'upload_video':
                driver.identity()
                page.locator(driver.VIDEO).set_input_files(str(video))
                driver._wait_uploaded()
                emit({'uploaded': True})
            elif action == 'fill':
                driver._field(driver.TITLE, 'TITLE').fill(task['title'])
                driver._field(driver.CAPTION, 'CAPTION').fill(adapters.caption_text(task['description'], task['tags']))
                emit({'filled': True})
            elif action in ('inspect', 'click', 'upload_cover'):
                locator = page.locator(command['selector']) if command.get('selector') else page.get_by_text(command['text'], exact=True)
                if action == 'inspect':
                    emit(locator.evaluate_all('''nodes => nodes.slice(0,15).map(e => {
                      const attrs = x => ({tag:x.tagName, cls:x.className, role:x.getAttribute('role'),
                        text:(x.innerText||'').slice(0,220), visible:x.checkVisibility({checkVisibilityCSS:true}),
                        accept:x.getAttribute('accept'), alt:x.getAttribute('alt'),
                        pointerEvents:getComputedStyle(x).pointerEvents});
                      const parents=[]; let p=e.parentElement;
                      for(let i=0;p&&p!==document.body&&i<4;i++,p=p.parentElement) parents.push(attrs(p));
                      return {...attrs(e),parents};
                    })'''))
                elif action == 'upload_cover':
                    if locator.count() != 1 or locator.get_attribute('type') != 'file':
                        raise RuntimeError('Need one file input')
                    locator.set_input_files(str(cover))
                    emit({'cover_file_set': True})
                else:
                    if locator.count() != 1:
                        raise RuntimeError('Need one unambiguous control')
                    label = locator.inner_text().strip()
                    if label in ('发布', '确认并发布', '暂存离开'):
                        raise RuntimeError('Publication actions prohibited in inspector')
                    locator.click(timeout=10000)
                    emit({'clicked': label[:100]})
            elif action == 'screenshot':
                path = Path(__file__).parent / ('封面检查-' + str(command.get('name', 'current')) + '.png')
                page.screenshot(path=str(path), full_page=True)
                emit({'screenshot': str(path)})
            else:
                raise RuntimeError('Unknown inspection action')
        except Exception as exc:
            emit({'error_type': type(exc).__name__,
                  'error': str(exc) if type(exc) is RuntimeError else 'Browser action failed; inspect controls again'})
    context.close()
