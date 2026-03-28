/**
 * Sign a challenge message for MoltMart registration.
 * Usage: PRIVATE_KEY=0x... node sign-challenge.mjs "challenge message"
 */

import { privateKeyToAccount } from 'viem/accounts';

const challenge = process.argv[2] || "MoltMart Registration: I own this wallet and have an ERC-8004 identity";

const privateKey = process.env.PRIVATE_KEY;
if (!privateKey) {
  console.error("Error: PRIVATE_KEY env var required");
  console.error("Usage: PRIVATE_KEY=0x... node sign-challenge.mjs \"challenge message\"");
  process.exit(1);
}

const account = privateKeyToAccount(privateKey);

console.log(`Wallet: ${account.address}`);
console.log(`Challenge: "${challenge}"`);

const signature = await account.signMessage({ message: challenge });
console.log(`Signature: ${signature}`);
