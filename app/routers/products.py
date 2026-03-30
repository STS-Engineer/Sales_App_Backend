from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.validation_matrix import ValidationMatrix
from app.schemas.products import ProductLineResponse, ProductListResponse

router = APIRouter(tags=["products"])


def _row_to_dict(row: ValidationMatrix) -> dict:
    return {
        "product_line": row.product_line,
        "acronym": row.acronym,
        "n3_kam_limit": row.n3_kam_limit,
        "n2_zone_limit": row.n2_zone_limit,
        "n1_vp_limit": row.n1_vp_limit,
    }


@router.get(
    "/api/products",
    summary="Retrieve Product Data",
    operation_id="retrieveProducts",
    response_model=ProductListResponse,
)
async def retrieve_products(
    productName: str | None = Query(default=None, description="Filter by product line name (case-insensitive, partial match)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all product lines. If `productName` is provided, filters by partial,
    case-insensitive match on the product_line name.
    """
    query = select(ValidationMatrix)
    if productName:
        query = query.where(ValidationMatrix.product_line.ilike(f"%{productName}%"))
    result = await db.execute(query)
    rows = result.scalars().all()
    return ProductListResponse(products=[_row_to_dict(r) for r in rows])


@router.get(
    "/api/product-lines",
    summary="Retrieve Product Line Data by ID",
    operation_id="retrieveProductLine",
    response_model=ProductLineResponse,
)
async def retrieve_product_line(
    productLineId: str = Query(..., description="Exact product line name (e.g. 'Brushes', 'Seals')"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the thresholds for a single product line identified by `productLineId`.
    Returns `productLine` as a single-item list (consistent array shape for ChatGPT tool schema).
    Raises 404 if not found.
    """
    from fastapi import HTTPException

    result = await db.execute(
        select(ValidationMatrix).where(ValidationMatrix.product_line == productLineId)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"Product line '{productLineId}' not found.")
    return ProductLineResponse(productLine=[_row_to_dict(row)])
