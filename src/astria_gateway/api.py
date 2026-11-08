"""Endpoint internal gateway (`/internal/v1`).

Setiap endpoint wajib token layanan: tambahkan parameter
`caller: ServicePrincipal = Depends(require_service("svc:gateway"))` dari `astria_common.auth`.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/internal/v1", tags=["gateway"])
