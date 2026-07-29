from fastapi import APIRouter, Depends, Query, Response, status

from app.api.schemas import ProviderCreate, ProviderKind, ProviderUpdate
from app.dependencies import get_provider_service
from app.services.provider_service import ProviderService

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("")
def list_providers(
    kind: ProviderKind | None = Query(default=None),
    service: ProviderService = Depends(get_provider_service),
) -> list[dict]:
    return service.list(kind)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_provider(
    body: ProviderCreate,
    service: ProviderService = Depends(get_provider_service),
) -> dict:
    return service.create(body.model_dump())


@router.put("/{provider_id}")
def update_provider(
    provider_id: str,
    body: ProviderUpdate,
    service: ProviderService = Depends(get_provider_service),
) -> dict:
    return service.update(provider_id, body.model_dump(exclude_unset=True))


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(
    provider_id: str,
    service: ProviderService = Depends(get_provider_service),
) -> Response:
    service.delete(provider_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{provider_id}/test")
def test_provider(
    provider_id: str,
    service: ProviderService = Depends(get_provider_service),
) -> dict:
    return service.test(provider_id)
