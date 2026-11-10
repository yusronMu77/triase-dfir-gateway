"""Jalankan layanan: `astria-gateway` atau `python -m astria_gateway` (listener internal dan publik)."""

import asyncio

from astria_gateway.main import serve
from astria_gateway.settings import Settings


def main() -> None:
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
