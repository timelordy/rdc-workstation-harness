import fs from 'node:fs';

// Installed runtime first, then the source checkout. The installer copies VERSION.
export function readVersion(base = import.meta.url) {
  for (const relative of ['../VERSION', '../../../VERSION']) {
    try {
      const version = fs.readFileSync(new URL(relative, base), 'utf8').trim();
      if (/^\d+\.\d+\.\d+$/.test(version)) return version;
    } catch { /* Try the other supported layout. */ }
  }
  return 'unknown';
}

export const runtimeVersion = readVersion();
