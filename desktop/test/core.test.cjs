const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const http = require('node:http')
const { createLocalServer } = require('../core.cjs')

test('local frontend serves production files, blocks traversal and reports backend startup', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'norn-desktop-'))
  await fs.writeFile(path.join(directory, 'index.html'), '<h1>NORN</h1>')
  await fs.writeFile(path.join(directory, 'model.wasm'), 'wasm')
  const local = await createLocalServer(directory)
  try {
    const index = await fetch(local.origin)
    assert.equal(await index.text(), '<h1>NORN</h1>')
    assert.match(index.headers.get('content-security-policy'), /wasm-unsafe-eval/)
    assert.equal((await fetch(local.origin + '/model.wasm')).headers.get('content-type'), 'application/wasm')
    assert.equal((await fetch(local.origin + '/api/status')).status, 503)
    assert.equal((await fetch(local.origin + '/..%2foutside.txt')).status, 403)
    assert.equal((await fetch(local.origin + '/missing.js')).status, 404)
    assert.throws(() => local.setBackend('https://example.com'), /loopback/)
  } finally {
    await local.close()
    await fs.rm(directory, { recursive: true, force: true })
  }
})

test('API proxy forwards requests without exposing arbitrary network targets', async () => {
  const upstream = http.createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json')
    response.end(JSON.stringify({ url: request.url, method: request.method }))
  })
  await new Promise((resolve) => upstream.listen(0, '127.0.0.1', resolve))
  const local = await createLocalServer(os.tmpdir())
  try {
    local.setBackend(`http://127.0.0.1:${upstream.address().port}`)
    const response = await fetch(local.origin + '/api/status?check=1', { method: 'POST', body: 'test' })
    assert.deepEqual(await response.json(), { url: '/api/status?check=1', method: 'POST' })
    const absolute = await new Promise((resolve, reject) => {
      const request = http.request({ hostname: '127.0.0.1', port: new URL(local.origin).port,
        path: 'http://example.invalid/api/status?absolute=1' }, (result) => {
        let body = ''
        result.on('data', (data) => { body += data })
        result.on('end', () => resolve(JSON.parse(body)))
      })
      request.on('error', reject)
      request.end()
    })
    assert.deepEqual(absolute, { url: '/api/status?absolute=1', method: 'GET' })
  } finally {
    await local.close()
    await new Promise((resolve) => upstream.close(resolve))
  }
})