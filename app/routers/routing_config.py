import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import require_role
from app.models.product_line_routing import ProductLineRouting
from app.models.user import User, UserRole
from app.models.validation_matrix import ValidationMatrix
from app.schemas.product_line_routing import (
    ProductLineRoutingCreate,
    ProductLineRoutingOut,
    ProductLineRoutingUpdate,
    RoutingAssignRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/owner/routing-config", tags=["routing-config"])


async def _ensure_product_line_exists(db: AsyncSession, product_line: str) -> str:
    normalized_product_line = str(product_line or "").strip()
    result = await db.execute(
        select(ValidationMatrix).where(ValidationMatrix.product_line == normalized_product_line)
    )
    matrix = result.scalar_one_or_none()
    if matrix is None:
        raise HTTPException(status_code=404, detail="Product line not found.")
    return matrix.product_line


@router.get("", response_model=list[ProductLineRoutingOut])
async def list_product_line_routing(
    product_line: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    query = select(ProductLineRouting).order_by(
        ProductLineRouting.product_line.asc(),
        ProductLineRouting.role.asc(),
        ProductLineRouting.id.asc(),
    )
    normalized_product_line = str(product_line or "").strip()
    if normalized_product_line:
        query = query.where(ProductLineRouting.product_line == normalized_product_line)

    result = await db.execute(query)
    return result.scalars().all()


@router.put("/assign", response_model=list[ProductLineRoutingOut])
async def assign_product_line_routing(
    body: RoutingAssignRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    """Replace all email assignments for a (product_line, role) pair atomically.

    Only the assignments for the given product_line + role are touched;
    all other product lines and roles remain unchanged.
    """
    product_line = await _ensure_product_line_exists(db, body.product_line)

    # Deduplicate, preserve order, keep only non-empty emails
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_email in body.emails:
        email = str(raw_email).strip()
        key = email.lower()
        if email and key not in seen:
            seen.add(key)
            normalized.append(email)

    logger.debug(
        "ROUTING SETTINGS: assigning product_line=%r role=%r users_count=%d",
        product_line,
        body.role.value,
        len(normalized),
    )

    # Delete existing assignments for this (product_line, role) only
    await db.execute(
        sa_delete(ProductLineRouting).where(
            ProductLineRouting.product_line == product_line,
            ProductLineRouting.role == body.role,
        )
    )

    # Insert new assignments
    for email in normalized:
        db.add(ProductLineRouting(
            product_line=product_line,
            role=body.role,
            email=email,
        ))

    await db.commit()

    result = await db.execute(
        select(ProductLineRouting)
        .where(
            ProductLineRouting.product_line == product_line,
            ProductLineRouting.role == body.role,
        )
        .order_by(ProductLineRouting.id.asc())
    )
    saved = result.scalars().all()

    logger.debug(
        "ROUTING SETTINGS: assignments saved successfully — saved_count=%d",
        len(saved),
    )

    return saved


@router.post("", response_model=ProductLineRoutingOut, status_code=status.HTTP_201_CREATED)
async def create_product_line_routing(
    body: ProductLineRoutingCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    product_line = await _ensure_product_line_exists(db, body.product_line)
    existing = await db.execute(
        select(ProductLineRouting).where(
            ProductLineRouting.product_line == product_line,
            ProductLineRouting.role == body.role,
            ProductLineRouting.email == str(body.email).strip(),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="This email is already assigned to this product line and role.",
        )

    entry = ProductLineRouting(
        product_line=product_line,
        role=body.role,
        email=str(body.email).strip(),
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


@router.put("/{routing_id}", response_model=ProductLineRoutingOut)
async def update_product_line_routing(
    routing_id: int,
    body: ProductLineRoutingUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    result = await db.execute(
        select(ProductLineRouting).where(ProductLineRouting.id == routing_id)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Routing entry not found.")

    product_line = await _ensure_product_line_exists(db, body.product_line)
    existing = await db.execute(
        select(ProductLineRouting).where(
            ProductLineRouting.product_line == product_line,
            ProductLineRouting.role == body.role,
            ProductLineRouting.email == str(body.email).strip(),
            ProductLineRouting.id != routing_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="This email is already assigned to this product line and role.",
        )

    entry.product_line = product_line
    entry.role = body.role
    entry.email = str(body.email).strip()
    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/{routing_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product_line_routing(
    routing_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    result = await db.execute(
        select(ProductLineRouting).where(ProductLineRouting.id == routing_id)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Routing entry not found.")

    await db.delete(entry)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)