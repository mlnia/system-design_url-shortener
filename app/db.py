from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base

# Arbitrary constant; serialises schema creation when several replicas boot at once.
_SCHEMA_LOCK_ID = 727_274_001


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.engine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        return self._sessionmaker()

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                await conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _SCHEMA_LOCK_ID})
            await conn.run_sync(Base.metadata.create_all)

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
