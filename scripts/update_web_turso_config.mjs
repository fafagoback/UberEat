import fs from 'node:fs';

const file = process.argv[2] || 'web/config.js';
const databaseUrl = String(process.env.TURSO_DATABASE_URL || '').replace(/^libsql:\/\//, 'https://');
const readonlyToken = process.env.TURSO_READONLY_TOKEN;

if (!databaseUrl || !readonlyToken) {
  throw new Error('TURSO_DATABASE_URL and TURSO_READONLY_TOKEN are required');
}

let source = fs.readFileSync(file, 'utf8');
const urlPattern = /TURSO_DATABASE_URL:\s*'[^']*'/;
const tokenPattern = /TURSO_READONLY_TOKEN:\s*'[^']*'/;
if (!urlPattern.test(source) || !tokenPattern.test(source)) {
  throw new Error(`Refusing to publish: ${file} does not contain the expected Turso config fields`);
}
source = source.replace(urlPattern, `TURSO_DATABASE_URL: '${databaseUrl}'`);
source = source.replace(tokenPattern, `TURSO_READONLY_TOKEN: '${readonlyToken}'`);
fs.writeFileSync(file, source);
console.log(`Updated ${file} to use ${databaseUrl}`);
