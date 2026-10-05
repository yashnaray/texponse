from __future__ import annotations

from datetime import datetime

from sqlmodel import and_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from texponse.db.models import (
    Conversation,
    ConversationEvent,
    ConversationStatus,
    JobberToken,
    Response,
    User,
)


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_phone(self, company_id: str, phone: str) -> User | None:
        stmt = select(User).where(and_(User.company_id == company_id, User.phone_number == phone))
        result = await self._session.exec(stmt)
        return result.first()

    async def get_by_id(self, user_id: str) -> User | None:
        return await self._session.get(User, user_id)

    async def create(self, user: User) -> User:
        self._session.add(user)
        await self._session.commit()
        await self._session.refresh(user)
        return user


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self,
        aircall_id: str,
        company_id: str,
        direction: str,
        channel: str,
    ) -> Conversation:
        stmt = select(Conversation).where(
            and_(
                Conversation.aircall_id == aircall_id,
                Conversation.company_id == company_id,
            )
        )
        result = await self._session.exec(stmt)
        conv = result.first()
        if conv:
            return conv

        conv = Conversation(
            aircall_id=aircall_id,
            company_id=company_id,
            direction=direction,
            channel=channel,
        )
        self._session.add(conv)
        await self._session.commit()
        await self._session.refresh(conv)
        return conv

    async def get(self, conversation_id: str) -> Conversation | None:
        return await self._session.get(Conversation, conversation_id)

    async def get_by_aircall_id(self, company_id: str, aircall_id: str) -> Conversation | None:
        stmt = select(Conversation).where(
            and_(
                Conversation.aircall_id == aircall_id,
                Conversation.company_id == company_id,
            )
        )
        result = await self._session.exec(stmt)
        return result.first()

    async def add_event(self, event: ConversationEvent) -> ConversationEvent:
        self._session.add(event)
        await self._session.commit()
        await self._session.refresh(event)
        return event

    async def inbound_already_queued(
        self,
        conversation_id: str,
        event_type: object,
        aircall_message_id: str | None,
    ) -> bool:
        """True if this inbound message was already ingested (webhook+callback double-fire)."""
        if not aircall_message_id:
            return False
        stmt = select(ConversationEvent).where(
            and_(
                ConversationEvent.conversation_id == conversation_id,
                ConversationEvent.event_type == event_type,  # type: ignore[arg-type]
                ConversationEvent.aircall_message_id == aircall_message_id,
            )
        )
        result = await self._session.exec(stmt)
        return result.first() is not None

    async def get_history(
        self,
        conversation_id: str,
        limit: int = 50,
    ) -> list[ConversationEvent]:
        from sqlmodel import desc

        stmt = (
            select(ConversationEvent)
            .where(ConversationEvent.conversation_id == conversation_id)
            .order_by(desc(ConversationEvent.created_at))
            .limit(limit)
        )
        result = await self._session.exec(stmt)
        return list(result.all())

    async def get_history_by_aircall_id(
        self, company_id: str, aircall_id: str, limit: int = 50
    ) -> list[ConversationEvent]:
        conv = await self.get_by_aircall_id(company_id, aircall_id)
        if conv is None:
            return []
        return await self.get_history(conv.id, limit)

    async def close(self, conversation_id: str) -> None:
        conv = await self._session.get(Conversation, conversation_id)
        if conv:
            conv.status = ConversationStatus.CLOSED
            conv.ended_at = datetime.utcnow()
            await self._session.commit()

    async def save_response(self, response: Response) -> Response:
        self._session.add(response)
        await self._session.commit()
        await self._session.refresh(response)
        return response


class JobberTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, company_id: str) -> JobberToken | None:
        stmt = select(JobberToken).where(JobberToken.company_id == company_id)
        result = await self._session.exec(stmt)
        return result.first()

    async def save(self, token: JobberToken) -> JobberToken:
        existing = await self.get(token.company_id)
        if existing:
            existing.access_token = token.access_token
            existing.refresh_token = token.refresh_token
            existing.expires_at = token.expires_at
            await self._session.commit()
            await self._session.refresh(existing)
            return existing

        self._session.add(token)
        await self._session.commit()
        await self._session.refresh(token)
        return token

    async def is_expired(self, token: JobberToken) -> bool:
        return datetime.utcnow() >= token.expires_at
