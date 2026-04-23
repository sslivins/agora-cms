"""Row model for the ``leader_leases`` table.

Used by :mod:`cms.services.leader` to elect a single replica to run
loops where bounded-time failover matters (see
``docs/multi-replica-architecture.md`` §Stage 4 for the design).

The row is defined here mostly so ``Base.metadata.create_all`` picks
it up for unit tests that spin a fresh schema; runtime semantics live
in :mod:`cms.services.leader`.
"""

from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base


class LeaderLease(Base):
    __tablename__ = "leader_leases"

    loop_name: Mapped[str] = mapped_column(Text, primary_key=True)
    holder_id: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    renewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
