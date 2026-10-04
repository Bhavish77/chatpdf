"""Builds the LangGraph chat graph: retrieve -> grade -> [rewrite -> retrieve]*
-> generate -> verify. See BUILD_SPEC.md section 6.5.
"""

from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.llm import LLMClient
from app.rag import nodes
from app.rag.state import ChatState
from app.vectorindex import VectorIndex


def build_graph(llm: LLMClient, vectors: VectorIndex, settings: Settings):
    def _after_grade(state: ChatState) -> str:
        candidates = state.get("candidates") or []
        relevant = state.get("relevant") or []
        # Cap the threshold by how many candidates actually exist: with
        # GRADE_MIN_RELEVANT=2 (the default) and e.g. a one-chunk document,
        # "at least 2 relevant" could never be satisfied even if that one
        # chunk is a perfect match. Found live against the real API.
        effective_min = min(settings.GRADE_MIN_RELEVANT, len(candidates)) if candidates else settings.GRADE_MIN_RELEVANT
        if len(relevant) >= effective_min:
            return "generate"
        if state.get("rewrites", 0) < 1:
            return "rewrite"
        return "no_answer"

    def _after_generate(state: ChatState) -> str:
        return "verify" if settings.ENABLE_GROUNDING_CHECK else END

    builder = StateGraph(ChatState)
    builder.add_node("condense", nodes.make_condense(llm, settings))
    builder.add_node("retrieve", nodes.make_retrieve(llm, vectors, settings))
    builder.add_node("grade", nodes.make_grade(llm, settings))
    builder.add_node("rewrite", nodes.make_rewrite(llm, settings))
    builder.add_node("generate", nodes.make_generate(llm, settings))
    builder.add_node("verify", nodes.make_verify(llm, settings))
    builder.add_node("no_answer", nodes.make_no_answer())

    builder.add_edge(START, "condense")
    builder.add_edge("condense", "retrieve")
    builder.add_edge("retrieve", "grade")
    builder.add_conditional_edges(
        "grade", _after_grade, {"generate": "generate", "rewrite": "rewrite", "no_answer": "no_answer"}
    )
    builder.add_edge("rewrite", "retrieve")
    builder.add_conditional_edges("generate", _after_generate, {"verify": "verify", END: END})
    builder.add_edge("verify", END)
    builder.add_edge("no_answer", END)

    return builder.compile()
