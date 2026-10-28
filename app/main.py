"""Runtime settings come from the process environment, never connection files."""

import os
import re

from fastapi import FastAPI


def create_app() -> FastAPI:
    version = os.environ.get("WEBOPS_VERSION", "dev")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version):
        raise ValueError("WEBOPS_VERSION must be 1-128 letters, digits, dots, underscores or hyphens")

    application = FastAPI(title="webops-app", docs_url=None, redoc_url=None, openapi_url=None)

    @application.get("/")
    def root() -> dict[str, str]:
        return {"service": "webops-app"}

    @application.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/version")
    def get_version() -> dict[str, str]:
        return {"version": version}

    return application


app = create_app()
