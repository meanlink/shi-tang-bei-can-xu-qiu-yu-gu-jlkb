from pydantic import BaseModel, Field


class CampaignInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    dataset_id: int
    start_date: str = Field(min_length=8, max_length=10)
    end_date: str = Field(min_length=8, max_length=10)
    lock_hour: int = Field(default=10, ge=0, le=23)
    current_params: dict = Field(default_factory=dict)
    candidate_params: dict = Field(default_factory=dict)
    threshold: dict = Field(default_factory=lambda: {
        "metric": "wape", "min_improve_pct": 5.0, "min_scored_days": 7})


class ForecastLockInput(BaseModel):
    day: str = Field(min_length=8, max_length=10)
    rule: str | None = Field(default=None, pattern="^(current|candidate)$")


class ActualInput(BaseModel):
    day: str = Field(min_length=8, max_length=10)
    value: float = Field(ge=0)


class TrialInput(BaseModel):
    campaign_id: int
    name: str = Field(min_length=2, max_length=160)
    target_day: str = Field(min_length=8, max_length=10)
