"""LLM tool that reports the current datetime in the Home Assistant host's
local timezone (the Supervisor sets TZ for the add-on container)."""
from langchain.tools import BaseTool
from datetime import datetime

from core import disclosure


class tool_time(BaseTool):
    """LLM tool returning the current datetime in the host's local timezone."""
    name: str = "Datetime get tool"  # Add type annotation here
    description: str = "Return the current date and time. There is no input parameter."

    def _run(self) -> str:
        """Returns the current datetime plus today's start/end bounds, in the host's local timezone."""
        try:
            now = datetime.now().astimezone()
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
            return disclosure.text(
                f"Current datetime: {now.strftime('%Y-%m-%d %H:%M:%S')}. "
                f"Today's date: {now.strftime('%Y-%m-%d')}. "
                f"Weekday: {now.strftime('%A')}. "
                f"Start of today: {start_of_day.strftime('%Y-%m-%dT%H:%M:%S')}. "
                f"End of today: {end_of_day.strftime('%Y-%m-%dT%H:%M:%S')}. "
                f"Timezone: {now.tzinfo}."
            )
        except Exception as e:
            return disclosure.text(f"Error: {str(e)}")
