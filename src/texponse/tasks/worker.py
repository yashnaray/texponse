from __future__ import annotations

import logging

from texponse.tasks.background import app as celery_app

logging.basicConfig(
    level="INFO",
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def cli() -> None:

    argv = ["worker", "--loglevel=INFO", "--concurrency=4", "--pool=solo"]
    celery_app.worker_main(argv)


if __name__ == "__main__":
    cli()
