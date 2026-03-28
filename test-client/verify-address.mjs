import { privateKeyToAccount } from 'viem/accounts';
import { readFileSync } from 'fs';

const pk = readFileSync(process.env.HOME + '/.openclaw/credentials/.kyro-wallet-key', 'utf8').trim();
const account = privateKeyToAccount(pk);
console.log('Address:', account.address);
