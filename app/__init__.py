import os
from datetime import datetime, timezone

from flask import Flask


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.environ.get(
            "APP_DB", os.path.join(app.instance_path, "triage.db")
        ),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 单个批次请求上限 16MB
    )
    if config:
        app.config.update(config)

    os.makedirs(app.instance_path, exist_ok=True)
    db_dir = os.path.dirname(app.config["DATABASE"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    from . import db as db_module

    db_module.init_app(app)

    from .views import bp

    app.register_blueprint(bp)

    @app.template_filter("utc")
    def fmt_utc(ts):
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
