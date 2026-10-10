import { mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build, version as esbuildVersion } from 'esbuild';
import { createRequire } from 'node:module';

if (process.versions.node !== '24.19.0') throw new Error('Build requires pinned Node 24.19.0');
if (esbuildVersion !== '0.28.2') throw new Error('Build requires pinned esbuild 0.28.2');
const require = createRequire(import.meta.url);
if (require('agent-messenger/package.json').version !== '2.37.1') throw new Error('Build requires pinned agent-messenger 2.37.1');

const bridgeRoot = dirname(fileURLToPath(import.meta.url));
const distRoot = join(bridgeRoot, 'dist');
await mkdir(distRoot, { recursive: true });

const shared = {
  bundle: true,
  format: 'esm',
  platform: 'node',
  target: 'node24',
  minify: true,
  sourcemap: false,
  legalComments: 'eof',
  logLevel: 'info',
  absWorkingDir: bridgeRoot,
};

await Promise.all([
  build({ ...shared, entryPoints: [join(bridgeRoot, 'src', 'bridge.mjs')], outfile: join(distRoot, 'spoiler-bridge.mjs') }),
  build({ ...shared, entryPoints: [join(bridgeRoot, 'src', 'auth.mjs')], outfile: join(distRoot, 'spoiler-auth.mjs') }),
]);
