import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

const options = {
  absWorkingDir: fileURLToPath(new URL('../', import.meta.url)),
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: ['es2020'],
  jsx: 'automatic',
  define: { 'process.env.NODE_ENV': '"production"' },
  minify: true,
  sourcemap: false,
  external: ['/jdm-stickerwall.svg'],
  legalComments: 'eof',
  logLevel: 'info'
};

await Promise.all([
  build({ ...options, entryPoints: ['web/App.jsx'], outfile: 'build/site/app.js' }),
  build({ ...options, entryPoints: ['web/DashboardFlow.jsx'], outfile: 'build/site/dashboard-flow.js' })
]);
