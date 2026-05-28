from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict

from models import EventStatus, ReminderStatus


# ==========================================
# USER SCHEMAS
# ==========================================

class UserBase(BaseModel):
    """Base fields for User schemas."""
    tg_id: int = Field(..., description="Telegram ID of the student")
    timezone: str = Field(default="Europe/Moscow", description="Student's timezone")


class UserCreate(UserBase):
    """Schema for registering a new student/user."""
    pass


class UserResponse(UserBase):
    """Schema for responding with user details."""
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ==========================================
# REMINDER SCHEMAS
# ==========================================

class ReminderBase(BaseModel):
    """Base fields for Reminder schemas."""
    remind_at: datetime = Field(..., description="Time at which to send the reminder")
    status: ReminderStatus = Field(default=ReminderStatus.PENDING, description="Status of the reminder")


class ReminderCreate(ReminderBase):
    """Schema for creating a reminder."""
    event_id: int = Field(..., description="The ID of the event this reminder is linked to")


class ReminderResponse(ReminderBase):
    """Schema for responding with reminder details."""
    id: int
    event_id: int

    model_config = ConfigDict(from_attributes=True)


# ==========================================
# EVENT SCHEMAS
# ==========================================

class EventBase(BaseModel):
    """Base fields for Event schemas."""
    title: str = Field(..., max_length=255, description="Title of the academic or personal event")
    description: Optional[str] = Field(default=None, description="Detailed description of the event")
    deadline: datetime = Field(..., description="The event's deadline or target completion time")
    status: EventStatus = Field(default=EventStatus.DRAFT, description="Current status of the event")


class EventCreate(EventBase):
    """Schema for creating a new event."""
    user_id: int = Field(..., description="The ID of the user who owns this event")
    # Allows attaching reminders directly during event creation for convenience
    reminders: Optional[List[datetime]] = Field(
        default=None, 
        description="Optional list of timestamps to schedule reminders upon creation"
    )


class EventUpdate(BaseModel):
    """Schema for updating an existing event (all fields are optional)."""
    title: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = Field(default=None)
    deadline: Optional[datetime] = Field(default=None)
    status: Optional[EventStatus] = Field(default=None)


class EventResponse(EventBase):
    """Schema for responding with event details, including its nested reminders."""
    id: int
    user_id: int
    created_at: datetime
    reminders: List[ReminderResponse] = []

    model_config = ConfigDict(from_attributes=True)
