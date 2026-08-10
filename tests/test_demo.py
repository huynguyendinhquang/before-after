from __future__ import annotations

from pathlib import Path

import pytest

from app import create_app


def test_demo_auto_login_is_development_only(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Demo Mode"):
        create_app(
            {
                "APP_ENV": "production",
                "DATABASE_URL": "postgresql+psycopg://localhost/example",
                "MEDIA_ROOT": str(tmp_path / "media"),
                "SECRET_KEY": "test-secret",
                "DEMO_AUTO_LOGIN": True,
            }
        )
