# Write Response Schemas (`trade.py`)

## Common success envelope

All write commands return the API request, native transaction preview, and Sui execution result:
```json
{"ok":true,"endpoint":"/api/perpetuals/account/transactions/place-limit-order","request":{...},"preview":{"txKind":"...","sponsorSignature":null},"execution":{"digest":"...","effects":{...},"events":[...]}}
```

## order market / order limit
```json
{"symbol":"BTCUSD","marketId":"0x...","ok":true,"request":{"side":0,"size":"10000000n","price":"77877800000000n"},"execution":{"digest":"..."}}
```

## order cancel
```json
{"symbol":"BTCUSD","marketId":"0x...","ok":true,"request":{"marketIdsToData":{"0x...":{"orderIds":["123n","456n"]}}},"execution":{"digest":"..."}}
```

## order cancel-and-place
```json
{"symbol":"BTCUSD","marketId":"0x...","ok":true,"request":{"orderIdsToCancel":["123n"],"ordersToPlace":[{"side":0,"price":"77877800000000n","size":"10000000n"}]},"execution":{"digest":"..."}}
```

## position leverage
```json
{"symbol":"BTCUSD","marketId":"0x...","ok":true,"request":{"leverage":10.0},"execution":{"digest":"..."}}
```

## funds deposit / funds withdraw
```json
{"ok":true,"request":{"depositAmount":100000000},"execution":{"digest":"..."}}
{"ok":true,"request":{"withdrawAmount":100000000,"recipientAddress":"0x..."},"execution":{"digest":"..."}}
```

## Error shape
```json
{"error":"descriptive message"}
```
