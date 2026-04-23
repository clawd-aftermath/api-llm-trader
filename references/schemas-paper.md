# Paper Response Schemas (`paper.py`)

## init / reset
```json
{"status":"ok","collateral":10000.0,"state_path":"~/.aftermath/api-llm-trader/paper-state.json"}
```

## status
```json
{"status":"ok","collateral":9500.0,"initial_collateral":10000.0,"unrealized_pnl":150.5,"realized_pnl":-50.0,"total_pnl":-349.5,"positions_count":2,"trades_count":5}
```

## positions
```json
{"positions":[{"symbol":"BTCUSD","market_id":"0x...","side":"long","size":0.01,"entry_price":95000.0,"mark_price":95150.0,"unrealized_pnl":1.5,"realized_pnl":0.0}]}
```

## trades
```json
{"trades":[{"market_id":"0x...","symbol":"BTCUSD","side":"long","size":0.01,"price":95000.0,"fee":0.475,"realized_pnl":0.0,"timestamp":"2026-04-23T...Z"}]}
```

## order market / order ioc
```json
{"status":"ok","symbol":"BTCUSD","side":"long","order_type":"market","filled_size":0.01,"avg_price":95000.5,"fee":0.475,"realized_pnl":0.0,"fills_count":3}
```

## health
```json
{"status":"healthy","account_value":10150.5,"collateral":10000.0,"total_notional":950.5,"margin_usage":0.094,"leverage":0.094}
```

## liquidation_price
```json
{"symbol":"BTCUSD","market_id":"0x...","liquidation_price":85000.0,"position_side":"long","position_size":0.01,"mark_price":95000.0}
```

## refresh
```json
{"status":"ok","symbol":"BTCUSD","market_id":"0x...","mid_price":95000.5,"best_bid":95000.0,"best_ask":95001.0}
```
