from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import PublicUserResponse
from ..security import current_user

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[PublicUserResponse])
def users(
    q: str = Query(default="", max_length=120),
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    pattern = f"%{q.strip()}%"
    return list(
        database.scalars(
            select(User)
            .where(
                User.id != user.id,
                User.account_status == "active",
                User.name.ilike(pattern),
            )
            .order_by(User.is_bot.desc(), User.name)
            .limit(100)
        )
    )
