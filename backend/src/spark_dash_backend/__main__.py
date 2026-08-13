"""Entry point: `python -m spark_dash_backend`."""

from __future__ import annotations

import uvicorn

from spark_dash_backend.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "spark_dash_backend.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
