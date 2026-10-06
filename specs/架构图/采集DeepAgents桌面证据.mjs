import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

const folder = path.dirname(fileURLToPath(import.meta.url));
const basename = 'DeepAgents多智能体架构';
const artifactPath = path.join(folder, `${basename}.html`);
const runtimePath = String.raw`C:\Users\22346\.agents\skills\archify\bin\visual-check.mjs`;
const source = fs.readFileSync(runtimePath, 'utf8')
  .replace('class ChromeVisualBrowser {', 'export class ChromeVisualBrowser {')
  .replace('async function evaluate(cdp,', 'export async function evaluate(cdp,');
const { ChromeVisualBrowser, evaluate, findChrome } = await import(
  'data:text/javascript;base64,' + Buffer.from(source).toString('base64')
);
const browser = new ChromeVisualBrowser(findChrome());
const screenshots = [];
try {
  for (const [width, height] of [[1600, 1000], [1920, 1080]]) {
    const filename = `${basename}.visual-check.${width}x${height}.light.png`;
    const metrics = await browser.inspect({
      artifactPath, width, height, theme: 'light',
      screenshotPath: path.join(folder, filename)
    });
    screenshots.push({ width, height, file: filename, ...metrics });
    if (metrics.scrollWidth > metrics.innerWidth || metrics.scrollHeight > metrics.innerHeight) {
      throw new Error(`Desktop overflow: ${width}x${height}`);
    }
  }
  const session = await browser.sessionPromise;
  await browser.cdp.send('Browser.setDownloadBehavior', { behavior: 'deny' });
  const exported = await evaluate(browser.cdp, session, `new Promise((resolve,reject)=>{
    const original=URL.createObjectURL.bind(URL);
    URL.createObjectURL=(blob)=>{
      if(blob.type==='image/png'){
        const reader=new FileReader();
        reader.onload=()=>resolve(reader.result);
        reader.onerror=reject;
        reader.readAsDataURL(blob);
      }
      return original(blob);
    };
    document.querySelector('button[data-format="png"]').click();
    setTimeout(()=>reject(new Error('PNG export timeout')),20000);
  })`, true);
  const png = Buffer.from(exported.split(',')[1], 'base64');
  fs.writeFileSync(path.join(folder, `${basename}.png`), png);
  const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
  const receipt = {
    artifact: { file: `${basename}.html`, sha256: sha256(fs.readFileSync(artifactPath)) },
    export: { file: `${basename}.png`, bytes: png.length, sha256: sha256(png), method: 'viewer-native-png-export' },
    screenshots,
    visualReview: 'pending'
  };
  fs.writeFileSync(path.join(folder, 'DeepAgents导出核验.json'), JSON.stringify(receipt, null, 2) + '\n');
  console.log(JSON.stringify(receipt, null, 2));
} finally {
  await browser.close();
}
