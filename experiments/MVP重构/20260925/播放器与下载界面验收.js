async (page) => {
  // 只替换此浏览器页面的交付状态响应；不写后端审核、不调用模型。
  // 真实音视频由本地媒体端点返回。ZIP 是工程验收样包，不是发布资格证明。
  const root = 'C:/Users/22346/Desktop/自媒体视频生成/experiments/MVP重构/20260925';
  const workId = 'acceptance_real_preview';
  const scope = `**/api/v1/works/${workId}`;
  const media = (name) => '/api/v1/media?uri=' + encodeURIComponent('production/backend/runtime/acceptance/20260925/' + name);
  let confirmed = false;
  let confirmationPayload = null;
  const errors = [];
  const onError = (err) => errors.push(err.message);
  page.on('pageerror', onError);
  const pkg = { id: 'ui_only_verified_download', work_id: workId, task_id: 'acceptance_render_task',
    mode: 'integration', state: 'ready', title: '工程组件验收 · 既有样片',
    video_url: media('成片.mp4'), cover_url: media('封面.jpg'),
    download_url: 'http://127.0.0.1:8767/' + encodeURIComponent('播放器验收包.zip'),
    files: [], description: '未做生产人工审核', tags: [] };
  const status = () => ({ task: { id: 'acceptance_render_task', work_id: workId, mode: 'integration',
    state: confirmed ? 'succeeded' : 'waiting_selection', stage: confirmed ? 'publish_package' : 'video_review',
    pending_selection_ids: confirmed ? [] : ['sel_final_video'], stage_rows: [] },
    execution: { id: 'ui_only_execution', state: 'succeeded' },
    preview_url: media('成片.mp4'), cover_url: media('封面.jpg'), files: [], ready_for_review: true });
  await page.route(scope + '/postprocess', route => route.fulfill({json: status()}));
  await page.route(scope + '/publish-package', route => route.fulfill({json: confirmed ? pkg : null}));
  await page.route(scope + '/postprocess-confirmation', async route => {
    confirmationPayload = route.request().postDataJSON();
    if (confirmationPayload.execution_id !== 'ui_only_execution' || !confirmationPayload.review_ack) {
      return route.fulfill({status: 409, json: {error: {message: '界面没有提交当前预览版本'}}});
    }
    confirmed = true;
    await route.fulfill({json: pkg});
  });
  try {
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto('http://127.0.0.1:5174/#/video');
    await page.locator('select').first().selectOption(workId);
    const section = page.locator('#delivery');
    await section.scrollIntoViewIfNeeded();
    const video = section.getByLabel('成片预览');
    await video.evaluate(async v => {v.muted = true; await v.play();});
    await page.waitForTimeout(400);
    const playback = await video.evaluate(v => { v.pause(); return {duration:v.duration,
      currentTime:v.currentTime, width:v.videoWidth, height:v.videoHeight, readyState:v.readyState, error:v.error?.code}; });
    const button = page.getByRole('button', {name:'确认成片并生成下载包', exact:true});
    if (await button.isEnabled()) throw new Error('未勾选时不应允许确认');
    await page.getByLabel('我已观看这版成片，确认字幕、画面、声音和片尾没有问题').check();
    await button.click();
    await section.getByRole('link', {name:'下载完整作品包', exact:true}).waitFor();
    const downloadPromise = page.waitForEvent('download');
    await section.getByRole('link', {name:'下载完整作品包', exact:true}).click();
    const download = await downloadPromise;
    await download.saveAs(root + '/浏览器下载验收.zip');
    await section.scrollIntoViewIfNeeded();
    await page.screenshot({path:root + '/成片播放器与下载-工程夹具.png', fullPage:false});
    const evidence = {scope:'浏览器交付状态为显式路由夹具；视频字节真实；不代表生产审核或新模型生成',
      playback, confirmationPayload, downloadFailure:await download.failure(),
      pageErrors:errors};
    return evidence;
  } finally {
    await page.unroute(scope + '/postprocess');
    await page.unroute(scope + '/publish-package');
    await page.unroute(scope + '/postprocess-confirmation');
    page.off('pageerror', onError);
  }
}
