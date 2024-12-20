import sys
from collections import defaultdict, deque
from functools import partial
from hashlib import sha1
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    Union,
    cast,
    overload,
)
from uuid import UUID

from langchain_core.callbacks.manager import AsyncParentRunManager, ParentRunManager
from langchain_core.runnables.config import RunnableConfig

from langgraph.channels.base import BaseChannel
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    PendingWrite,
    V,
    copy_checkpoint,
)
from langgraph.constants import (
    CONF,
    CONFIG_KEY_CHECKPOINT_ID,
    CONFIG_KEY_CHECKPOINT_MAP,
    CONFIG_KEY_CHECKPOINT_NS,
    CONFIG_KEY_CHECKPOINTER,
    CONFIG_KEY_READ,
    CONFIG_KEY_SCRATCHPAD,
    CONFIG_KEY_SEND,
    CONFIG_KEY_STORE,
    CONFIG_KEY_TASK_ID,
    CONFIG_KEY_WRITES,
    EMPTY_SEQ,
    ERROR,
    INTERRUPT,
    NO_WRITES,
    NS_END,
    NS_SEP,
    NULL_TASK_ID,
    PULL,
    PUSH,
    RESERVED,
    RESUME,
    RETURN,
    TAG_HIDDEN,
    TASKS,
    Send,
)
from langgraph.errors import EmptyChannelError, InvalidUpdateError
from langgraph.managed.base import ManagedValueMapping
from langgraph.pregel.call import get_runnable_for_func
from langgraph.pregel.io import read_channel, read_channels
from langgraph.pregel.log import logger
from langgraph.pregel.manager import ChannelsManager
from langgraph.pregel.read import PregelNode
from langgraph.store.base import BaseStore
from langgraph.types import (
    All,
    LoopProtocol,
    PregelExecutableTask,
    PregelTask,
    RetryPolicy,
)
from langgraph.utils.config import merge_configs, patch_config

GetNextVersion = Callable[[Optional[V], BaseChannel], V]
SUPPORTS_EXC_NOTES = sys.version_info >= (3, 11)


class WritesProtocol(Protocol):
    """Protocol for objects containing writes to be applied to checkpoint.
    Implemented by PregelTaskWrites and PregelExecutableTask."""

    @property
    def path(self) -> tuple[Union[str, int, tuple], ...]: ...

    @property
    def name(self) -> str: ...

    @property
    def writes(self) -> Sequence[tuple[str, Any]]: ...

    @property
    def triggers(self) -> Sequence[str]: ...


class PregelTaskWrites(NamedTuple):
    """Simplest implementation of WritesProtocol, for usage with writes that
    don't originate from a runnable task, eg. graph input, update_state, etc."""

    path: tuple[Union[str, int, tuple], ...]
    name: str
    writes: Sequence[tuple[str, Any]]
    triggers: Sequence[str]


class Call:
    __slots__ = ("func", "input", "retry")

    func: Callable
    input: Any
    retry: Optional[RetryPolicy]

    def __init__(
        self, func: Callable, input: Any, *, retry: Optional[RetryPolicy]
    ) -> None:
        self.func = func
        self.input = input
        self.retry = retry


def should_interrupt(
    checkpoint: Checkpoint,
    interrupt_nodes: Union[All, Sequence[str]],
    tasks: Iterable[PregelExecutableTask],
) -> list[PregelExecutableTask]:
    """Check if the graph should be interrupted based on current state."""
    version_type = type(next(iter(checkpoint["channel_versions"].values()), None))
    null_version = version_type()  # type: ignore[misc]
    seen = checkpoint["versions_seen"].get(INTERRUPT, {})
    # interrupt if any channel has been updated since last interrupt
    any_updates_since_prev_interrupt = any(
        version > seen.get(chan, null_version)  # type: ignore[operator]
        for chan, version in checkpoint["channel_versions"].items()
    )
    # and any triggered node is in interrupt_nodes list
    return (
        [
            task
            for task in tasks
            if (
                (
                    not task.config
                    or TAG_HIDDEN not in task.config.get("tags", EMPTY_SEQ)
                )
                if interrupt_nodes == "*"
                else task.name in interrupt_nodes
            )
        ]
        if any_updates_since_prev_interrupt
        else []
    )


