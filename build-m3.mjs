import * as esbuild from 'esbuild';
import { readFileSync } from 'fs';
import { mkdirSync } from 'fs';

mkdirSync('app/web/static/fonts', { recursive: true });

const result = await esbuild.build({
  entryPoints: ['src/m3-entry.js'],
  bundle: true,
  format: 'esm',
  outfile: 'app/web/static/m3.bundle.js',
  minify: true,
  splitting: false,
  treeShaking: true,
  define: {
    'process.env.NODE_ENV': '"production"',
  },
  logLevel: 'info',
});

console.log('✅ @material/web bundled → app/web/static/m3.bundle.js');
