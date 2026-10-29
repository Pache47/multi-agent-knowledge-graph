"""Technician chat: the answer first, its sources one click away, the work visible while it runs.

Rendering never asks a model to rewrite a verified answer. Counts, records and stock come
from stored tool outputs; claims keep their reviewed wording and references.
"""

import json
from pathlib import Path
from uuid import uuid4

import streamlit as st

from factory_assistant.answer_models import EvidenceCard
from factory_assistant.assistant import TECHNICIAN_CARD, answer_question
from factory_assistant.assistant_contracts import AssistantResult
from factory_assistant.display import EVENT_LABELS, equipment_label
from factory_assistant.settings_ui import readable_time
from factory_assistant.source_view import source_link

SECTION_TITLES = {
    "findings": "What the records show",
    "possible_causes": "Possible causes to check",
    "next_steps": "Next steps",
}
TOOL_STEPS = {
    "find_equipment": "Identifying the equipment",
    "troubleshooting_context": "Gathering {equipment_id}'s setup, manual, events and history",
    "query_events": "Counting recorded events on {equipment_id}",
    "query_history": "Reading past maintenance on {equipment_id}",
    "approved_parts": "Checking approved parts for {equipment_id}",
    "query_stock": "Checking approved parts and recorded stock for {equipment_id}",
    "search_passages": "Searching manuals and notes",
    "walk_graph": "Following connections in the knowledge graph",
    "search_documents": "Searching the documents",
    "read_document": "Reading a document",
}


def event_label(code: str) -> str:
    return EVENT_LABELS.get(code, code.replace("_", " ").capitalize())


def event_summary(card: EvidenceCard) -> dict:
    """Counts by event type, so 'three records' cannot be read as 'three stops'."""
    values = json.loads(card.text)
    by_code = values["by_code"]
    if by_code:
        headline = " · ".join(
            f"{count} {event_label(code).lower()}" for code, count in by_code.items()
        )
    else:
        headline = (
            f"0 {event_label(values['event_code']).lower()}" if values["event_code"] else "0 events"
        )
    total = values["count"]
    scope = f"{total} matching record{'s' if total != 1 else ''} · Available records only"
    return {"headline": headline, "scope": scope, "equipment": equipment_label(values["equipment"])}


def selected_fact_ids(result: AssistantResult) -> set[str]:
    """Never promote a withheld draft's selected facts into a displayed answer."""
    if result.response:
        return set(result.response.fact_ids)
    if result.status == "evidence_only":
        return {fact.id for fact in result.facts}
    return set()


def cited_sources(result: AssistantResult) -> tuple[dict, dict]:
    """Group exact cited excerpts by source file; numbers are what the technician sees."""
    cards = {card.id: card for card in result.evidence}
    citations = [
        citation
        for fact in result.facts
        if fact.id in selected_fact_ids(result)
        for citation in fact.citations
    ]
    if result.response:
        citations += [citation for claim in result.response.claims for citation in claim.citations]
    sources, card_numbers = {}, {}
    for citation in dict.fromkeys(citations):
        numbers = []
        for ref in cards[citation].evidence:
            source = sources.setdefault(
                (ref.path, ref.sha256),
                {
                    "number": len(sources) + 1,
                    "name": Path(ref.path).name,
                    "path": ref.path,
                    "source_id": ref.source_id,
                    "refs": {},
                },
            )
            source["refs"][(ref.line, ref.page)] = ref
            if source["number"] not in numbers:
                numbers.append(source["number"])
        card_numbers[citation] = numbers
    return sources, card_numbers


def citation_marks(citations: list[str], card_numbers: dict) -> str:
    numbers = sorted(
        {number for citation in citations for number in card_numbers.get(citation, [])}
    )
    marks = " ".join(f"[{number}]" for number in numbers)
    return marks + (" (your report)" if TECHNICIAN_CARD in citations else "")