def local_read(
    step: int,
    checkpoint: Checkpoint,
    channels: Mapping[str, BaseChannel],
    managed: ManagedValueMapping,
    task: WritesProtocol,
    config: RunnableConfig,
    select: Union[list[str], str],
    fresh: bool = False,
) -> Union[dict[str, Any], Any]:
    """Function injected under CONFIG_KEY_READ in task config, to read current state.
    Used by conditional edges to read a copy of the state with reflecting the writes
    from that node only."""
    if isinstance(select, str):
        managed_keys = []
        for c, _ in task.writes:
            if c == select:
                updated = {c}
                break
        else:
            updated = set()
    else:
        managed_keys = [k for k in select if k in managed]
        select = [k for k in select if k not in managed]
        updated = set(select).intersection(c for c, _ in task.writes)
    if fresh and updated:
        with ChannelsManager(
            {k: v for k, v in channels.items() if k in updated},
            checkpoint,
            LoopProtocol(config=config, step=step, stop=step + 1),
            skip_context=True,
        ) as (local_channels, _):
            apply_writes(copy_checkpoint(checkpoint), local_channels, [task], None)
            values = read_channels({**channels, **local_channels}, select)
    else:
        values = read_channels(channels, select)
    if managed_keys:
        values.update({k: managed[k]() for k in managed_keys})
    return values


def local_write(
    commit: Callable[[Sequence[tuple[str, Any]]], None],
    process_keys: Iterable[str],
    writes: Sequence[tuple[str, Any]],
) -> None:
    """Function injected under CONFIG_KEY_SEND in task config, to write to channels.
    Validates writes and forwards them to `commit` function."""
    for chan, value in writes:
        if chan in (PUSH, TASKS) and value is not None:
            if not isinstance(value, Send):
                raise InvalidUpdateError(f"Expected Send, got {value}")
            if value.node not in process_keys:
                raise InvalidUpdateError(f"Invalid node name {value.node} in packet")
    commit(writes)


def increment(current: Optional[int], channel: BaseChannel) -> int:
    """Default channel versioning function, increments the current int version."""
    return current + 1 if current is not None else 1


def apply_writes(
    checkpoint: Checkpoint,
    channels: Mapping[str, BaseChannel],
    tasks: Iterable[WritesProtocol],
    get_next_version: Optional[GetNextVersion],
) -> dict[str, list[Any]]:
    """Apply writes from a set of tasks (usually the tasks from a Pregel step)
    to the checkpoint and channels, and return managed values writes to be applied
    externally."""
    # sort tasks on path, to ensure deterministic order for update application
    # any path parts after the 3rd are ignored for sorting
    # (we use them for eg. task ids which aren't good for sorting)
    tasks = sorted(tasks, key=lambda t: t.path[:3])
    # if no task has triggers this is applying writes from the null task only
    # so we don't do anything other than update the channels written to
    bump_step = any(t.triggers for t in tasks)

    # update seen versions
    for task in tasks:
        checkpoint["versions_seen"].setdefault(task.name, {}).update(
            {
                chan: checkpoint["channel_versions"][chan]
                for chan in task.triggers
                if chan in checkpoint["channel_versions"]
            }
        )

    # Find the highest version of all channels
    if checkpoint["channel_versions"]:
        max_version = max(checkpoint["channel_versions"].values())
    else:
        max_version = None

    # Consume all channels that were read
    for chan in {
        chan
        for task in tasks
        for chan in task.triggers
        if chan not in RESERVED and chan in channels
    }:
        if channels[chan].consume() and get_next_version is not None:
            checkpoint["channel_versions"][chan] = get_next_version(
                max_version,
                channels[chan],
            )

    # clear pending sends
    if checkpoint["pending_sends"] and bump_step:
        checkpoint["pending_sends"].clear()

    # Group writes by channel
    pending_writes_by_channel: dict[str, list[Any]] = defaultdict(list)
    pending_writes_by_managed: dict[str, list[Any]] = defaultdict(list)
    for task in tasks:
        for chan, val in task.writes:
            if chan in (NO_WRITES, PUSH, RESUME, INTERRUPT, RETURN, ERROR):
                pass
            elif chan == TASKS:  # TODO: remove branch in 1.0
                checkpoint["pending_sends"].append(val)
            elif chan in channels:
                pending_writes_by_channel[chan].append(val)
            else:
                pending_writes_by_managed[chan].append(val)

    # Find the highest version of all channels
    if checkpoint["channel_versions"]:
        max_version = max(checkpoint["channel_versions"].values())
    else:
        max_version = None

    # Apply writes to channels
    updated_channels: set[str] = set()
    for chan, vals in pending_writes_by_channel.items():
        if chan in channels:
            if channels[chan].update(vals) and get_next_version is not None:
                checkpoint["channel_versions"][chan] = get_next_version(
                    max_version,
                    channels[chan],
                )
            updated_channels.add(chan)

    # Channels that weren't updated in this step are notified of a new step
    if bump_step:
        for chan in channels:
            if chan not in updated_channels:
                if channels[chan].update([]) and get_next_version is not None:
                    checkpoint["channel_versions"][chan] = get_next_version(
                        max_version,
                        channels[chan],
                    )

    # Return managed values writes to be applied externally
    return pending_writes_by_managed


