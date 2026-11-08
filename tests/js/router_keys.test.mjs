/* Router API keys in Settings: the id the backend uses and the key check,
 * printed as JSON for tests/test_router_keys_js.py. */

import { checkKey, keyId } from '../../frontend/src/lib/routerKeys.ts';

const out = {
  id: keyId('sparky', 8108),
  ok: checkKey('  ' + 'a'.repeat(64) + '\n'),
  empty: checkKey('   '),
  short: checkKey('abc'),
  long: checkKey('x'.repeat(513)),
  space: checkKey('has a space inside'),
  unicode: checkKey('ключ-ключ-ключ'),
};
console.log(JSON.stringify(out));
