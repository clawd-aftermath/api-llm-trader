"""Basic health checks for API LLM Trader runtime dependencies."""

from pathlib import Path

from _api import _requests, get_aftermath_host, get_sui_rpc_url
from _cli import output, error
from _paths import credentials_path


def main():
    try:
        creds = credentials_path()
        mode = None
        if creds.exists():
            mode = oct(creds.stat().st_mode & 0o777)

        output(
            {
                "ok": True,
                "aftermathHost": get_aftermath_host(),
                "suiRpcUrl": get_sui_rpc_url(),
                "credentialsPath": str(creds),
                "credentialsExists": creds.exists(),
                "credentialsMode": mode,
                "dataDirExists": Path(creds).parent.exists(),
                "requestsInstalled": _requests is not None,
            }
        )
    except Exception as exc:
        error(str(exc))


if __name__ == "__main__":
    main()