@overload
def prepare_next_tasks(
    checkpoint: Checkpoint,
    pending_writes: Sequence[PendingWrite],
    processes: Mapping[str, PregelNode],
    channels: Mapping[str, BaseChannel],
    managed: ManagedValueMapping,
    config: RunnableConfig,
    step: int,
    *,
    for_execution: Literal[False],
    store: Literal[None] = None,
    checkpointer: Literal[None] = None,
    manager: Literal[None] = None,
) -> dict[str, PregelTask]: ...


@overload
def prepare_next_tasks(
    checkpoint: Checkpoint,
    pending_writes: Sequence[PendingWrite],
    processes: Mapping[str, PregelNode],
    channels: Mapping[str, BaseChannel],
    managed: ManagedValueMapping,
    config: RunnableConfig,
    step: int,
    *,
    for_execution: Literal[True],
    store: Optional[BaseStore],
    checkpointer: Optional[BaseCheckpointSaver],
    manager: Union[None, ParentRunManager, AsyncParentRunManager],
) -> dict[str, PregelExecutableTask]: ...


def prepare_next_tasks(
    checkpoint: Checkpoint,
    pending_writes: Sequence[PendingWrite],
    processes: Mapping[str, PregelNode],
    channels: Mapping[str, BaseChannel],
    managed: ManagedValueMapping,
    config: RunnableConfig,
    step: int,
    *,
    for_execution: bool,
    store: Optional[BaseStore] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
    manager: Union[None, ParentRunManager, AsyncParentRunManager] = None,
) -> Union[dict[str, PregelTask], dict[str, PregelExecutableTask]]:
    """Prepare the set of tasks that will make up the next Pregel step.
    This is the union of all PUSH tasks (Sends) and PULL tasks (nodes triggered
    by edges)."""
    tasks: list[Union[PregelTask, PregelExecutableTask]] = []
    # Consume pending_sends from previous step (legacy version of Send)
    for idx, _ in enumerate(checkpoint["pending_sends"]):  # TODO: remove branch in 1.0
        if task := prepare_single_task(
            (PUSH, idx),
            None,
            checkpoint=checkpoint,
            pending_writes=pending_writes,
            processes=processes,
            channels=channels,
            managed=managed,
            config=config,
            step=step,
            for_execution=for_execution,
            store=store,
            checkpointer=checkpointer,
            manager=manager,
        ):
            tasks.append(task)
    # Check if any processes should be run in next step
    # If so, prepare the values to be passed to them
    for name in processes:
        if task := prepare_single_task(
            (PULL, name),
            None,
            checkpoint=checkpoint,
            pending_writes=pending_writes,
            processes=processes,
            channels=channels,
            managed=managed,
            config=config,
            step=step,
            for_execution=for_execution,
            store=store,
            checkpointer=checkpointer,
            manager=manager,
        ):
            tasks.append(task)
    # Consume pending Sends from this step (new version of Send)
    if any(c == PUSH for _, c, _ in pending_writes):
        # group writes by task id
        grouped_by_task = defaultdict(list)
        for tid, c, _ in pending_writes:
            grouped_by_task[tid].append(c)
        # prepare send tasks from grouped writes
        # 1. start from sends originating from existing tasks
        tidx = 0
        while tidx < len(tasks):
            task = tasks[tidx]
            if twrites := grouped_by_task.pop(task.id, None):
                for idx, c in enumerate(twrites):
                    if c != PUSH:
                        continue
                    if next_task := prepare_single_task(
                        (PUSH, task.path, idx, task.id),
                        None,
                        checkpoint=checkpoint,
                        pending_writes=pending_writes,
                        processes=processes,
                        channels=channels,
                        managed=managed,
                        config=config,
                        step=step,
                        for_execution=for_execution,
                        store=store,
                        checkpointer=checkpointer,
                        manager=manager,
                    ):
                        tasks.append(next_task)
            tidx += 1
        # key tasks by id
        task_map = {t.id: t for t in tasks}
        # 2. create new tasks for remaining sends (eg. from update_state)
        for tid, writes in grouped_by_task.items():
            task = task_map.get(tid)
            for idx, c in enumerate(writes):
                if c != PUSH:
                    continue
                if next_task := prepare_single_task(
                    (PUSH, task.path if task else (), idx, tid),
                    None,
                    checkpoint=checkpoint,
                    pending_writes=pending_writes,
                    processes=processes,
                    channels=channels,
                    managed=managed,
                    config=config,
                    step=step,
                    for_execution=for_execution,
                    store=store,
                    checkpointer=checkpointer,
                    manager=manager,
                ):
                    task_map[next_task.id] = next_task
    else:
        task_map = {t.id: t for t in tasks}
    return task_map


