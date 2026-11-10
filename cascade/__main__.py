"""Run Cascade with `python -m cascade`."""

from __future__ import annotations

import uvicorn

from .config import settings


def main() -> None:
    uvicorn.run(
        "cascade.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=settings.trust_proxy,
        forwarded_allow_ips="*" if settings.trust_proxy else None,
        log_level="info",
    )


if __name__ == "__main__":
    main()
