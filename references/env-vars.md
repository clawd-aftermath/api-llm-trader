# Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `AFTERMATH_PRIVATE_KEY` | Writes | — | Sui Ed25519 private key (`suiprivkey1...`) |
| `AFTERMATH_WALLET_ADDRESS` | Writes | derived from key | Sui wallet address (`0x...`) |
| `AFTERMATH_ACCOUNT_ID` | Writes | — | Numeric perpetuals account ID |
| `AFTERMATH_ACCOUNT_CAP_ID` | Optional | — | Account capability Sui object ID |
| `AFTERMATH_HOST` | No | `https://v2-preview.aftermath.finance` | API host |
| `SUI_RPC_URL` | No | auto | Sui fullnode JSON-RPC URL |
| `AFTERMATH_PAPER_STATE_PATH` | No | `~/.aftermath/api-llm-trader/paper-state.json` | Paper state file |

## Credentials File

Location: `~/.aftermath/api-llm-trader/credentials`

Format (shell-style):
```
AFTERMATH_HOST=https://v2-preview.aftermath.finance
AFTERMATH_WALLET_ADDRESS=0x...
AFTERMATH_ACCOUNT_ID=123
AFTERMATH_ACCOUNT_CAP_ID=0x...
AFTERMATH_PRIVATE_KEY=suiprivkey1...
```

Must be mode `0600`. Run `./aftermath-config` to create it interactively.

Environment variables take precedence over the credentials file.
