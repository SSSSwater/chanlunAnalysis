import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const mode = process.argv[2] === 'preview' ? 'preview' : 'dev'
const viteCli = fileURLToPath(new URL('../node_modules/vite/bin/vite.js', import.meta.url))
const args = mode === 'preview'
  ? ['preview', '--host', '127.0.0.1', '--port', '5173', '--strictPort']
  : ['--host', '127.0.0.1', '--port', '5173', '--strictPort']

// Deliberately do not forward user-supplied CLI arguments: local ports are fixed.
const child = spawn(process.execPath, [viteCli, ...args], { stdio: 'inherit' })

const forwardSignal = (signal) => child.kill(signal)
process.on('SIGINT', () => forwardSignal('SIGINT'))
process.on('SIGTERM', () => forwardSignal('SIGTERM'))
child.on('exit', (code, signal) => {
  if (signal) process.kill(process.pid, signal)
  else process.exit(code ?? 1)
})
