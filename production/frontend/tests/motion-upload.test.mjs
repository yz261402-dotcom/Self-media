import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'

const component = await readFile(new URL('../src/components/MotionUpload.vue', import.meta.url), 'utf8')
const http = await readFile(new URL('../src/api/http.ts', import.meta.url), 'utf8')
const types = await readFile(new URL('../src/api/types.ts', import.meta.url), 'utf8')

test('自定义动作入口明确限制格式、大小、时长并要求真实预览确认', () => {
  assert.match(component, /accept="video\/mp4,video\/quicktime,\.mp4,\.mov"/)
  assert.match(component, /100 \* 1024 \* 1024/)
  assert.match(component, /localDuration\.value < 3 \|\| localDuration\.value > 60/)
  assert.match(component, /我已预览，确认有权将这段视频用于本项目的动作参考/)
  assert.match(component, /保存并用于本期/)
  assert.match(component, /只迁移头部与面部，不支持挥手等全身手势/)
})

test('动作上传使用原始字节协议并展示后端检查状态', () => {
  assert.match(http, /Content-Type', 'application\/octet-stream'/)
  assert.match(http, /X-File-Name', encodeURIComponent\(file\.name\)/)
  assert.match(http, /uploadMotion:/)
  assert.match(http, /listMotionUploads:/)
  assert.match(http, /confirmMotionUpload:/)
  assert.match(types, /export interface MotionUploadView/)
  assert.match(types, /state: 'pending' \| 'confirmed'/)
})

test('上下文切换会中止上传且不会把旧人物动作绑定到新作品', () => {
  assert.match(component, /uploadController\?\.abort\(\)/)
  assert.match(component, /store\.workContextMatches\(workId, loadVersion\)/)
  assert.match(component, /identityId\.value === personId/)
  assert.match(component, /if \(!context\.matches\(\)\) return/)
})
