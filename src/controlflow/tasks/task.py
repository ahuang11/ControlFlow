import datetime
import textwrap
import warnings
from contextlib import ExitStack, contextmanager
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    GenericAlias,
    Optional,
    TypeVar,
    Union,
    _AnnotatedAlias,
    _GenericAlias,
    _LiteralGenericAlias,
    _SpecialGenericAlias,
)

from prefect.context import TaskRunContext
from pydantic import (
    Field,
    PydanticSchemaGenerationError,
    RootModel,
    TypeAdapter,
    field_serializer,
    field_validator,
)

import controlflow
from controlflow.agents import Agent
from controlflow.instructions import get_instructions
from controlflow.tools import Tool, tool
from controlflow.tools.input import cli_input
from controlflow.tools.tools import as_tools
from controlflow.utilities.context import ctx
from controlflow.utilities.general import (
    NOTSET,
    ControlFlowModel,
    hash_objects,
)
from controlflow.utilities.logging import get_logger
from controlflow.utilities.prefect import prefect_task as prefect_task

if TYPE_CHECKING:
    from controlflow.flows import Flow
    from controlflow.orchestration.handler import Handler
    from controlflow.orchestration.turn_strategies import TurnStrategy

T = TypeVar("T")
logger = get_logger(__name__)


def get_task_run_name():
    context = TaskRunContext.get()
    task = context.parameters["self"]
    return f"Task.run() ({task.friendly_name()})"


class Labels(RootModel):
    root: tuple[Any, ...]

    def __iter__(self):
        return iter(self.root)

    def __getitem__(self, item):
        return self.root[item]

    def __repr__(self) -> str:
        return f'Labels: {", ".join(self.root)}'


class TaskStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESSFUL = "SUCCESSFUL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


INCOMPLETE_STATUSES = {TaskStatus.PENDING, TaskStatus.RUNNING}
COMPLETE_STATUSES = {TaskStatus.SUCCESSFUL, TaskStatus.FAILED, TaskStatus.SKIPPED}


