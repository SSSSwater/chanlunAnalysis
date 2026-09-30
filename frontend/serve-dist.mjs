// Minimal static file server for the built frontend (frontend/dist).
// Local static server: the frontend port is fixed by project convention.
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { join, normalize, extname } from 'node:path';
import { brotliCompress, constants, gzip } from 'node:zlib';
import { promisify } from 'node:util';

const ROOT = process.env.DIST_ROOT || 'E:\\WorkingSpace\\Project\\chanlunAnalysis\\frontend\\dist';
const PORT = Number(process.env.PORT || 5173);

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.mjs': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.map': 'application/json',
};

const compressBrotli = promisify(brotliCompress);
const compressGzip = promisify(gzip);
const fileCache = new Map();

const cacheVersion = (info) => `${info.size}:${Math.trunc(info.mtimeMs)}`;

const loadCachedFile = async (filePath, info) => {
  const version = cacheVersion(info);
  const existing = fileCache.get(filePath);
  if (existing?.version === version) return existing;

  const data = await readFile(filePath);
  const entry = {
    version,
    data,
    // The size/mtime pair changes whenever Vite emits a new hashed asset and
    // avoids hashing megabyte-sized bundles on every request.
    etag: `"${info.size.toString(16)}-${Math.trunc(info.mtimeMs).toString(16)}"`,
    brPromise: null,
    gzipPromise: null,
  };
  fileCache.set(filePath, entry);
  return entry;
};

const compressedBody = async (entry, acceptEncoding) => {
  if (entry.data.length < 512) return { body: entry.data, encoding: null };
  if (acceptEncoding.includes('br')) {
    entry.brPromise ||= compressBrotli(entry.data, {
      params: { [constants.BROTLI_PARAM_QUALITY]: 4 },
    });
    return { body: await entry.brPromise, encoding: 'br' };
  }
  if (acceptEncoding.includes('gzip')) {
    entry.gzipPromise ||= compressGzip(entry.data, { level: 6 });
    return { body: await entry.gzipPromise, encoding: 'gzip' };
  }
  return { body: entry.data, encoding: null };
};

const isImmutableAsset = (urlPath, ext) => (
  urlPath.startsWith('/assets/')
  || (ext !== '.html' && ext !== '')
);

const server = createServer(async (req, res) => {
  try {
    let urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
    if (urlPath === '/health') {
      const body = Buffer.from(JSON.stringify({ status: 'ok' }));
      res.writeHead(200, {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
        'Content-Length': body.length,
        'X-Content-Type-Options': 'nosniff',
      });
      if (req.method === 'HEAD') res.end();
      else res.end(body);
      return;
    }
    if (urlPath === '/') urlPath = '/index.html';
    let filePath = normalize(join(ROOT, urlPath));
    if (!filePath.startsWith(ROOT)) {
      res.writeHead(403); res.end('Forbidden'); return;
    }
    let info;
    try { info = await stat(filePath); } catch { info = null; }
    if (!info || !info.isFile()) {
      // Never SPA-fallback asset requests: Cloudflare would cache the fake
      // 200/HTML for a year (immutable) and the site would break.
      if (urlPath.startsWith('/assets/') || /\.[a-z0-9]+$/i.test(urlPath)) {
        res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }); res.end('Not found'); return;
      }
      filePath = join(ROOT, 'index.html');
      info = await stat(filePath);
    }
    const ext = extname(filePath).toLowerCase();
    const mime = MIME[ext] || 'application/octet-stream';
    const entry = await loadCachedFile(filePath, info);

    const acceptEncoding = (req.headers['accept-encoding'] || '').toString();
    const isAsset = isImmutableAsset(urlPath, ext);
    const headers = {
      'Content-Type': mime,
      // index.html must never come from bfcache/aggressive caches so a
      // rebuilt bundle (new hashed css/js and self-hosted fonts) is picked
      // up on the next refresh; assets stay immutable.
      'Cache-Control': isAsset ? 'public, max-age=31536000, immutable' : 'no-cache, no-store, must-revalidate',
      'X-Content-Type-Options': 'nosniff',
      // Fonts and assets may load through CORS-mode stylesheet links
      // (Vite adds crossorigin) and under reverse-proxy origins.
      'Access-Control-Allow-Origin': '*',
      ETag: entry.etag,
      'Last-Modified': info.mtime.toUTCString(),
      'Accept-Ranges': 'bytes',
    };
    const ifNoneMatch = String(req.headers['if-none-match'] || '')
      .split(',')
      .map((value) => value.trim())
      .includes(entry.etag);
    if (ifNoneMatch) {
      res.writeHead(304, headers);
      res.end();
      return;
    }
    const { body, encoding } = await compressedBody(entry, acceptEncoding);
    if (encoding) {
      headers['Content-Encoding'] = encoding;
      headers.Vary = 'Accept-Encoding';
    }
    headers['Content-Length'] = body.length;
    res.writeHead(200, headers);
    if (req.method === 'HEAD') res.end();
    else res.end(body);
  } catch (err) {
    res.writeHead(500); res.end('Server error: ' + err.message);
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log('static server listening on http://127.0.0.1:' + PORT + ' root=' + ROOT);
});
