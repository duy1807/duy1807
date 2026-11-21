
## soak resume 2026-11-21 23:02:55 UTC

```json
{
 "mode": "resume",
 "seconds": 60,
 "boot_ms": 1545,
 "snapshots_fresh_pct": 100.0,
 "snapshot_age_p50_ms": 207.6,
 "snapshot_age_max_ms": 349.5,
 "rpc_calls_per_s": 8.28,
 "rpc": {
  "mainnet.helius-rpc.com": {
   "calls": 364,
   "429": 31
  },
  "solana-rpc.publicnode.com": {
   "calls": 133,
   "429": 0
  }
 },
 "poll": {
  "ticks": 201,
  "calls": 201,
  "errors": 0
 },
 "orders": [
  {
   "id": "1",
   "kind": "LIMIT",
   "count": "3",
   "status": "ARMED",
   "trigger_price": "0.00001404102617234487474668727484",
   "reference_price": "0.00002808205234468974949337454967",
   "status_reason": null,
   "wait_note": null,
   "armed_at": "1795302015.56887",
   "triggered_at": null
  },
  {
   "id": "2",
   "kind": "LIMIT",
   "count": "1",
   "status": "ARMED",
   "trigger_price": "0.00002816629850172381874185467332",
   "reference_price": "0.00002808205234468974949337454967",
   "status_reason": null,
   "wait_note": "leg 1/1: gi\u00e1 th\u1ef1c hi\u1ec7n d\u1ef1 ki\u1ebfn 0.000028301705 > tr\u1ea7n 0.000028166299",
   "armed_at": "1795302015.5693977",
   "triggered_at": null
  },
  {
   "id": "3",
   "kind": "DIP",
   "count": "5",
   "status": "ARMED",
   "trigger_price": "0.00002799780618765568024489442602",
   "reference_price": "0.00002808205234468974949337454967",
   "status_reason": null,
   "wait_note": null,
   "armed_at": "1795302015.5697331",
   "triggered_at": null
  },
  {
   "id": "4",
   "kind": "LIMIT",
   "count": "3",
   "status": "ARMED",
   "trigger_price": "0.000003957246487356129471731131476",
   "reference_price": "0.000007914492974712258943462262953",
   "status_reason": null,
   "wait_note": null,
   "armed_at": "1795302015.7311559",
   "triggered_at": null
  },
  {
   "id": "5",
   "kind": "LIMIT",
   "count": "1",
   "status": "ARMED",
   "trigger_price": "0.000007938236453636395720292649742",
   "reference_price": "0.000007914492974712258943462262953",
   "status_reason": null,
   "wait_note": "leg 1/1: gi\u00e1 th\u1ef1c hi\u1ec7n d\u1ef1 ki\u1ebfn 0.000007994456 > tr\u1ea7n 0.000007938236",
   "armed_at": "1795302015.7315958",
   "triggered_at": null
  },
  {
   "id": "6",
   "kind": "DIP",
   "count": "5",
   "status": "ARMED",
   "trigger_price": "0.000007890749495788122166631876164",
   "reference_price": "0.000007914492974712258943462262953",
   "status_reason": null,
   "wait_note": null,
   "armed_at": "1795302015.7318678",
   "triggered_at": null
  },
  {
   "id": "7",
   "kind": "LIMIT",
   "count": "3",
   "status": "ARMED",
   "trigger_price": "4.820463009909073034992407349E-7",
   "reference_price": "9.640926019818146069984814698E-7",
   "status_reason": null,
   "wait_note": null,
   "armed_at": "1795302015.8940046",
   "triggered_at": null
  },
  {
   "id": "8",
   "kind": "LIMIT",
   "count": "1",
   "status": "FAILED_SAFE",
   "trigger_price": "9.669848797877600508194769142E-7",
   "reference_price": "9.640926019818146069984814698E-7",
   "status_reason": "RiskReject: S\u1ed1 d\u01b0 kh\u00f4ng \u0111\u1ee7: c\u1ea7n 0.038117 SOL (g\u1ed3m ph\u00ed, tip, rent, d\u1ef1 tr\u1eef), c\u00f3 0.000000",
   "wait_note": null,
   "armed_at": "1795302015.8942432",
   "triggered_at": "1795302152.0698662"
  },
  {
   "id": "9",
   "kind": "DIP",
   "count": "5",
   "status": "FAILED_SAFE",
   "trigger_price": "9.612003241758691631774860254E-7",
   "reference_price": "9.640926019818146069984814698E-7",
   "status_reason": "RiskReject: S\u1ed1 d\u01b0 kh\u00f4ng \u0111\u1ee7: c\u1ea7n 0.079699 SOL (g\u1ed3m ph\u00ed, tip, rent, d\u1ef1 tr\u1eef), c\u00f3 0.000000",
   "wait_note": null,
   "armed_at": "1795302015.894472",
   "triggered_at": "1795302152.0704587"
  },
  {
   "id": "10",
   "kind": "LIMIT",
   "count": "1",
   "status": "FAILED_SAFE",
   "trigger_price": "0.00002948417475843386433698727377",
   "reference_price": "0.00002808016643660368032094026073",
   "status_reason": "RiskReject: S\u1ed1 d\u01b0 kh\u00f4ng \u0111\u1ee7: c\u1ea7n 0.038081 SOL (g\u1ed3m ph\u00ed, tip, rent, d\u1ef1 tr\u1eef), c\u00f3 0.000000",
   "wait_note": null,
   "armed_at": "1795302115.5227659",
   "triggered_at": "1795302115.523776"
  },
  {
   "id": "11",
   "kind": "LIMIT",
   "count": "3",
   "status": "FAILED_SAFE",
   "trigger_price": "0.00002948417475843386433698727377",
   "reference_price": "0.00002808016643660368032094026073",
   "status_reason": "RiskReject: S\u1ed1 d\u01b0 kh\u00f4ng \u0111\u1ee7: c\u1ea7n 0.058963 SOL (g\u1ed3m ph\u00ed, tip, rent, d\u1ef1 tr\u1eef), c\u00f3 0.000000",
   "wait_note": null,
   "armed_at": "1795302115.5230968",
   "triggered_at": "1795302115.5245833"
  },
  {
   "id": "12",
   "kind": "LIMIT",
   "count": "5",
   "status": "FAILED_SAFE",
   "trigger_price": "0.00002948417475843386433698727377",
   "reference_price": "0.00002808016643660368032094026073",
   "status_reason": "RiskReject: S\u1ed1 d\u01b0 kh\u00f4ng \u0111\u1ee7: c\u1ea7n 0.079845 SOL (g\u1ed3m ph\u00ed, tip, rent, d\u1ef1 tr\u1eef), c\u00f3 0.000000",
   "wait_note": null,
   "armed_at": "1795302115.5233266",
   "triggered_at": "1795302115.5249412"
  }
 ],
 "armed_to_trigger_ms": [
  136176,
  136176,
  1,
  1,
  2
 ]
}
```

