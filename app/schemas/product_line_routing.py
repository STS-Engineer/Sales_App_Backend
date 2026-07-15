from datetime import datetime

from pydantic import BaseModel

from app.models.product_line_routing import ProductLineRoutingRole


class ProductLineRoutingBase(BaseModel):
    product_line: str
    role: ProductLineRoutingRole
    email: str


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
    emails: list[str]


class RoutingViewerOut(BaseModel):
    id: int
    product_line: str
    role: ProductLineRoutingRole
    user_email: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class RoutingViewerAssignRequest(BaseModel):
    """Replace the full list of viewers for a (product_line, role) pair."""
    product_line: str
    role: ProductLineRoutingRole
    viewer_emails: list[str]


class ProductLineOut(BaseModel):
    product_line: str
    acronym: str

    model_config = {"from_attributes": True}