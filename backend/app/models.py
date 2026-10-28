import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RunStatus(str, enum.Enum):
    queued = "queued"
    retrieving = "retrieving"
    reproducing = "reproducing"
    patching = "patching"
    testing = "testing"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    rejected = "rejected"
    completed = "completed"
    failed = "failed"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(Integer, unique=True)
    login: Mapped[str] = mapped_column(String(255))

    repositories: Mapped[list["Repository"]] = relationship(back_populates="owner")


class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("full_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    github_id: Mapped[int] = mapped_column(Integer, unique=True)
    installation_id: Mapped[int | None] = mapped_column(Integer)
    full_name: Mapped[str] = mapped_column(String(512))
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    indexed_commit: Mapped[str | None] = mapped_column(String(64))

    owner: Mapped[User] = relationship(back_populates="repositories")
    issues: Mapped[list["Issue"]] = relationship(back_populates="repository")
    files: Mapped[list["IndexedFile"]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )


class Issue(TimestampMixin, Base):
    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("repository_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(1024))
    body: Mapped[str] = mapped_column(Text, default="")

    repository: Mapped[Repository] = relationship(back_populates="issues")
    runs: Mapped[list["Run"]] = relationship(back_populates="issue")


class Run(TimestampMixin, Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"))
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.queued)
    thread_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)

    issue: Mapped[Issue] = relationship(back_populates="runs")


class IndexedFile(Base):
    __tablename__ = "indexed_files"
    __table_args__ = (UniqueConstraint("repository_id", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(1024))
    language: Mapped[str] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(Integer)
    line_count: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)

    repository: Mapped[Repository] = relationship(back_populates="files")
    symbols: Mapped[list["Symbol"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("indexed_files.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    qualname: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    docstring: Mapped[str | None] = mapped_column(Text)

    file: Mapped[IndexedFile] = relationship(back_populates="symbols")
