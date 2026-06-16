from pydantic import BaseModel


class TeamOrganisationRow(BaseModel):
    depth: int | None = None
    role: str | None = None
    zone: str | None = None
    hierarchy: str | None = None
    person: str | None = None
    email: str | None = None
    reports_to: str | None = None
    reports_email: str | None = None
    sort_path: str | None = None