def render_result(result: AssistantResult, turn_id: str, production_line: str = "main"):
    """The verified answer first; supporting records and sources underneath."""
    cards = {card.id: card for card in result.evidence}
    sources, card_numbers = cited_sources(result)
    facts = [fact for fact in result.facts if fact.id in selected_fact_ids(result)]
    response = result.response
    if result.mode == "offline":
        st.info("Evidence preview: the matching records, without AI interpretation.")
    elif result.status == "evidence_only":
        st.info(result.message)
    elif result.status == "withheld":
        st.warning("I couldn't verify an answer from these records. Try naming the machine.")
    elif result.status == "insufficient_evidence":
        st.info("These records don't contain enough to answer this question.")
    for fact in facts:
        if fact.kind == "event_count":
            summary = event_summary(cards[fact.citations[0]])
            st.markdown(
                f"**Recorded events:** {summary['headline']} "
                f"{citation_marks(fact.citations, card_numbers)}"
            )
            st.caption(summary["equipment"] + " · " + summary["scope"])
    if response:
        for section, title in SECTION_TITLES.items():
            claims = getattr(response, section)
            if not claims:
                continue
            st.markdown(f"**{title}**")
            st.markdown(
                "\n".join(
                    f"- {claim.text} {citation_marks(claim.citations, card_numbers)}"
                    for claim in claims
                )
            )
            if section == "next_steps":
                for fact in facts:
                    if fact.kind == "safety_boundary":
                        st.warning(
                            fact.text + " " + citation_marks(fact.citations, card_numbers),
                            icon="⚠️",
                        )
        if response.question:
            label = "Which do you mean?" if result.status == "clarification" else "Question for you"
            st.info(f"**{label}** {response.question}", icon="❓")
            st.caption("Reply in the message box to continue.")
        if result.status == "answered" and result.message:
            st.caption(result.message)
    stock = [f for f in facts if f.kind == "stock_record"]
    if stock:
        # Selected stock counts are the answer to a stock question, so they are never folded.
        st.markdown("**Recorded stock**")
        for fact in stock:
            first, *rest = fact.text.splitlines()
            with st.container(border=True):
                st.markdown(f"**{first}** {citation_marks(fact.citations, card_numbers)}")
                st.markdown("  \n".join(rest))
    records = [f for f in facts if f.kind == "maintenance_record"]
    if records:
        # A pure lookup's records are the answer; otherwise they support it, collapsed.
        expanded = not (response and response.claims)
        st.markdown("**Supporting records**" if not expanded else "**Matching records**")
        for fact in records:
            with st.expander(
                f"Maintenance record · {fact.text.splitlines()[0]}", expanded=expanded
            ):
                for line in fact.text.splitlines()[1:]:
                    st.write(line)
                st.caption("Sources " + citation_marks(fact.citations, card_numbers))
    if sources:
        names = " · ".join(f"[{s['number']}] {s['name']}" for s in sources.values())
        with st.expander(f"📄 Sources: {names}"):
            st.caption("Click a file name to open the whole file in a new tab.")
            for source in sources.values():
                link = source_link(
                    source["source_id"], production_line, {r.line for r in source["refs"].values()}
                )
                st.markdown(f"**[{source['number']}] [{source['name']}]({link})** ↗")
                lines = []
                for ref in sorted(source["refs"].values(), key=lambda r: r.line):
                    location = f"p.{ref.page} " if ref.page else ""
                    lines.append(f"{location}line {ref.line:>3}  {ref.excerpt}")
                st.code("\n".join(lines), language=None, wrap_lines=True)
    with st.expander("How this answer was produced"):
        tools = [t for t in result.trace if t.get("tool")]
        st.write(
            f"{len(tools)} lookups · records up to {readable_time(result.as_of)} · "
            f"model {result.model or 'none'} · reviewer {result.verifier_model or 'none'}"
        )
        for step in tools:
            st.caption(f"`{step['tool']}` {json.dumps(step.get('arguments', {}))}")
        st.caption(
            "Every statement was checked against its cited sources by a separate reviewer; "
            "reviews are fallible, so treat answers as advice."
        )
        st.download_button(
            "Download answer and evidence",
            result.model_dump_json(indent=2),
            file_name="assistant-evidence.json",
            mime="application/json",
            key="download-" + turn_id,
        )


def conversation_context(turns: list[dict], message: str) -> str:
    """Keep the technician's own messages (not generated claims) as follow-up context."""
    if not turns:
        return message
    previous = "\n".join(turn["message"] for turn in turns)
    context = "Earlier technician messages:\n" + previous + "\nCurrent question:\n" + message
    if len(context) > 4000:
        raise ValueError(
            "This conversation is full. Start a new chat and include the equipment ID."
        )
    return context


