# --- Public top-level API ---


from langchain_core.language_models import BaseChatModel
from .settings import settings

from controlflow.defaults import defaults

# base classes
from .agents import Agent
from .tasks import Task
from .flows import Flow

# functions and decorators
from .instructions import instructions
from .decorators import flow, task
from .tools import tool
from .run import run, run_async, run_tasks, run_tasks_async
from .plan import plan


# --- Version ---

try:
    from ._version import version as __version__  # type: ignore
except ImportError:
    __version__ = "unknown"
