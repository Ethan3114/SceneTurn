const fs = require('fs');
const path = require('path');
const ts = require('typescript');

const root = path.join(__dirname, '..');
const source = path.join(root, 'shared', 'ipc-contract.ts');
const output = path.join(root, 'shared', 'ipc-contract.js');

const sourceText = fs.readFileSync(source, 'utf8');
const generated = ts.transpileModule(sourceText, {
  fileName: source,
  compilerOptions: {
    target: ts.ScriptTarget.ES2020,
    module: ts.ModuleKind.CommonJS,
    esModuleInterop: true,
  },
}).outputText;

const nextContent = `// Generated from shared/ipc-contract.ts. Run npm run ipc:build after editing the source.\n${generated}`;
if (!fs.existsSync(output) || fs.readFileSync(output, 'utf8') !== nextContent) {
  fs.writeFileSync(output, nextContent, 'utf8');
}