def prepare_single_task(
    task_path: tuple[Any, ...],
    task_id_checksum: Optional[str],
    *,
    checkpoint: Checkpoint,
    pending_writes: Sequence[PendingWrite],
    processes: Mapping[str, PregelNode],
    channels: Mapping[str, BaseChannel],
    managed: ManagedValueMapping,
    config: RunnableConfig,
    step: int,
    for_execution: bool,
    store: Optional[BaseStore] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
    manager: Union[None, ParentRunManager, AsyncParentRunManager] = None,
) -> Union[None, PregelTask, PregelExecutableTask]:
    """Prepares a single task for the next Pregel step, given a task path, which
    uniquely identifies a PUSH or PULL task within the graph."""
    checkpoint_id = UUID(checkpoint["id"]).bytes
    configurable = config.get(CONF, {})
    parent_ns = configurable.get(CONFIG_KEY_CHECKPOINT_NS, "")

    if task_path[0] == PUSH and isinstance(task_path[-1], Call):
        # (PUSH, parent task path, idx of PUSH write, id of parent task, Call)
        task_path_t = cast(tuple[str, tuple, int, str, Call], task_path)
        call = task_path_t[-1]
        proc_ = get_runnable_for_func(call.func)
        name = proc_.name
        if name is None:
            raise ValueError("`call` functions must have a `__name__` attribute")
        # create task id
        triggers = [PUSH]
        checkpoint_ns = f"{parent_ns}{NS_SEP}{name}" if parent_ns else name
        task_id = _uuid5_str(
            checkpoint_id,
            checkpoint_ns,
            str(step),
            name,
            PUSH,
            _tuple_str(task_path[1]),
            str(task_path[2]),
        )
        task_checkpoint_ns = f"{checkpoint_ns}:{task_id}"
        metadata = {
            "langgraph_step": step,
            "langgraph_node": name,
            "langgraph_triggers": triggers,
            "langgraph_path": task_path[:3],
            "langgraph_checkpoint_ns": task_checkpoint_ns,
        }
        if task_id_checksum is not None:
            assert task_id == task_id_checksum, f"{task_id} != {task_id_checksum}"
        if for_execution:
            writes: deque[tuple[str, Any]] = deque()
            return PregelExecutableTask(
                name,
                call.input,
                proc_,
                writes,
                patch_config(
                    merge_configs(config, {"metadata": metadata}),
                    run_name=name,
                    callbacks=(
                        manager.get_child(f"graph:step:{step}") if manager else None
                    ),
                    configurable={
                        CONFIG_KEY_TASK_ID: task_id,
                        # deque.extend is thread-safe
                        CONFIG_KEY_SEND: partial(
                            local_write,
                            writes.extend,
                            processes.keys(),
                        ),
                        CONFIG_KEY_READ: partial(
                            local_read,
                            step,
                            checkpoint,
                            channels,
                            managed,
                            PregelTaskWrites(task_path[:3], name, writes, triggers),
                            config,
                        ),
                        CONFIG_KEY_STORE: (store or configurable.get(CONFIG_KEY_STORE)),
                        CONFIG_KEY_CHECKPOINTER: (
                            checkpointer or configurable.get(CONFIG_KEY_CHECKPOINTER)
                        ),
                        CONFIG_KEY_CHECKPOINT_MAP: {
                            **configurable.get(CONFIG_KEY_CHECKPOINT_MAP, {}),
                            parent_ns: checkpoint["id"],
                        },
                        CONFIG_KEY_CHECKPOINT_ID: None,
                        CONFIG_KEY_CHECKPOINT_NS: task_checkpoint_ns,
                        CONFIG_KEY_WRITES: [
                            w
                            for w in pending_writes
                            + configurable.get(CONFIG_KEY_WRITES, [])
                            if w[0] in (NULL_TASK_ID, task_id)
                        ],
                        CONFIG_KEY_SCRATCHPAD: {},
                    },
                ),
                triggers,
                call.retry,
                None,
                task_id,
                task_path[:3],
            )
        else:
            return PregelTask(task_id, name, task_path[:3])
    elif task_path[0] == PUSH:
        if len(task_path) == 2:  # TODO: remove branch in 1.0
            # legacy SEND tasks, executed in superstep n+1
            # (PUSH, idx of pending send)
            idx = cast(int, task_path[1])
            if idx >= len(checkpoint["pending_sends"]):
                return
            packet = checkpoint["pending_sends"][idx]
            if not isinstance(packet, Send):
                logger.warning(
                    f"Ignoring invalid packet type {type(packet)} in pending sends"
                )
                return
            if packet.node not in processes:
                logger.warning(
                    f"Ignoring unknown node name {packet.node} in pending sends"
                )
                return
            # create task id
            triggers = [PUSH]
            checkpoint_ns = (
                f"{parent_ns}{NS_SEP}{packet.node}" if parent_ns else packet.node
            )
            task_id = _uuid5_str(
                checkpoint_id,
                checkpoint_ns,
                str(step),
                packet.node,
                PUSH,
                str(idx),
            )
        elif len(task_path) >= 4:
            # new PUSH tasks, executed in superstep n
            # (PUSH, parent task path, idx of PUSH write, id of parent task)
            task_path_tt = cast(tuple[str, tuple, int, str], task_path)
            writes_for_path = [w for w in pending_writes if w[0] == task_path_tt[3]]
            if task_path_tt[2] >= len(writes_for_path):
                logger.warning(
                    f"Ignoring invalid write index {task_path[2]} in pending writes"
                )
                return
            packet = writes_for_path[task_path_tt[2]][2]
            if packet is None:
                return
            if not isinstance(packet, Send):
                logger.warning(
                    f"Ignoring invalid packet type {type(packet)} in pending writes"
                )
                return
            if packet.node not in processes:
                logger.warning(
                    f"Ignoring unknown node name {packet.node} in pending writes"
                )
                return
            # create task id
            triggers = [PUSH]
            checkpoint_ns = (
                f"{parent_ns}{NS_SEP}{packet.node}" if parent_ns else packet.node
            )
            task_id = _uuid5_str(
                checkpoint_id,
                checkpoint_ns,
                str(step),
                packet.node,
                PUSH,
                _tuple_str(task_path[1]),
                str(task_path[2]),
            )
        else:
            logger.warning(f"Ignoring invalid PUSH task path {task_path}")
            return
        task_checkpoint_ns = f"{checkpoint_ns}:{task_id}"
        metadata = {
            "langgraph_step": step,
            "langgraph_node": packet.node,
            "langgraph_triggers": triggers,
            "langgraph_path": task_path[:3],
            "langgraph_checkpoint_ns": task_checkpoint_ns,
        }
        if task_id_checksum is not None:
            assert task_id == task_id_checksum, f"{task_id} != {task_id_checksum}"
        if for_execution:
            proc = processes[packet.node]
            if node := proc.node:
                if proc.metadata:
                    metadata.update(proc.metadata)
                writes = deque()
                return PregelExecutableTask(
                    packet.node,
                    packet.arg,
                    node,
                    writes,
                    patch_config(
                        merge_configs(
                            config, {"metadata": metadata, "tags": proc.tags}
                        ),
                        run_name=packet.node,
                        callbacks=(
                            manager.get_child(f"graph:step:{step}") if manager else None
                        ),
                        configurable={
                            CONFIG_KEY_TASK_ID: task_id,
                            # deque.extend is thread-safe
                            CONFIG_KEY_SEND: partial(
                                local_write,
                                writes.extend,
                                processes.keys(),
                            ),
                            CONFIG_KEY_READ: partial(
                                local_read,
                                step,
                                checkpoint,
                                channels,
                                managed,
                                PregelTaskWrites(
                                    task_path[:3], packet.node, writes, triggers
                                ),
                                config,
                            ),
                            CONFIG_KEY_STORE: (
                                store or configurable.get(CONFIG_KEY_STORE)
                            ),
                            CONFIG_KEY_CHECKPOINTER: (
                                checkpointer
                                or configurable.get(CONFIG_KEY_CHECKPOINTER)
                            ),
                            CONFIG_KEY_CHECKPOINT_MAP: {
                                **configurable.get(CONFIG_KEY_CHECKPOINT_MAP, {}),
                                parent_ns: checkpoint["id"],
                            },
                            CONFIG_KEY_CHECKPOINT_ID: None,
                            CONFIG_KEY_CHECKPOINT_NS: task_checkpoint_ns,
                            CONFIG_KEY_WRITES: [
                                w
                                for w in pending_writes
                                + configurable.get(CONFIG_KEY_WRITES, [])
                                if w[0] in (NULL_TASK_ID, task_id)
                            ],
                            CONFIG_KEY_SCRATCHPAD: {},
                        },
                    ),
                    triggers,
                    proc.retry_policy,
                    None,
                    task_id,
                    task_path[:3],
                    writers=proc.flat_writers,
                )
        else:
            return PregelTask(task_id, packet.node, task_path[:3])
    elif task_path[0] == PULL:
        # (PULL, node name)
        name = cast(str, task_path[1])
        if name not in processes:
            return
        proc = processes[name]
        version_type = type(next(iter(checkpoint["channel_versions"].values()), None))
        null_version = version_type()  # type: ignore[misc]
        if null_version is None:
            return
        seen = checkpoint["versions_seen"].get(name, {})
        # If any of the channels read by this process were updated
        if triggers := sorted(
            chan
            for chan in proc.triggers
            if not isinstance(
                read_channel(channels, chan, return_exception=True), EmptyChannelError
            )
            and checkpoint["channel_versions"].get(chan, null_version)  # type: ignore[operator]
            > seen.get(chan, null_version)
        ):
            try:
                val = next(
                    _proc_input(proc, managed, channels, for_execution=for_execution)
                )
            except StopIteration:
                return
            except Exception as exc:
                if SUPPORTS_EXC_NOTES:
                    exc.add_note(
                        f"Before task with name '{name}' and path '{task_path[:3]}'"
                    )
                raise

            # create task id
            checkpoint_ns = f"{parent_ns}{NS_SEP}{name}" if parent_ns else name
            task_id = _uuid5_str(
                checkpoint_id,
                checkpoint_ns,
                str(step),
                name,
                PULL,
                *triggers,
            )
            task_checkpoint_ns = f"{checkpoint_ns}{NS_END}{task_id}"
            metadata = {
                "langgraph_step": step,
                "langgraph_node": name,
                "langgraph_triggers": triggers,
                "langgraph_path": task_path[:3],
                "langgraph_checkpoint_ns": task_checkpoint_ns,
            }
            if task_id_checksum is not None:
                assert task_id == task_id_checksum
            if for_execution:
                if node := proc.node:
                    if proc.metadata:
                        metadata.update(proc.metadata)
                    writes = deque()
                    return PregelExecutableTask(
                        name,
                        val,
                        node,
                        writes,
                        patch_config(
                            merge_configs(
                                config, {"metadata": metadata, "tags": proc.tags}
                            ),
                            run_name=name,
                            callbacks=(
                                manager.get_child(f"graph:step:{step}")
                                if manager
                                else None
                            ),
                            configurable={
                                CONFIG_KEY_TASK_ID: task_id,
                                # deque.extend is thread-safe
                                CONFIG_KEY_SEND: partial(
                                    local_write,
                                    writes.extend,
                                    processes.keys(),
                                ),
                                CONFIG_KEY_READ: partial(
                                    local_read,
                                    step,
                                    checkpoint,
                                    channels,
                                    managed,
                                    PregelTaskWrites(
                                        task_path[:3], name, writes, triggers
                                    ),
                                    config,
                                ),
                                CONFIG_KEY_STORE: (
                                    store or configurable.get(CONFIG_KEY_STORE)
                                ),
                                CONFIG_KEY_CHECKPOINTER: (
                                    checkpointer
                                    or configurable.get(CONFIG_KEY_CHECKPOINTER)
                                ),
                                CONFIG_KEY_CHECKPOINT_MAP: {
                                    **configurable.get(CONFIG_KEY_CHECKPOINT_MAP, {}),
                                    parent_ns: checkpoint["id"],
                                },
                                CONFIG_KEY_CHECKPOINT_ID: None,
                                CONFIG_KEY_CHECKPOINT_NS: task_checkpoint_ns,
                                CONFIG_KEY_WRITES: [
                                    w
                                    for w in pending_writes
                                    + configurable.get(CONFIG_KEY_WRITES, [])
                                    if w[0] in (NULL_TASK_ID, task_id)
                                ],
                                CONFIG_KEY_SCRATCHPAD: {},
                            },
                        ),
                        triggers,
                        proc.retry_policy,
                        None,
                        task_id,
                        task_path[:3],
                        writers=proc.flat_writers,
                    )
            else:
                return PregelTask(task_id, name, task_path[:3])


