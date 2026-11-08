"""Jalankan layanan: `astria-gateway` atau `python -m astria_gateway`."""

from astria_common.app import run

from astria_gateway.main import build_app
from astria_gateway.settings import Settings


def main() -> None:
    settings = Settings()
    run(build_app(settings), settings)


if __name__ == "__main__":
    main()
