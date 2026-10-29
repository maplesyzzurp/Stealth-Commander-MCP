import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

await build({
  absWorkingDir: fileURLToPath(new URL('../', import.meta.url)),
  entryPoints: ['web/App.jsx'],
  outfile: 'build/site/app.js',
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: ['es2020'],
  jsx: 'automatic',
  define: { 'process.env.NODE_ENV': '"production"' },
  minify: true,
  sourcemap: false,
  legalComments: 'eof',
  logLevel: 'info'
});
