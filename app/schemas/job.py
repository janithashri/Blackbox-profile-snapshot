from pydantic import BaseModel, Field


class ProfileJobRequest(BaseModel):
    linkedin_url_or_id: str = Field(min_length=1)
