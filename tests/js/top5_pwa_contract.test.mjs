import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const viewsSource = readFileSync(resolve(__dir, '../../docs/js/views.js'), 'utf8');

test('PWA exposes a keyboard-accessible Top-5 league filter for all canonical leagues', () => {
  assert.match(viewsSource, /\['top5',\s*'⭐ Top-5'\]/);
  assert.match(viewsSource, /filter\.liga === 'top5'/);
  for (const code of ['epl', 'bl1', 'll', 'sa', 'l1']) {
    assert.match(viewsSource, new RegExp(`['"]${code}['"]`));
  }
  assert.match(viewsSource, /role="tab"/);
  assert.match(viewsSource, /keydown/);
});
