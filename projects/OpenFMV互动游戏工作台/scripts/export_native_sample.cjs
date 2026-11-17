const fs = require('node:fs/promises');
const path = require('node:path');

async function main() {
  const [modulePath, inputPath, outputPath] = process.argv.slice(2);
  if (!modulePath || !inputPath || !outputPath) {
    throw new Error('需要提供原导出模块、项目文件与独立输出目录');
  }
  const { saveProjectToDirectory } = require(path.resolve(modulePath));
  const project = JSON.parse(await fs.readFile(inputPath, 'utf8'));
  await saveProjectToDirectory(project, path.resolve(outputPath));
  console.log('原工程已保存项目主文件及实际媒体');
}

main().catch((failure) => {
  process.stderr.write(`${failure.message}\n`);
  process.exitCode = 1;
});
