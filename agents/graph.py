"""
graph.py
--------
Wires monitor_agent → analyst_agent → communicator_agent into a LangGraph
StateGraph.  The graph runs linearly; if monitor returns 0 anomalies the
analyst and communicator nodes are skipped and the run ends immediately.
"""

from __future__ import annotations

import traceback
from datetime import date
from typing import TypedDict

from langgraph.graph import StateGraph, END

from agents.monitor_agent      import run as _monitor_run
from agents.analyst_agent      import run as _analyst_run
from agents.communicator_agent import run as _communicator_run


# ── shared state ──────────────────────────────────────────────────────────────

class PipelineState(TypedDict):
    """Mutable state passed between every node in the graph."""
    run_date:    date         # reference date for the pipeline run
    anomalies:   list[dict]   # raw anomaly records from monitor_agent
    diagnoses:   list[dict]   # enriched records from analyst_agent
    emails_sent: int          # count of emails sent/printed by communicator_agent
    errors:      list[str]    # any error messages accumulated during the run


# ── nodes ─────────────────────────────────────────────────────────────────────

def monitor_node(state: PipelineState) -> PipelineState:
    """
    Call monitor_agent.run() and store the returned anomaly list in state.
    Any exception is caught, appended to state['errors'], and the node returns
    an empty anomaly list so the conditional edge skips downstream nodes.
    """
    print("\n── [graph] monitor_node ──────────────────────────────────────────")
    try:
        anomalies = _monitor_run(ref_date=state["run_date"])
        return {**state, "anomalies": anomalies}
    except Exception as exc:
        msg = f"monitor_node failed: {exc}"
        print(f"[graph] ERROR {msg}")
        traceback.print_exc()
        return {**state, "anomalies": [], "errors": state["errors"] + [msg]}


def analyst_node(state: PipelineState) -> PipelineState:
    """
    Call analyst_agent.run() with the anomaly list and store diagnoses in state.
    """
    print("\n── [graph] analyst_node ──────────────────────────────────────────")
    try:
        diagnoses = _analyst_run(state["anomalies"], ref_date=state["run_date"])
        return {**state, "diagnoses": diagnoses}
    except Exception as exc:
        msg = f"analyst_node failed: {exc}"
        print(f"[graph] ERROR {msg}")
        traceback.print_exc()
        return {**state, "diagnoses": [], "errors": state["errors"] + [msg]}


def communicator_node(state: PipelineState) -> PipelineState:
    """
    Call communicator_agent.run() with the diagnosed anomalies and record how
    many emails were sent.
    """
    print("\n── [graph] communicator_node ─────────────────────────────────────")
    try:
        emails_sent = _communicator_run(state["diagnoses"], ref_date=state["run_date"])
        return {**state, "emails_sent": emails_sent}
    except Exception as exc:
        msg = f"communicator_node failed: {exc}"
        print(f"[graph] ERROR {msg}")
        traceback.print_exc()
        return {**state, "errors": state["errors"] + [msg]}


# ── routing ───────────────────────────────────────────────────────────────────

def _route_after_monitor(state: PipelineState) -> str:
    """
    Route to 'analyst' when anomalies were found, otherwise skip straight to END.
    Also skips if monitor itself raised an error (anomaly list will be empty).
    """
    if state["anomalies"]:
        return "analyst"
    print("[graph] No anomalies detected — skipping analyst and communicator.")
    return END


# ── graph construction ────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """Construct and compile the anomaly-monitoring pipeline graph."""
    g = StateGraph(PipelineState)

    g.add_node("monitor",      monitor_node)
    g.add_node("analyst",      analyst_node)
    g.add_node("communicator", communicator_node)

    g.set_entry_point("monitor")

    # Conditional branch after monitor: anomalies? → analyst, else → END
    g.add_conditional_edges(
        "monitor",
        _route_after_monitor,
        {"analyst": "analyst", END: END},
    )

    # Linear path for the happy case
    g.add_edge("analyst",      "communicator")
    g.add_edge("communicator", END)

    return g.compile()


# ── convenience runner ────────────────────────────────────────────────────────

def run_pipeline(ref_date: date | None = None) -> PipelineState:
    """
    Build the graph, seed the initial state, and invoke the pipeline.

    Parameters
    ----------
    ref_date : date, optional
        Date to treat as 'today'. Defaults to date.today().

    Returns
    -------
    PipelineState
        The final state after all nodes have executed.
    """
    graph = build_graph()

    initial_state: PipelineState = {
        "run_date":    ref_date or date.today(),
        "anomalies":   [],
        "diagnoses":   [],
        "emails_sent": 0,
        "errors":      [],
    }

    final_state: PipelineState = graph.invoke(initial_state)
    return final_state
