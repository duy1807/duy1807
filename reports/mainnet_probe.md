# Mainnet probe (NO_SEND — nothing signed or sent)

Time: 2026-11-21 17:34:13 UTC

| Check | Result | Detail |
|---|---|---|
| Endpoints | INFO | {'rpc': ['mainnet.helius-rpc.com', 'solana-rpc.publicnode.com'], 'sim_rpc': ['mainnet.helius-rpc.com', 'solana-rpc.publicnode.com'], 'bundle_sim_rpc': 'mainnet.helius-rpc.com', 'verify_a': 'mainnet.helius-rpc.com', 'verify_b': 'solana-rpc.publicnode.com', 'grpc': None, 'jito': 'tokyo.mainnet.block-engine.jito.wtf', 'allow_live': False, 'snapshot_max_age_ms': 600} |
| Live PumpSwap pool decoded | PASS | mint FRgvRd6qoNhBxeHtAKrw4oHNjpFciYwL1Gmo8CC2pump pool 23mX91ybroPSH1C4dQdKTvRi1twGmH34938D7Qy4NjwE token_program Token-2022 price 1.056413e-6 SOL/token MC 1029.4 SOL liq 273.4 SOL fees Fees(lp_bps=20, protocol_bps=5, creator_bps=95) virtual_quote 17564482547 slot 472224252 |
| Simulated fee payer (public address, nothing signed) | INFO | 4SNW4Jppeh1szDyjVz3feVZRYKutGzst2HHsj2hCKwb8 |
| BUY exact cost accepted on-chain | PASS | 0.01 SOL -> base_out 9353075499, cost 9999999 lamports (fees Fees(lp_bps=20, protocol_bps=5, creator_bps=95)), received 9353075499, units 82510, attempt 1 |
| BUY cost-1 lamport rejected on-chain | PASS | err={'InstructionError': [5, {'Custom': 6004}]} |
| Sequential ×3 exact costs accepted on-chain | PASS | legs base_out [9353016460, 9351663167, 9350310167], costs [9999999, 9999999, 9999999] (rising price), units 191379 |
| Sequential ×3 leg-3 cost-1 rejected | PASS | err={'InstructionError': [7, {'Custom': 6004}]} |
| simulateBundle | PASS | summary=succeeded |
| SELL exact user_out accepted on-chain | PASS | sell 21933838919291 -> 19571617452 lamports net of fees, units 70747, attempt 1 |
| SELL user_out+1 rejected on-chain | PASS | err={'InstructionError': [2, {'Custom': 6004}]} |
| DIP/LIMIT ceiling below planned price refused (nothing built) | PASS | planned 1.069151e-6 SOL/token, ceiling ×0.999 |
| Slippage 100% capped by ceiling on-chain | PASS | max_in 9999999 (100% slippage would allow 20000000), ceiling bound 9999999 |
| E2E BUY NGAY ×1 (signal→Jito ACK) | PASS | p50 287 ms, max 1240 ms (target < 500 ms) |
| E2E BUY NGAY ×3 (signal→Jito ACK) | PASS | p50 294 ms, max 980 ms (target < 500 ms) |
| E2E BUY NGAY ×5 (signal→Jito ACK) | PASS | p50 299 ms, max 983 ms (target < 500 ms) |
| E2E DIP/LIMIT ×1 (signal→Jito ACK) | PASS | p50 229 ms, max 290 ms (target < 500 ms) |
| E2E DIP/LIMIT ×3 (signal→Jito ACK) | PASS | p50 231 ms, max 840 ms (target < 500 ms) |
| E2E DIP/LIMIT ×5 (signal→Jito ACK) | PASS | p50 241 ms, max 679 ms (target < 500 ms) |

## Latency (ms, warm connections, from the machine that ran this script)

