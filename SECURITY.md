# Security policy

Do not open a public issue containing credentials, private keys, account identifiers, order IDs,
positions, or logs with sensitive account data.

If a key may have been exposed, disable it in Kalshi immediately, stop all trader instances, cancel
open orders through an independent channel, and rotate the key. Git history is not a secret store;
deleting a file in a later commit does not remove it from earlier commits.
