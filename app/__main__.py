"""Development entry point for the application factory."""

from __future__ import annotations

import os

from app import create_app


if __name__ == "__main__":
    application = create_app()
    application.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8765")),
        debug=application.debug,
    )
