"""LangGraph state for the chat graph. See BUILD_SPEC.md section 6.5.

resolve_scope's validation (does every doc_id belong to this user or is
public, and is it ready?) runs in the chat route before the SSE stream opens,
not as a graph node: by the time the graph runs, the HTTP response has
already committed to 200 with an event-stream body, so a 404 for an unknown
or foreign document id can only be returned before that point. The graph
itself starts from condense, trusting resolved_doc_ids.
"""

import operator
from typing import Annotated, TypedDict

from app.vectorindex import ChunkCandidate


class ChatState(TypedDict, total=False):
    question: str
    history: list[dict]
    user_id: str
    resolved_doc_ids: list[str]
    standalone_query: str
    candidates: list[ChunkCandidate]
    relevant: list[ChunkCandidate]
    rewrites: int
    answer: str
    citations: list[dict]
    grounding: dict | None
    trace: Annotated[list[dict], operator.add]
