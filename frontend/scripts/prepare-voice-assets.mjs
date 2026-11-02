import { mkdir, copyFile, readdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const destination = path.join(root, 'public/voice')
await mkdir(destination, { recursive: true })
const vad = path.join(root, 'node_modules/@ricky0123/vad-web/dist')
for (const name of ['vad.worklet.bundle.min.js', 'silero_vad_v5.onnx']) {
  await copyFile(path.join(vad, name), path.join(destination, name))
}
const runtime = path.join(root, 'node_modules/onnxruntime-web/dist')
for (const name of await readdir(runtime)) {
  if (name.startsWith('ort-wasm') && /\.(wasm|mjs)$/.test(name)) await copyFile(path.join(runtime, name), path.join(destination, name))
}
console.log('Local VAD assets ready in public/voice')