def describe(step: dict) -> str:
    if step["stage"] == "tool":
        template = TOOL_STEPS.get(step["tool"], "Looking up records")
        label = template.format(**{"equipment_id": "the machine", **step["arguments"]})
        return f"~~{label}~~ not possible yet, trying another way" if step.get("failed") else label
    if step["stage"] == "draft":
        return "Writing the answer" if step["attempt"] == 1 else "Correcting the answer"
    return f"Checking {step['claims']} statements against their sources"


def render_chat(root: Path, runtime: Path, settings: dict):
    if settings.get("bundle") is None:
        st.info("The knowledge for this line hasn't been built yet.")
        if st.button("Build it now (replays recorded agent decisions, no model calls)"):
            from factory_assistant.construction_ui import rebuild

            with st.spinner("Building the knowledge graph…"):
                rebuild(root, runtime)
            st.rerun()
        return
    metadata, bundle = settings["metadata"], settings["bundle"]
    signature = (
        settings["line"],
        bundle.fingerprint,
        settings["cutoff"],
        settings["mode"],
        settings["search"],
    )
    if st.session_state.get("chat_signature") != signature:
        st.session_state["chat_turns"] = []
        st.session_state["chat_signature"] = signature
    turns = st.session_state.setdefault("chat_turns", [])
    top = st.columns([4, 1])
    top[0].caption(
        f"{metadata['title']} · records up to {readable_time(settings['cutoff'])} · "
        + ("live AI answers" if settings["mode"] == "live" else "evidence preview")
    )
    if top[1].button("New chat", disabled=not turns, use_container_width=True):
        st.session_state["chat_turns"] = []
        st.rerun()
    # A submitted message is queued and the page redrawn, so the answer runs on a clean page.
    pending = st.session_state.pop("pending_message", None)
    chosen = None
    # One slot for the whole conversation: a new run replaces it at once, with no stale view.
    body = st.empty()
    with body.container():
        if not turns and not pending:
            st.markdown(
                "#### What happened on the line?\n"
                "Describe the stop or ask about a machine. I'll gather its records, manual, "
                "history and parts, and show where every statement comes from."
            )
            examples = metadata.get("examples") or [
                {"label": "Example", "question": metadata["question"]}
            ]
            columns = st.columns(min(len(examples), 3))
            for index, example in enumerate(examples):
                if columns[index % len(columns)].button(
                    example["label"],
                    help=example["question"],
                    key=f"example-{index}",
                    use_container_width=True,
                ):
                    chosen = example["question"]
        for turn in turns:
            with st.chat_message("user"):
                st.write(turn["message"])
            with st.chat_message("assistant"):
                render_result(
                    AssistantResult.model_validate(turn["result"]), turn["id"], settings["line"]
                )
        if pending:
            try:
                context = conversation_context(turns, pending)
            except ValueError as exc:
                st.error(str(exc))
            else:
                answer(runtime, settings, turns, pending, context)
            st.rerun()
    message = chosen or st.chat_input(
        "Describe the problem, or reply to the assistant…", max_chars=4000
    )
    if message and message.strip():
        st.session_state["pending_message"] = message.strip()
        st.rerun()


def answer(runtime, settings, turns, message, context):
    """Run one question in the conversation area, showing each step as it happens."""
    metadata, bundle = settings["metadata"], settings["bundle"]
    with st.chat_message("user"):
        st.write(message)
    with st.chat_message("assistant"):
        with st.status("Working on it…", expanded=True) as status:
            result = answer_question(
                bundle,
                context,
                settings["cutoff"],
                metadata["dataset"],
                runtime,
                mode=settings["mode"],
                search_mode=settings["search"],
                progress=lambda step: status.write(describe(step)),
            )
            status.update(label="Done", state="complete", expanded=False)
        turn_id = uuid4().hex
        folder = runtime / "assistant_runs"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{turn_id}.json").write_text(result.model_dump_json(indent=2))
        turns.append({"id": turn_id, "message": message.strip(), "result": result.model_dump()})