## soak start 2026-11-21 23:04:54 UTC

```json
{
 "mode": "start",
 "seconds": 60,
 "boot_ms": 1195,
 "snapshots_fresh_pct": 100.0,
 "snapshot_age_p50_ms": 197.6,
 "snapshot_age_max_ms": 355.7,
 "rpc_calls_per_s": 9.98,
 "rpc": {
  "mainnet.helius-rpc.com": {
   "calls": 373,
   "429": 12
  },
  "solana-rpc.publicnode.com": {
   "calls": 226,
   "429": 0
  }
 },
 "poll": {
  "ticks": 201,
  "calls": 201,
  "errors": 0
 },
 "orders": [
  {
   "id": "1",
   "kind": "LIMIT",
   "count": "3",
   "status": "ARMED",
   "trigger_price": "4.315321184972482053385227402E-7",
   "reference_price": "8.630642369944964106770454804E-7",
   "status_reason": null,
   "wait_note": null,
   "armed_at": "1795302233.7422924",
   "triggered_at": null
  },
  {
   "id": "2",
   "kind": "LIMIT",
   "count": "1",
   "status": "FAILED_SAFE",
   "trigger_price": "8.656534297054798999090766168E-7",
   "reference_price": "8.630642369944964106770454804E-7",
   "status_reason": "RiskReject: S\u1ed1 d\u01b0 kh\u00f4ng \u0111\u1ee7: c\u1ea7n 0.037683 SOL (g\u1ed3m ph\u00ed, tip, rent, d\u1ef1 tr\u1eef), c\u00f3 0.000000",
   "wait_note": null,
   "armed_at": "1795302233.7426763",
   "triggered_at": "1795302240.1502428"
  },
  {
   "id": "3",
   "kind": "DIP",
   "count": "5",
   "status": "FAILED_SAFE",
   "trigger_price": "8.60475044283512921445014344E-7",
   "reference_price": "8.630642369944964106770454804E-7",
   "status_reason": "RiskReject: S\u1ed1 d\u01b0 kh\u00f4ng \u0111\u1ee7: c\u1ea7n 0.077876 SOL (g\u1ed3m ph\u00ed, tip, rent, d\u1ef1 tr\u1eef), c\u00f3 0.000000",
   "wait_note": null,
   "armed_at": "1795302233.7428594",
   "triggered_at": "1795302261.2044132"
  },
  {
   "id": "4",
   "kind": "LIMIT",
   "count": "3",
   "status": "ARMED",
   "trigger_price": "0.001796859949861158018047643219",
   "reference_price": "0.003593719899722316036095286438",
   "status_reason": null,
   "wait_note": null,
   "armed_at": "1795302233.8999166",
   "triggered_at": null
  },
  {
   "id": "5",
   "kind": "LIMIT",
   "count": "1",
   "status": "ARMED",
   "trigger_price": "0.003604501059421482984203572297",
   "reference_price": "0.003593719899722316036095286438",
   "status_reason": null,
   "wait_note": "leg 1/1: gi\u00e1 th\u1ef1c hi\u1ec7n d\u1ef1 ki\u1ebfn 0.003605388037 > tr\u1ea7n 0.003604501059",
   "armed_at": "1795302233.9001615",
   "triggered_at": null
  },
  {
   "id": "6",
   "kind": "DIP",
   "count": "5",
   "status": "ARMED",
   "trigger_price": "0.003582938740023149087987000579",
   "reference_price": "0.003593719899722316036095286438",
   "status_reason": null,
   "wait_note": null,
   "armed_at": "1795302233.9003716",
   "triggered_at": null
  },
  {
   "id": "7",
   "kind": "LIMIT",
   "count": "3",
   "status": "ARMED",
   "trigger_price": "0.000998631123015952970008587278",
   "reference_price": "0.001997262246031905940017174556",
   "status_reason": null,
   "wait_note": null,
   "armed_at": "1795302234.070916",
   "triggered_at": null
  },
  {
   "id": "8",
   "kind": "LIMIT",
   "count": "1",
   "status": "ARMED",
   "trigger_price": "0.00200325403277000165783722608",
   "reference_price": "0.001997262246031905940017174556",
   "status_reason": null,
   "wait_note": "leg 1/1: gi\u00e1 th\u1ef1c hi\u1ec7n d\u1ef1 ki\u1ebfn 0.002003573173 > tr\u1ea7n 0.002003254033",
   "armed_at": "1795302234.0711825",
   "triggered_at": null
  },
  {
   "id": "9",
   "kind": "DIP",
   "count": "5",
   "status": "ARMED",
   "trigger_price": "0.001991270459293810222197123032",
   "reference_price": "0.001997262246031905940017174556",
   "status_reason": null,
   "wait_note": null,
   "armed_at": "1795302234.0714068",
   "triggered_at": null
  }
 ],
 "armed_to_trigger_ms": [
  6408,
  27462
 ]
}
```
