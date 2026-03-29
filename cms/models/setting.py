"""CMS runtime settings stored in the database."""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base


class CMSSetting(Base):
    __tablename__ = "cms_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
