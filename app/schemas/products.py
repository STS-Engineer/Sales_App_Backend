from typing import Any

from pydantic import BaseModel


class ProductListResponse(BaseModel):
    """Response schema for GET /api/products (operationId: retrieveProducts)."""

    products: list[dict[str, Any]]


class ProductLineResponse(BaseModel):
    """Response schema for GET /api/product-lines (operationId: retrieveProductLine)."""

    productLine: list[dict[str, Any]]
