"""Схема БД и доступ к ней (SQLAlchemy 2.0, async + SQLite).

Схема повторяет описание из CLAUDE.md. Дедупликация вакансий — на уровне БД
через UNIQUE(platform_id, external_id), а не руками в коде.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import DB_URL

# Известные площадки — их имена совпадают с platforms.name.
PLATFORMS = ("hh", "habr_career", "getmatch", "company_site")


class Base(DeclarativeBase):
    pass


class Platform(Base):
    __tablename__ = "platforms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)

    vacancies: Mapped[list["Vacancy"]] = relationship(back_populates="platform")
    sessions: Mapped[list["AgentSession"]] = relationship(back_populates="platform")


class Vacancy(Base):
    __tablename__ = "vacancies"
    __table_args__ = (UniqueConstraint("platform_id", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"))
    external_id: Mapped[str | None] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    company: Mapped[str | None] = mapped_column(String)
    salary_from: Mapped[int | None] = mapped_column(Integer)
    salary_to: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String)
    work_format: Mapped[str | None] = mapped_column(String)  # "REMOTE,HYBRID" — коды HH через запятую
    experience: Mapped[str | None] = mapped_column(String)   # код HH: noExperience/between1And3/…
    raw_description: Mapped[str | None] = mapped_column(Text)
    match_score: Mapped[float | None] = mapped_column(Float)
    match_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="new")
    found_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    platform: Mapped["Platform"] = relationship(back_populates="vacancies")
    applications: Mapped[list["Application"]] = relationship(back_populates="vacancy")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"))
    cover_letter: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    status: Mapped[str | None] = mapped_column(String)  # applied / failed / response_received

    vacancy: Mapped["Vacancy"] = relationship(back_populates="applications")


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"))
    storage_state_path: Mapped[str | None] = mapped_column(String)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False)

    platform: Mapped["Platform"] = relationship(back_populates="sessions")


engine = create_async_engine(DB_URL)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы (если их нет), доращивает колонки на существующих
    (SQLite/create_all новые таблицы не трогает существующие — своя лёгкая
    миграция, Alembic для одного файла БД избыточен) и наполняет справочник
    платформ."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        existing_cols = {
            row[1] for row in (await conn.execute(text("PRAGMA table_info(vacancies)")))
        }
        for col, ddl_type in (
            ("salary_currency", "TEXT"),
            ("work_format", "TEXT"),
            ("experience", "TEXT"),
        ):
            if col not in existing_cols:
                await conn.execute(text(f"ALTER TABLE vacancies ADD COLUMN {col} {ddl_type}"))

    async with Session() as session:
        have = set((await session.execute(select(Platform.name))).scalars())
        for name in PLATFORMS:
            if name not in have:
                session.add(Platform(name=name))
        await session.commit()


async def record_agent_session(
    platform_name: str, storage_state_path: str, is_valid: bool = True
) -> None:
    """Апсерт agent_sessions — по одной строке на площадку.

    Вызывать после того, как убедились, что сессия рабочая (например, сразу
    после успешного login() или после успешного поиска на уже сохранённой
    сессии) — is_valid и last_login_at отражают факт «сессия только что
    подтверждена рабочей», а не момент физического ввода логина/пароля.
    """
    async with Session() as session:
        platform = (
            await session.execute(select(Platform).where(Platform.name == platform_name))
        ).scalar_one()

        row = (
            await session.execute(
                select(AgentSession).where(AgentSession.platform_id == platform.id)
            )
        ).scalar_one_or_none()

        if row is None:
            row = AgentSession(platform_id=platform.id)
            session.add(row)

        row.storage_state_path = storage_state_path
        row.last_login_at = datetime.utcnow()
        row.is_valid = is_valid
        await session.commit()


if __name__ == "__main__":
    import asyncio

    asyncio.run(init_db())
    print("БД инициализирована")