| Stage | p50 | min | max | n |
|---|---|---|---|---|
| snapshot(getMultipleAccounts) | 49.8 | 46.7 | 56.2 | 17 |
| simulateTransaction | 51.5 | 48.0 | 1111.9 | 18 |
| simulateBundle | 55.6 | 55.6 | 55.6 | 1 |
| 429 responses | 0.0 | 0.0 | 0.0 | 2 |
| node behind snapshot slot (-32016) | 0.0 | 0.0 | 0.0 | 2 |
| RPC getSlot (warm) | 42.8 | 39.8 | 140.3 | 10 |
| userAccounts(getMultipleAccounts) | 51.2 | 46.0 | 61.4 | 10 |
| blockhash(cached) | 0.0 | 0.0 | 0.0 | 10 |
| plan ×1 | 0.1 | 0.1 | 0.1 | 10 |
| build+sign ×1 | 0.3 | 0.3 | 0.4 | 10 |
| plan ×3 | 0.1 | 0.1 | 0.1 | 10 |
| build+sign ×3 | 0.6 | 0.5 | 0.9 | 10 |
| plan ×5 | 0.1 | 0.1 | 0.2 | 10 |
| build+sign ×5 | 0.9 | 0.9 | 1.1 | 10 |
| Jito Tokyo RTT (warm) | 177.8 | 177.3 | 178.0 | 10 |
| E2E BUY NGAY ×1 (signal→Jito ACK) | 287.4 | 279.1 | 1240.2 | 8 |
|   · BUY NGAY ×1: data | 55.6 | 50.7 | 352.1 | 8 |
|   · BUY NGAY ×1: plan+build | 0.4 | 0.3 | 0.4 | 8 |
|   · BUY NGAY ×1: simulate | 48.9 | 47.4 | 92.0 | 8 |
|   · BUY NGAY ×1: Jito | 181.5 | 178.5 | 840.0 | 8 |
| E2E BUY NGAY ×3 (signal→Jito ACK) | 294.2 | 283.9 | 980.3 | 8 |
|   · BUY NGAY ×3: data | 55.0 | 49.5 | 60.8 | 8 |
|   · BUY NGAY ×3: plan+build | 0.7 | 0.6 | 0.7 | 8 |
|   · BUY NGAY ×3: simulate | 52.9 | 50.6 | 748.0 | 8 |
|   · BUY NGAY ×3: Jito | 180.3 | 178.7 | 188.3 | 8 |
| E2E BUY NGAY ×5 (signal→Jito ACK) | 299.2 | 286.6 | 983.3 | 8 |
|   · BUY NGAY ×5: data | 55.3 | 53.4 | 150.1 | 8 |
|   · BUY NGAY ×5: plan+build | 1.0 | 0.9 | 1.3 | 8 |
|   · BUY NGAY ×5: simulate | 56.0 | 51.9 | 749.7 | 8 |
|   · BUY NGAY ×5: Jito | 179.3 | 178.3 | 180.4 | 8 |
| E2E DIP/LIMIT ×1 (signal→Jito ACK) | 229.2 | 226.4 | 289.8 | 8 |
|   · DIP/LIMIT ×1: data | 0.0 | 0.0 | 46.4 | 8 |
|   · DIP/LIMIT ×1: plan+build | 0.3 | 0.3 | 0.4 | 8 |
|   · DIP/LIMIT ×1: simulate | 50.0 | 47.6 | 54.0 | 8 |
|   · DIP/LIMIT ×1: Jito | 178.5 | 178.1 | 194.2 | 8 |
| E2E DIP/LIMIT ×3 (signal→Jito ACK) | 231.1 | 229.9 | 839.6 | 8 |
|   · DIP/LIMIT ×3: data | 0.0 | 0.0 | 49.9 | 8 |
|   · DIP/LIMIT ×3: plan+build | 0.7 | 0.7 | 0.8 | 8 |
|   · DIP/LIMIT ×3: simulate | 51.7 | 51.1 | 54.1 | 8 |
|   · DIP/LIMIT ×3: Jito | 178.5 | 178.0 | 784.8 | 8 |
| E2E DIP/LIMIT ×5 (signal→Jito ACK) | 241.2 | 230.8 | 679.3 | 8 |
|   · DIP/LIMIT ×5: data | 0.0 | 0.0 | 48.7 | 8 |
|   · DIP/LIMIT ×5: plan+build | 1.0 | 1.0 | 2.3 | 8 |
|   · DIP/LIMIT ×5: simulate | 53.5 | 51.4 | 65.6 | 8 |
|   · DIP/LIMIT ×5: Jito | 179.7 | 178.2 | 623.2 | 8 |
