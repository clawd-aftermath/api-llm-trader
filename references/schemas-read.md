# Read Response Schemas (`query.py`)

## system status
```json
{"status":"ok","markets_count":1,"timestamp":1785274453,"host":"https://v2-preview.aftermath.finance"}
```

## market list
```json
{"markets":[{"symbol":"BTCUSD","market_id":"0x...","index_price":95000.5}]}
```

## market stats
```json
{"stats":[{"symbol":"BTCUSD","market_id":"0x...","index_price":95000.5,"estimated_funding_rate":-0.00008,"open_interest":"...","premium_twap":-50.5,"taker_fee":0.0005,"maker_fee":0.0002}]}
```

## market info
```json
{"markets":[{"symbol":"BTCUSD","market_id":"0x...","lot_size":"1n","tick_size":"100000n","taker_fee":0.00045,"maker_fee":-0.00005,"margin_ratio_initial":0.05,"margin_ratio_maintenance":0.025,"min_order_usd_value":1}]}
```

## market book
```json
{"symbol":"BTCUSD","market_id":"0x...","mid_price":95000.5,"best_bid":95000.0,"best_ask":95001.0,"bids":[{"size":0.01,"price":95000.0}],"asks":[{"size":0.01,"price":95001.0}],"bids_total_size":2.0,"asks_total_size":2.5}
```

## market trades
```json
{"symbol":"BTCUSD","market_id":"0x...","trades":[...]}
```

## market candles
```json
{"symbol":"BTCUSD","market_id":"0x...","resolution":"1h","candles":[...]}
```

## market funding
```json
{"funding":[{"symbol":"BTCUSD","estimated_funding_rate":-0.00008,"premium_twap":-50.5,"index_price":95000.5,"next_funding_ms":"1776978000000n","funding_frequency_ms":"3600000n","funding_period_ms":"28800000n"}]}
```

## account info
```json
{"address":"0x...","account_caps":[{"objectId":"0x...","accountId":"123n","accountObjectId":"0x..."}]}
```

## account positions
```json
{"account_id":123,"positions":[{"marketId":"0x...","baseAssetAmount":0.01,"collateralUsd":100.0,...}],"account":{...}}
```

## orders open / orders history
```json
{"account_id":123,"orders":[...]}
```

## auth status
```json
{"status":"ok","auth_capable":false,"host":"https://v2-preview.aftermath.finance","sources":{"AFTERMATH_PRIVATE_KEY":"not set",...},"credentials_file":{"path":"...","present":false},"missing":["AFTERMATH_PRIVATE_KEY",...]}
```
