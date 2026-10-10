import os
from dataclasses import dataclass
from dotenv import load_dotenv

MODES = {"critical": 90, "high_signal": 75, "full_feed": 55, "research": 55}

@dataclass
class Config:
    token: str
    channel: int
    research_channel: int
    admins: set[int]
    guild: int
    api_key: str
    model: str
    mode: str
    threshold: int
    poll: int
    max_age: int
    llm_budget: int
    database: str
    sources: str
    backfill: bool

    @classmethod
    def load(cls):
        load_dotenv()
        mode = os.getenv("MODE", "high_signal")
        if mode not in MODES:
            raise ValueError(f"MODE must be one of {list(MODES)}")
        return cls(os.getenv("DISCORD_TOKEN", ""), int(os.getenv("DISCORD_CHANNEL_ID") or 0),
                   int(os.getenv("DISCORD_RESEARCH_CHANNEL_ID") or 0),
                   {int(x.strip()) for x in os.getenv("DISCORD_ADMIN_IDS", "").split(",") if x.strip()},
                   int(os.getenv("DISCORD_GUILD_ID") or 0), os.getenv("OPENAI_API_KEY", ""),
                   os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), mode,
                   max(0, min(100, int(os.getenv("MIN_SCORE", str(MODES[mode]))))),
                   max(30, int(os.getenv("POLL_SECONDS", "300"))),
                   max(1, int(os.getenv("MAX_POST_AGE_HOURS", "72"))),
                   max(0, int(os.getenv("MAX_LLM_PER_CYCLE", "20"))),
                   os.getenv("DATABASE_PATH", "data/news.sqlite3"),
                   os.getenv("SOURCES_PATH", "sources.json"), os.getenv("BACKFILL", "false").lower() == "true")
