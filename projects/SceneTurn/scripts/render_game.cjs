const fs = require('node:fs/promises');
const { buildExportedRuntimeScript, createGameShellHtml } = require('../frontend/electron/exporter.js');

async function main() {
  const [input, output] = process.argv.slice(2);
  if (!input || !output) throw new Error('必须提供游戏清单和播放器文件路径');
  const game = await fs.readFile(input, 'utf8');
  await fs.writeFile(output, createGameShellHtml(game, await buildExportedRuntimeScript()), { encoding: 'utf8', flag: 'wx' });
}

main().catch((error) => { process.stderr.write(`${error.name}\n`); process.exitCode = 1; });
