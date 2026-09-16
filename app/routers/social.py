from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Comment, Connection, ModerationCase, Post, PostLike, User
from ..presenters import post_response, public_user_response
from ..schemas import (
    CommentCreate,
    CommentResponse,
    FeedResponse,
    PostCreate,
    PostResponse,
    ReportCreate,
)
from ..security import current_user
from ..services.audit import record_audit
from ..services.moderation import ModerationService

router = APIRouter(prefix="/api", tags=["social"])


@router.get("/feed", response_model=FeedResponse)
def feed(
    cursor: int | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    connection_rows = database.execute(
        select(Connection.requester_id, Connection.recipient_id).where(
            Connection.status == "accepted",
            or_(Connection.requester_id == user.id, Connection.recipient_id == user.id),
        )
    ).all()
    peers = {b if a == user.id else a for a, b in connection_rows}
    query = select(Post).where(
        Post.status == "published",
        Post.moderation_status == "approved",
        or_(
            Post.visibility == "public",
            Post.author_id == user.id,
            Post.author_id.in_(peers or {-1}),
        ),
    )
    if cursor:
        query = query.where(Post.id < cursor)
    rows = list(database.scalars(query.order_by(Post.id.desc()).limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    return FeedResponse(
        items=[post_response(database, row, user.id) for row in rows],
        next_cursor=rows[-1].id if has_more and rows else None,
    )


@router.post("/posts", response_model=PostResponse, status_code=201)
def create_post(
    payload: PostCreate,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    policy = ModerationService(database).evaluate(payload.content, generated=False)
    post = Post(
        author_id=user.id,
        content=payload.content.strip(),
        visibility=payload.visibility,
        moderation_status=policy.status,
    )
    database.add(post)
    database.flush()
    if not policy.allowed:
        database.add(
            ModerationCase(
                entity_type="post",
                entity_id=post.id,
                reporter_id=user.id,
                severity="high",
                category="automated_flag",
                reason=", ".join(policy.reasons),
                evidence={"content": post.content},
            )
        )
    record_audit(database, "post.created", "post", post.id, actor_id=user.id, actor_type="user")
    database.commit()
    database.refresh(post)
    return post_response(database, post, user.id)


@router.delete("/posts/{post_id}", status_code=204)
def delete_post(
    post_id: int,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    post = database.get(Post, post_id)
    if not post or (post.author_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Post not found")
    post.status = "deleted"
    record_audit(database, "post.deleted", "post", post.id, actor_id=user.id, actor_type="user")
    database.commit()
    return Response(status_code=204)


@router.put("/posts/{post_id}/like", status_code=204)
def like_post(
    post_id: int,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    post = database.get(Post, post_id)
    if not post or post.status != "published":
        raise HTTPException(status_code=404, detail="Post not found")
    existing = database.scalar(
        select(PostLike).where(PostLike.post_id == post_id, PostLike.user_id == user.id)
    )
    if not existing:
        database.add(PostLike(post_id=post_id, user_id=user.id))
        database.commit()
    return Response(status_code=204)


@router.delete("/posts/{post_id}/like", status_code=204)
def unlike_post(
    post_id: int,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    existing = database.scalar(
        select(PostLike).where(PostLike.post_id == post_id, PostLike.user_id == user.id)
    )
    if existing:
        database.delete(existing)
        database.commit()
    return Response(status_code=204)


@router.get("/posts/{post_id}/comments", response_model=list[CommentResponse])
def comments(
    post_id: int,
    _: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    if not database.get(Post, post_id):
        raise HTTPException(status_code=404, detail="Post not found")
    rows = list(
        database.scalars(
            select(Comment)
            .where(Comment.post_id == post_id, Comment.status == "published")
            .order_by(Comment.created_at)
        )
    )
    return [
        CommentResponse(
            id=row.id,
            post_id=row.post_id,
            author=public_user_response(database.get(User, row.author_id)),
            parent_comment_id=row.parent_comment_id,
            content=row.content,
            is_bot_generated=row.is_bot_generated,
            generation_metadata=row.generation_metadata or {},
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.post("/posts/{post_id}/comments", response_model=CommentResponse, status_code=201)
def create_comment(
    post_id: int,
    payload: CommentCreate,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    post = database.get(Post, post_id)
    if not post or post.status != "published":
        raise HTTPException(status_code=404, detail="Post not found")
    if payload.parent_comment_id:
        parent = database.get(Comment, payload.parent_comment_id)
        if not parent or parent.post_id != post_id:
            raise HTTPException(
                status_code=400, detail="Parent comment does not belong to this post"
            )
    policy = ModerationService(database).evaluate(payload.content, generated=False)
    row = Comment(
        post_id=post_id,
        author_id=user.id,
        parent_comment_id=payload.parent_comment_id,
        content=payload.content.strip(),
        moderation_status=policy.status,
    )
    database.add(row)
    database.flush()
    if not policy.allowed:
        database.add(
            ModerationCase(
                entity_type="comment",
                entity_id=row.id,
                reporter_id=user.id,
                severity="high",
                category="automated_flag",
                reason=", ".join(policy.reasons),
                evidence={"content": row.content},
            )
        )
    database.commit()
    database.refresh(row)
    return CommentResponse(
        id=row.id,
        post_id=row.post_id,
        author=public_user_response(user),
        parent_comment_id=row.parent_comment_id,
        content=row.content,
        is_bot_generated=False,
        generation_metadata=row.generation_metadata or {},
        created_at=row.created_at,
    )


@router.post("/reports", status_code=201)
def create_report(
    payload: ReportCreate,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    row = ModerationCase(
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        reporter_id=user.id,
        category=payload.category,
        reason=payload.reason,
    )
    database.add(row)
    database.flush()
    record_audit(database, "moderation.reported", "moderation_case", row.id, actor_id=user.id)
    database.commit()
    return {"id": row.id, "status": row.status}
