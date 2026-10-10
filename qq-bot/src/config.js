import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const envPath = path.join(__dirname, '..', '.env');

const env = {};
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
    if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
}

export const APP_ID = process.env.APP_ID || env.APP_ID;
export const APP_SECRET = process.env.APP_SECRET || env.APP_SECRET;
// 正式环境:https://api.sgroup.qq.com ;沙箱(未过审):https://sandbox.api.sgroup.qq.com
export const BASE = process.env.BASE || 'https://api.sgroup.qq.com';
