import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# DATABASE_URL examples:
#   Local/free dev (default):  sqlite:///./nora.db
#   Free Postgres (Supabase):  postgresql://user:password@host:port/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./nora.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
