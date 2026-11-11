"""A deliberately tiny, LangGraph-shaped state graph.

    g = StateGraph()
    g.add_node("intro", intro_fn)        # async fn(state) -> next node name | END
    g.set_entry("intro")
    await g.run(state)

Nodes share one mutable `state` object and return the name of the next node, so the
routing logic lives next to the work. Swapping to LangGraph (or another graph
framework) later means wrapping these same node functions — nothing else changes.
"""
from __future__ import annotations

from typing import Awaitable, Callable

END = "__end__"
Node = Callable[[object], Awaitable[str]]


class GraphError(Exception):
    pass


class StateGraph:
    def __init__(self, name: str = "graph"):
        self.name = name
        self.nodes: dict[str, Node] = {}
        self.entry: str | None = None
        self.on_transition: Callable[[str, str], None] | None = None

    def add_node(self, name: str, fn: Node) -> "StateGraph":
        if name in self.nodes or name == END:
            raise GraphError(f"duplicate or reserved node name {name!r}")
        self.nodes[name] = fn
        return self

    def set_entry(self, name: str) -> "StateGraph":
        self.entry = name
        return self

    async def run(self, state, max_steps: int = 100_000) -> str:
        """Run until a node returns END. Returns the last node executed."""
        if self.entry is None:
            raise GraphError("no entry node")
        current, last = self.entry, self.entry
        for _ in range(max_steps):
            if current == END:
                return last
            fn = self.nodes.get(current)
            if fn is None:
                raise GraphError(f"{self.name}: unknown node {current!r}")
            nxt = await fn(state)
            if self.on_transition:
                self.on_transition(current, nxt)
            last, current = current, nxt
        raise GraphError(f"{self.name}: exceeded {max_steps} steps")
