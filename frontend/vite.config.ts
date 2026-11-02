import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { readFile } from 'node:fs/promises'
import path from 'node:path'

// https://vite.dev/config/
const apiTarget = process.env.NORN_API_URL ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), {
    name: 'local-voice-wasm-modules',
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        const pathname = new URL(request.url ?? '/', 'http://localhost').pathname
        if (!/^\/voice\/ort-wasm[\w-]*\.mjs$/.test(pathname)) return next()
        try {
          const contents = await readFile(path.join(server.config.publicDir, pathname.slice(1)))
          response.setHeader('Content-Type', 'text/javascript')
          response.end(contents)
        } catch (error) { next(error) }
      })
    },
  }],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': { target: apiTarget, ws: true },
    },
  },
})
