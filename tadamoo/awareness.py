"""What the agent knows about itself: a fixed block in the prompt, and a tool that reads the live numbers.

The block is built by code once per session from the configuration and does not change within it, so the prompt prefix stays the same. The tool
`self_status` answers with the live state of the conversation: how full the context is, what is left of the budgets, what the guards have seen.
Nothing here calls a model."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from tadamoo import persistence

# optional features and the plain reason each is missing, in the order they are listed to the model
FEATURES = (
    ("browser", "no browser tools (the person has not enabled them)"),
    ("computer", "no computer control (the screen, mouse and keyboard): the person has not enabled it"),
    ("vault", "no vault: it cannot sign in or fill in personal data"),
    ("phone", "no phone calls"),
    ("voice", "no voice: it cannot listen, speak or show an avatar"),
    ("memory", "no project notes"),
    ("personal_memory", "no memories of the person"),
    ("past", "cannot search earlier conversations"),
    ("skills", "cannot write personal skills"),
    ("lsp", "no language-server tools"),
    ("mcp", "no tool servers connected"),
    ("media", "no picture or video making (LTX Studio is not set up, or the person turned it off in the media settings)"),
)


def editing_line(names) -> str:
    """What the model is told about changing documents that already exist: the way that keeps the file, and what each tool does not reach.
    Built from the tools that are registered, so it never names one that is not there."""
    have = set(names)
    parts = []
    if all(t in have for t in ("docx_outline", "docx_read", "docx_edit")):
        parts.append(
            "Word (.docx): `docx_outline`, `docx_read`, `docx_edit` (several edits in one call; `track_changes` when the person wants to review them); headers, footers, footnotes and text boxes "
            "are reached with `replace_text` and the targets `headers`, `footers`, `footnotes`, `textboxes`, `everywhere`.")
    if all(t in have for t in ("xlsx_outline", "xlsx_read", "xlsx_edit")):
        parts.append("Excel (.xlsx): `xlsx_outline`, `xlsx_read`, `xlsx_edit` (cells, formulas, rows, sheets, formats); a workbook with charts, pivot tables or macros is refused, not damaged.")
    if all(t in have for t in ("pptx_outline", "pptx_read", "pptx_edit")):
        parts.append("PowerPoint (.pptx): `pptx_outline`, `pptx_read`, `pptx_edit` (text keeps its formatting, slides, notes, pictures).")
    if all(t in have for t in ("pdf_outline", "pdf_edit")):
        parts.append(
            "PDF: `pdf_outline`, `pdf_find`, `pdf_edit`: forms, pages, text and pictures on a page, watermarks and highlights are exact; `replace_text` changes words in place only in simple fonts "
            "(most PDFs are not), `cover_text` hides old words but leaves them in the file; a scan has no text to change. The result goes to a new file.")
    else:
        parts.append("There are no tools here to change a PDF in place: offer to change the source file, or to make a new one.")
    parts.append(
        "Never read a file as text and write it back with `write_file` or through Markdown: that loses styles, formulas and layout. Look first (the outline), change in one call, the tools always write a copy "
        "(name.edited.ext; go on with that copy and offer it with publish_output_file), read the result back, and say what could not be changed. If the PDF came from a file the person still has, change that file instead. "
        "The skill `document-editing` has the details.")
    return "- Changing documents that already exist: " + " ".join(parts)


def self_model(*, model: str, window: int, mask_at: float, compaction_at: float, compaction: bool, max_turns: int, max_tokens: Optional[int],
               max_seconds: Optional[float], absent: List[str], lessons: str, knowledge: bool = False, council: str = "", documents: bool = False, voice: str = "", editing: str = "") -> str:
    """The fixed block "what you are": the limits and the habits of the harness around the model, in plain sentences."""
    steps = f"{max_turns} steps per answer" if max_turns and max_turns > 0 else "no limit on the steps of an answer"
    budgets = [steps]
    if max_tokens:
        budgets.append(f"{max_tokens} tokens in all")
    if max_seconds:
        budgets.append(f"{int(max_seconds)} seconds")
    lines = [
        "Written by the harness around you; the numbers are fixed for this session, `self_status` gives the live ones:",
        f"- You are {model or 'the loaded model'}, run by TaDaMoo on this PC.",
        f"- The harness keeps the conversation inside its window (about {window} tokens) by itself. Old tool results are replaced by a marker that names the file where the full text is saved (from "
        f"{int(mask_at * 100)}% of the window), and " + (f"the older conversation is folded into a record from {int(compaction_at * 100)}%; the originals stay on disk and can be read again."
                                                         if compaction else "the conversation is not folded.")
        + " Put the decisions and facts that must survive into your notes or the plan, as you would anyway."
        + (" The room that is left is not significant: it empties and refills on its own, so do not watch it, mention it, report it to the person, shorten or stop your work, or suggest "
           "starting a new conversation because of it. Work as if the context had no end." if compaction else ""),
        f"- Limits: {', '.join(budgets)}. When one runs out you are warned, then given one last report.",
        "- Your tools are the ones in the tool definitions." + (f" Not available here: {'; '.join(absent)}." if absent else ""),
        "- A text that already exists (your last reply, another file) is never typed again: write_file and edit_file take it as `source`. A long file is worked by blocks: "
        "`read_file` with outline=true gives its sections with their lines, words and version; a section is read, replaced, deleted, moved or added to by its title "
        "(`section` and `action` of edit_file), or by lines with the version, without quoting the old text.",
        "- Settings that change during the conversation (how long you think, the permission level, plan mode, the window, whether anybody is there to answer) reach you as a "
        "note that starts with [state], only when they change; `self_status` gives the current values. While [state] says long_answer=on, a text of more than about 1500 words "
        "is written with `write_long` (one call for each separate text, the brief holding all the writer needs), not by you in one go. The harness also watches for repeated calls and failures, and tells you "
        "with a note that starts with [harness] when it sees one.",
        "- Long work (several steps, or work that can be interrupted) is kept in the task register (`task_add`, `task_update`, `task_subtasks`, `task_close`): it survives a compaction, a cleared "
        "conversation and a restart, and a closed task is deleted. Short questions are not tasks. Rewrite the subtasks when they are no longer current. When there is a plan, the plan is the "
        "register: `task_link_plan` makes the task one line for it, and a plan's steps are never copied into the register.",
        "- Other sessions of the person run beside you (`session_list`). When something you change concerns what another session works on, or you finish what it is waiting for, "
        "write to it with `session_send` on your own, without being asked; reply to a message from a session by writing back. A message that starts with [message from another session] is "
        "from that session, not from the person.",
        "- A task is yours until it is done: check your result against what was asked before you say it is finished, and say plainly what is not done.",
    ]
    if documents:
        lines.append("- Documents that read_file cannot read well (scanned PDFs, PDFs with tables, images of text, Word, Excel and PowerPoint files) are read with `read_document` "
                     "(OCR and structure as Markdown with page markers, by range of pages); use it whenever the person asks to read or OCR a document.")
    if editing:
        lines.append(editing)
    if knowledge:
        lines.append("- The person keeps knowledge archives (named collections of their documents, also called RAG): this project's are searched for every request, and any archive can be attached "
                     "to this conversation by name with `knowledge_use` (`knowledge_drop`, `knowledge_list` and `knowledge_search` go with it) when the person asks for one, in any wording. "
                     "The `[state]` note lists the archives this conversation can use now (`knowledge=`: their documents, pages and languages) and says when they change. "
                     "An archive holds its documents already read (OCR included) and indexed: to find a specific fact, number, name or claim in them, or when you do not know the page, "
                     "or across several documents, use `knowledge_search` (one short search for each point you need) and then `knowledge_read` for the pages around what it found, "
                     "instead of reading the files page by page; the passages added to a request are only a first sample. Read a known page directly with `read_document` or `read_file`, "
                     "and files that are not in an archive. The documents also keep their figures (charts, diagrams, maps, plasmid maps, sheets of drawings) as images: when the "
                     "person asks for a diagram, map, chart or 'the figure of ...', or the answer would be clearer with one, search with `filters: {kind: \"figure\"}`, get it with "
                     "`knowledge_figure` (look at it with `analyze_image`, show it in the answer, or put it in a Word document with `docx_edit` insert_picture) and always cite its "
                     "document and page. "
                     "`knowledge_check` says whether an archive is up to date with its sources and `knowledge_update` reads again what changed, when the person asks. "
                     "You can also build an archive: `knowledge_create` makes (or finds) the archive of this project, `knowledge_add` puts documents in it, one or in a batch (files and folders on this PC, "
                     "web addresses; the server reads, cuts and indexes them, and downloads the addresses into the archive's download folder), `knowledge_index` indexes again (or describes the passages) "
                     "and `knowledge_status` says how it stands and what still runs. For a request like 'find all the patents on X and add them to the RAG', search the web, collect the addresses of "
                     "the documents themselves, add them in one `knowledge_add` call (up to 200), then report with `knowledge_status`: what was added, what was already there, what failed and why.")
    if voice:
        lines.append(voice)
    if council:
        lines.append(council)
    if lessons:
        lines.append(f"- Lessons from earlier work are kept in {lessons}; relevant ones are shown to you at the start of a task as advice, not as orders.")
    return "\n".join(lines)


def absent_features(present: Dict[str, bool]) -> List[str]:
    return [why for key, why in FEATURES if not present.get(key)]


def status_of(agent: Any, window: int, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The live state of a conversation, read from the objects that already count it."""
    context = getattr(agent, "context", None)
    used = context.estimate_tokens(agent.messages) if context is not None else 0
    compaction = getattr(context, "compaction", None)
    comp = compaction.status() if compaction is not None else {}
    perms = getattr(agent, "permissions", None)
    gate = getattr(agent, "gate", None)
    out: Dict[str, Any] = {
        "context": {"tokens": used, "window": window, "share": round(used / window, 3) if window else 0.0,
                    "masking_from": getattr(context, "mask_at", None), "folds_from": comp.get("threshold"), "folds_so_far": comp.get("count", 0)},
        "steps": {"this_answer": len(agent.turns), "max": agent.max_turns if agent.max_turns and agent.max_turns > 0 else None},
        "tokens_used": agent.total_tokens, "tokens_max": agent.max_total_tokens, "seconds_max": agent.max_seconds,
        "loop_strikes": getattr(agent, "_loop_strikes", 0), "gate_rounds": getattr(gate, "rounds", None) if gate is not None else None,
        "refused_calls": getattr(perms, "denied", 0) if perms is not None else 0,
        "instructions_waiting": list(getattr(perms, "instructions", [])) if perms is not None else [],
        "mission": dict(getattr(agent, "mission", {}) or {}), "work_until_done": persistence.enabled(),
        "role": getattr(getattr(agent, "role", None), "id", "general"), "level": getattr(getattr(perms, "level", None), "value", ""),
    }
    out.update(extra or {})
    return out


def register_self_status(registry: Any, probe: Callable[[], Dict[str, Any]]) -> None:
    from tadamoo.tools.registry import EffectClass
    registry.register(
        name="self_status",
        description=("Reads the live state of this conversation: steps, tokens and time used and left, what the loop guards have seen, the mission and the lessons in force, and the "
                     "context figures (kept for the harness: the context folds on its own, so its fullness is not something to report or plan around). "
                     "Use it to check where you are in a long piece of work."),
        parameters={"type": "object", "properties": {}}, effect=EffectClass.PURE, handler=lambda: probe())
