from datetime import datetime

from pydantic import BaseModel, EmailStr

from app.models.product_line_routing import ProductLineRoutingRole


class ProductLineRoutingBase(BaseModel):
    product_line: str
    role: ProductLineRoutingRole
    email: EmailStr


class ProductLineRoutingCreate(ProductLineRoutingBase):
    pass


class ProductLineRoutingUpdate(ProductLineRoutingBase):
    pass


class ProductLineRoutingOut(ProductLineRoutingBase):
    id: int
    updated_at: datetime

    model_config = {"from_attributes": True}


class RoutingAssignRequest(BaseModel):
    """Replace the full list of email assignments for a (product_line, role) pair."""
    product_line: str
    role: ProductLineRoutingRole
    emails: list[EmailStr]