class Task(ControlFlowModel):
    id: str = None
    name: Optional[str] = Field(None, description="A name for the task.")
    objective: str = Field(
        ..., description="A brief description of the required result."
    )
    instructions: Union[str, None] = Field(
        None, description="Detailed instructions for completing the task."
    )
    agents: Optional[list[Agent]] = Field(
        default=None,
        description="A list of agents assigned to the task. "
        "If not provided, it will be inferred from the caller, parent task, flow, or global default.",
    )
    context: dict = Field(
        default_factory=dict,
        description="Additional context for the task. If tasks are provided as "
        "context, they are automatically added as `depends_on`",
    )
    parent: Optional["Task"] = Field(
        NOTSET,
        description="The parent task of this task. Subtasks are considered"
        " upstream dependencies of their parents.",
        validate_default=True,
    )
    depends_on: set["Task"] = Field(
        default_factory=set, description="Tasks that this task depends on explicitly."
    )
    prompt: Optional[str] = Field(
        None, description="A prompt to display to the agent working on the task."
    )
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[Union[T, str]] = None
    result_type: Union[
        type[T],
        GenericAlias,
        _GenericAlias,
        _SpecialGenericAlias,
        _AnnotatedAlias,
        Labels,
        None,
    ] = Field(
        NOTSET,
        description="The expected type of the result. This should be a type"
        ", generic alias, BaseModel subclass, or list of choices. "
        "Can be None if no result is expected or the agent should communicate internally.",
        validate_default=True,
    )
    result_validator: Optional[Callable] = Field(
        None,
        description="A function that validates the result. This should be a "
        "function that takes the raw result and either returns a validated "
        "result or raises an informative error if the result is not valid. The "
        "result validator function is called *after* the `result_type` is "
        "processed.",
    )
    tools: list[Tool] = Field(
        default_factory=list,
        description="Tools available to every agent working on this task.",
    )
    completion_agents: Optional[list[Agent]] = Field(
        default=None,
        description="Agents that are allowed to mark this task as complete. If None, all agents are allowed.",
    )
    interactive: bool = False
    max_llm_calls: Optional[int] = Field(
        default_factory=lambda: controlflow.settings.task_max_llm_calls,
        description="Maximum number of LLM calls to make before the task should be marked as failed. "
        "The total calls are measured over the life of the task, and include any LLM call for "
        "which this task is considered `assigned`.",
    )
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.now)
    _subtasks: set["Task"] = set()
    _downstreams: set["Task"] = set()
    _cm_stack: list[contextmanager] = []
    _llm_calls: int = 0

    model_config = dict(extra="forbid", arbitrary_types_allowed=True)

    def __init__(
        self,
        objective: str = None,
        user_access: bool = None,
        **kwargs,
    ):
        """
        Initialize a Task object.

        Args:
            objective (str, optional): The objective of the task. Defaults to None.
            result_type (Any, optional): The type of the result. Defaults to NOTSET.
            user_access (bool, optional): Whether the task is interactive. Defaults to None.
            **kwargs: Additional keyword arguments.
        """
        # allow certain args to be provided as a positional args
        if objective is not None:
            kwargs["objective"] = objective

        if additional_instructions := get_instructions():
            kwargs["instructions"] = (
                kwargs.get("instructions")
                or "" + "\n" + "\n".join(additional_instructions)
            ).strip()

        # deprecated in 0.9
        if user_access:
            warnings.warn(
                "The `user_access` argument is deprecated. Use `interactive=True` instead.",
                DeprecationWarning,
            )
            kwargs["interactive"] = True

        super().__init__(**kwargs)

        # create dependencies to tasks passed in as depends_on
        for task in self.depends_on:
            self.add_dependency(task)

        # create dependencies to tasks passed as subtasks
        if self.parent is not None:
            self.parent.add_subtask(self)

        if self.id is None:
            self.id = self._generate_id()

    def _generate_id(self):
        return hash_objects(
            (
                type(self).__name__,
                self.objective,
                self.instructions,
                str(self.result_type),
                self.prompt,
                str(self.context),
            )
        )

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other):
        """
        Tasks have set attributes and set equality is based on id() of their
        contents, not equality of objects. This means that two tasks are not
        equal unless their set attributes satisfy an identity criteria, which is
        too strict.
        """
        if type(self) is type(other):
            d1 = dict(self)
            d2 = dict(other)
            # conver sets to lists for comparison
            d1["depends_on"] = list(d1["depends_on"])
            d2["depends_on"] = list(d2["depends_on"])
            return d1 == d2
        return False

    def __repr__(self) -> str:
        serialized = self.model_dump(include={"id", "objective"})
        return f"{self.__class__.__name__}({', '.join(f'{key}={repr(value)}' for key, value in serialized.items())})"

    @field_validator("agents")
    def _validate_agents(cls, v):
        if isinstance(v, list) and not v:
            raise ValueError("Agents must be `None` or a non-empty list of agents.")
        return v

    @field_validator("parent", mode="before")
    def _default_parent(cls, v):
        if v == NOTSET:
            parent_tasks = ctx.get("tasks", [])
            v = parent_tasks[-1] if parent_tasks else None
        return v

    @field_validator("result_type", mode="before")
    def _validate_result_type(cls, v):
        if v == NOTSET:
            v = str
        if isinstance(v, _LiteralGenericAlias):
            v = v.__args__
        if isinstance(v, (list, tuple, set)):
            v = Labels(v)
        return v

    @field_validator("tools", mode="before")
    def _validate_tools(cls, v):
        return as_tools(v or [])

    @field_serializer("parent")
    def _serialize_parent(self, parent: Optional["Task"]):
        return parent.id if parent is not None else None

    @field_serializer("depends_on")
    def _serialize_depends_on(self, depends_on: set["Task"]):
        return [t.id for t in depends_on]

    @field_serializer("result_type")
    def _serialize_result_type(self, result_type: list["Task"]):
        if result_type is None:
            return None
        try:
            schema = TypeAdapter(result_type).json_schema()
        except PydanticSchemaGenerationError:
            schema = "<schema could not be generated>"

        return dict(type=repr(result_type), schema=schema)

    @field_serializer("agents")
    def _serialize_agents(self, agents: list[Agent]):
        return [agent.serialize_for_prompt() for agent in self.get_agents()]

    @field_serializer("completion_agents")
    def _serialize_completion_agents(self, completion_agents: Optional[list[Agent]]):
        if completion_agents is not None:
            return [agent.serialize_for_prompt() for agent in completion_agents]
        else:
            return None

    @field_serializer("tools")
    def _serialize_tools(self, tools: list[Callable]):
        return [t.serialize_for_prompt() for t in controlflow.tools.as_tools(tools)]

    def friendly_name(self):
        if self.name:
            name = self.name
        elif len(self.objective) > 50:
            name = f'"{self.objective[:50]}..."'
        else:
            name = f'"{self.objective}"'
        return f"Task #{self.id} ({name})"

    def serialize_for_prompt(self) -> dict:
        """
        Generate a prompt to share information about the task, for use in another object's prompt (like Flow)
        """
        return self.model_dump_json()

    @property
    def subtasks(self) -> list["Task"]:
        return list(sorted(self._subtasks, key=lambda t: t.created_at))

    # def subtask(self, **kwargs) -> "Task":
    #     task = Task(**kwargs)
    #     self.add_subtask(task)
    #     return task

    def add_subtask(self, task: "Task"):
        """
        Indicate that this task has a subtask (which becomes an implicit dependency).
        """
        if task.parent is None:
            task.parent = self
        elif task.parent is not self:
            raise ValueError(f"{self.friendly_name()} already has a parent.")
        self._subtasks.add(task)
        self.depends_on.add(task)

    def add_dependency(self, task: "Task"):
        """
        Indicate that this task depends on another task.
        """
        self.depends_on.add(task)
        task._downstreams.add(self)

    @prefect_task(task_run_name=get_task_run_name)
    def run(
        self,
        agent: Optional[Agent] = None,
        flow: "Flow" = None,
        turn_strategy: "TurnStrategy" = None,
        max_llm_calls: int = None,
        max_agent_turns: int = None,
        handlers: list["Handler"] = None,
    ) -> T:
        """
        Run the task
        """

        controlflow.run_tasks(
            tasks=[self],
            flow=flow,
            agent=agent,
            turn_strategy=turn_strategy,
            max_llm_calls=max_llm_calls,
            max_agent_turns=max_agent_turns,
            raise_on_error=False,
            handlers=handlers,
        )

        if self.is_successful():
            return self.result
        elif self.is_failed():
            raise ValueError(f"{self.friendly_name()} failed: {self.result}")

    @prefect_task(task_run_name=get_task_run_name)
    async def run_async(
        self,
        agent: Optional[Agent] = None,
        flow: "Flow" = None,
        turn_strategy: "TurnStrategy" = None,
        max_llm_calls: int = None,
        max_agent_turns: int = None,
        handlers: list["Handler"] = None,
    ) -> T:
        """
        Run the task
        """

        await controlflow.run_tasks_async(
            tasks=[self],
            flow=flow,
            agent=agent,
            turn_strategy=turn_strategy,
            max_llm_calls=max_llm_calls,
            max_agent_turns=max_agent_turns,
            raise_on_error=False,
            handlers=handlers,
        )

        if self.is_successful():
            return self.result
        elif self.is_failed():
            raise ValueError(f"{self.friendly_name()} failed: {self.result}")

    @contextmanager
    def create_context(self):
        stack = ctx.get("tasks") or []
        with ctx(tasks=stack + [self]):
            yield self

    def __enter__(self):
        # use stack so we can enter the context multiple times
        self._cm_stack.append(ExitStack())
        return self._cm_stack[-1].enter_context(self.create_context())

    def __exit__(self, *exc_info):
        return self._cm_stack.pop().close()

    def is_incomplete(self) -> bool:
        return self.status in INCOMPLETE_STATUSES

    def is_complete(self) -> bool:
        return self.status in COMPLETE_STATUSES

    def is_pending(self) -> bool:
        return self.status == TaskStatus.PENDING

    def is_running(self) -> bool:
        return self.status == TaskStatus.RUNNING

    def is_successful(self) -> bool:
        return self.status == TaskStatus.SUCCESSFUL

    def is_failed(self) -> bool:
        return self.status == TaskStatus.FAILED

    def is_skipped(self) -> bool:
        return self.status == TaskStatus.SKIPPED

    def is_ready(self) -> bool:
        """
        Returns True if all dependencies are complete and this task is
        incomplete, meaning it is ready to be worked on.
        """
        return self.is_incomplete() and all(t.is_complete() for t in self.depends_on)

    def get_agents(self) -> list[Agent]:
        if self.agents is not None:
            return self.agents
        elif self.parent:
            return self.parent.get_agents()
        else:
            from controlflow.flows import get_flow

            try:
                flow = get_flow()
            except ValueError:
                flow = None
            if flow and flow.default_agent:
                return [flow.default_agent]
            else:
                return [controlflow.defaults.agent]

    def get_tools(self) -> list[Union[Tool, Callable]]:
        tools = self.tools.copy()
        if self.interactive:
            tools.append(cli_input)
        return tools

    def get_completion_tools(self) -> list[Tool]:
        tools = [
            self.create_success_tool(),
            self.create_fail_tool(),
        ]
        return tools

    def get_prompt(self) -> str:
        """
        Generate a prompt to share information about the task with an agent.
        """
        from controlflow.orchestration import prompt_templates

        template = prompt_templates.TaskTemplate(template=self.prompt, task=self)
        return template.render()

    def set_status(self, status: TaskStatus):
        self.status = status

        # update TUI
        if tui := ctx.get("tui"):
            tui.update_task(self)

    def mark_running(self):
        self.set_status(TaskStatus.RUNNING)

    def mark_successful(self, result: T = None):
        self.result = self.validate_result(result)
        self.set_status(TaskStatus.SUCCESSFUL)

    def mark_failed(self, reason: Optional[str] = None):
        self.result = reason
        self.set_status(TaskStatus.FAILED)

    def mark_skipped(self):
        self.set_status(TaskStatus.SKIPPED)

    def create_success_tool(self) -> Tool:
        """
        Create an agent-compatible tool for marking this task as successful.
        """
        options = {}
        instructions = textwrap.dedent("""
            Use this tool to mark the task as successful and provide a result. 
            This tool can only be used one time per task.
        """)
        result_schema = None

        # if the result_type is a tuple of options, then we want the LLM to provide
        # a single integer index instead of writing out the entire option. Therefore
        # we create a tool that describes a series of options and accepts the index
        # as a result.
        if isinstance(self.result_type, Labels):
            result_schema = int
            options = {}
            serialized_options = {}
            for i, option in enumerate(self.result_type):
                options[i] = option
                try:
                    serialized = TypeAdapter(type(option)).dump_python(option)
                except PydanticSchemaGenerationError:
                    serialized = repr(option)
                serialized_options[i] = serialized
            options_str = "\n\n".join(
                f"Option {i}: {option}" for i, option in serialized_options.items()
            )
            instructions += "\n\n" + textwrap.dedent("""
                Provide a single integer as the result, corresponding to the index
                of your chosen option. Your options are: 
                
                {options_str}
                """).format(options_str=options_str)

        # otherwise try to load the schema for the result type
        elif self.result_type is not None:
            try:
                # see if the result type is a valid pydantic type
                TypeAdapter(self.result_type)
                result_schema = self.result_type
            except PydanticSchemaGenerationError:
                pass
            if result_schema is None:
                raise ValueError(
                    f"Could not load or infer schema for result type {self.result_type}. "
                    "Please use a custom type or add compatibility."
                )

        @tool(
            name=f"mark_task_{self.id}_successful",
            description=f"Mark task {self.id} as successful.",
            instructions=instructions,
            include_return_description=False,
        )
        def succeed(result: result_schema) -> str:  # type: ignore
            if self.is_successful():
                raise ValueError(
                    f"{self.friendly_name()} is already marked successful."
                )
            if options:
                if result not in options:
                    raise ValueError(f"Invalid option. Please choose one of {options}")
                result = options[result]
            self.mark_successful(result=result)
            return f"{self.friendly_name()} marked successful."

        return succeed

    def create_fail_tool(self) -> Tool:
        """
        Create an agent-compatible tool for failing this task.
        """

        @tool(
            name=f"mark_task_{self.id}_failed",
            description=(
                f"Mark task {self.id} as failed. Only use when technical errors prevent success. Provide a detailed reason for the failure."
            ),
            include_return_description=False,
        )
        def fail(reason: str) -> str:
            self.mark_failed(reason=reason)
            return f"{self.friendly_name()} marked failed."

        return fail

    def validate_result(self, raw_result: Any) -> T:
        if self.result_type is None and raw_result is not None:
            raise ValueError("Task has result_type=None, but a result was provided.")
        elif isinstance(self.result_type, Labels):
            if raw_result not in self.result_type:
                raise ValueError(
                    f"Result {raw_result} is not in the list of valid result types: {self.result_type}"
                )
            else:
                result = raw_result
        elif self.result_type is not None:
            try:
                result = TypeAdapter(self.result_type).validate_python(raw_result)
            except PydanticSchemaGenerationError:
                if isinstance(raw_result, dict):
                    result = self.result_type(**raw_result)
                else:
                    result = self.result_type(raw_result)

        # the raw result is None
        else:
            result = raw_result

            # Convert DataFrame schema back into pd.DataFrame object
            # if result_type == PandasDataFrame:
            #     import pandas as pd

            #     result = pd.DataFrame(**result)
            # elif result_type == PandasSeries:
            #     import pandas as pd

            #     result = pd.Series(**result)

        # apply custom validation
        if self.result_validator is not None:
            result = self.result_validator(result)

        return result


def _generate_result_schema(result_type: type[T]) -> type[T]:
    if result_type is None:
        return None

    result_schema = None
    # try loading pydantic-compatible schemas
    try:
        TypeAdapter(result_type)
        result_schema = result_type
    except PydanticSchemaGenerationError:
        pass
    if result_schema is None:
        raise ValueError(
            f"Could not load or infer schema for result type {result_type}. "
            "Please use a custom type or add compatibility."
        )
    return result_schema