def _proc_input(
    proc: PregelNode,
    managed: ManagedValueMapping,
    channels: Mapping[str, BaseChannel],
    *,
    for_execution: bool,
) -> Iterator[Any]:
    """Prepare input for a PULL task, based on the process's channels and triggers."""
    # If all trigger channels subscribed by this process are not empty
    # then invoke the process with the values of all non-empty channels
    if isinstance(proc.channels, dict):
        try:
            val: dict[str, Any] = {}
            for k, chan in proc.channels.items():
                if chan in proc.triggers:
                    val[k] = read_channel(channels, chan, catch=False)
                elif chan in channels:
                    try:
                        val[k] = read_channel(channels, chan, catch=False)
                    except EmptyChannelError:
                        continue
                else:
                    val[k] = managed[k]()
        except EmptyChannelError:
            return
    elif isinstance(proc.channels, list):
        for chan in proc.channels:
            try:
                val = read_channel(channels, chan, catch=False)
                break
            except EmptyChannelError:
                pass
        else:
            return
    else:
        raise RuntimeError(
            "Invalid channels type, expected list or dict, got {proc.channels}"
        )

    # If the process has a mapper, apply it to the value
    if for_execution and proc.mapper is not None:
        val = proc.mapper(val)

    yield val


def _uuid5_str(namespace: bytes, *parts: str) -> str:
    """Generate a UUID from the SHA-1 hash of a namespace UUID and a name."""

    sha = sha1(namespace, usedforsecurity=False)
    sha.update(b"".join(p.encode() for p in parts))
    hex = sha.hexdigest()
    return f"{hex[:8]}-{hex[8:12]}-{hex[12:16]}-{hex[16:20]}-{hex[20:32]}"


def _tuple_str(tup: Union[str, int, tuple]) -> str:
    """Generate a string representation of a tuple."""
    return (
        f"({', '.join(_tuple_str(x) for x in tup)})"
        if isinstance(tup, (tuple, list))
        else str(tup)
    )