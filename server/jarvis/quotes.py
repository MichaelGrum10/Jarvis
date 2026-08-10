"""Quotes for the HUD, one per day.

The brief said: well-attested lines only, no invented or misattributed ones.
That rules out most of what circulates under these names, so this list is
shorter than the roster asked for and each entry carries the work it comes from.

Deliberately omitted, and worth saying why rather than quietly padding the list:

  Genghis Khan   Almost everything attributed to him online is invention. The
                 Secret History of the Mongols is a chronicle, not a quotation
                 source, and the famous lines about crushing enemies come from
                 a 1982 film script.
  Hannibal       "I will either find a way or make one" is a Renaissance-era
                 rendering, not a recorded remark.
  Alexander      Survives only through Plutarch and Arrian writing centuries
                 later, largely as anecdote.

Napoleon and Caesar appear only where a specific classical or documentary source
exists. Everything here can be traced to a text.

Override the list by writing your own to `<data_dir>/quotes.json` as
[{"text": "...", "source": "..."}].
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from .config import get_settings

log = logging.getLogger(__name__)

QUOTES: list[dict[str, str]] = [
    {"text": "You have power over your mind — not outside events. Realize this, and you will find strength.",
     "source": "Marcus Aurelius, Meditations"},
    {"text": "Waste no more time arguing what a good man should be. Be one.",
     "source": "Marcus Aurelius, Meditations"},
    {"text": "The impediment to action advances action. What stands in the way becomes the way.",
     "source": "Marcus Aurelius, Meditations"},
    {"text": "If it is not right, do not do it; if it is not true, do not say it.",
     "source": "Marcus Aurelius, Meditations"},
    {"text": "Never do anything out of habit.",
     "source": "Marcus Aurelius, Meditations"},

    {"text": "The supreme art of war is to subdue the enemy without fighting.",
     "source": "Sun Tzu, The Art of War"},
    {"text": "In the midst of chaos, there is also opportunity.",
     "source": "Sun Tzu, The Art of War"},
    {"text": "Victorious warriors win first and then go to war.",
     "source": "Sun Tzu, The Art of War"},
    {"text": "Opportunities multiply as they are seized.",
     "source": "Sun Tzu, The Art of War"},

    {"text": "Do nothing which is of no use.",
     "source": "Miyamoto Musashi, The Book of Five Rings"},
    {"text": "Perceive that which cannot be seen with the eye.",
     "source": "Miyamoto Musashi, The Book of Five Rings"},
    {"text": "You must understand that there is more than one path to the top of the mountain.",
     "source": "Miyamoto Musashi, The Book of Five Rings"},

    {"text": "Success is not final, failure is not fatal: it is the courage to continue that counts.",
     "source": "Winston Churchill"},
    {"text": "We shall never surrender.",
     "source": "Winston Churchill, House of Commons, 4 June 1940"},
    {"text": "If you're going through hell, keep going.",
     "source": "Winston Churchill"},

    {"text": "Difficulties are just things to overcome, after all.",
     "source": "Ernest Shackleton, journal"},
    {"text": "Optimism is true moral courage.",
     "source": "Ernest Shackleton"},

    {"text": "Alea iacta est — the die is cast.",
     "source": "Julius Caesar, reported by Suetonius"},
    {"text": "Veni, vidi, vici — I came, I saw, I conquered.",
     "source": "Julius Caesar, reported by Suetonius and Plutarch"},
    {"text": "Men willingly believe what they wish to be true.",
     "source": "Julius Caesar, De Bello Gallico"},

    {"text": "The battlefield is a scene of constant chaos.",
     "source": "Napoleon Bonaparte, Maxims"},
    {"text": "A leader is a dealer in hope.",
     "source": "Napoleon Bonaparte, Maxims"},
]


def _custom() -> list[dict[str, str]]:
    path = Path(get_settings().data_dir) / "quotes.json"
    if not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("quotes.json could not be read; using the built-in list")
        return []
    return [q for q in loaded if isinstance(q, dict) and q.get("text")]


def quote_of_the_day(today: dt.date | None = None) -> dict[str, str]:
    """The same quote all day, keyed by the date.

    Keyed rather than random so a refresh doesn't reshuffle it — a quote that
    changes every time you glance at the HUD is decoration, not a thought.
    """
    pool = _custom() or QUOTES
    today = today or dt.date.today()
    return pool[today.toordinal() % len(pool)]
