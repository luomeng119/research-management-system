# -*- coding: utf-8 -*-
import os

from app import create_app
from waitress import serve


def main() -> None:
    app = create_app()
    serve(
        app,
        host=os.environ.get("APP_BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_PORT", "5001")),
        threads=4,
    )


if __name__ == "__main__":
    # V1 is intentionally single-process: authentication throttling and active
    # AI-run cancellation are bounded in-process registries.
    main()
