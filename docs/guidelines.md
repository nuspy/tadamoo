# Instruction files in cascade

Every folder from the repository root down to the working folder may hold an `AGENTS.md` (else `CLAUDE.md`, else `GEMINI.md`), and the person's
own `AGENTS.md` sits in the data folder. They all apply, the more general before the more specific, so a project file says only what is the
project's. Each file is read as far as 32 KiB; the system prompt says when one was cut.

With more than one file the technical session (the use `guidelines_merge`) reads them once and removes what is said twice: a concept an
earlier file already holds stays there, and where two files disagree the later one wins. The merged text is kept by the hash of the files in
`guidelines_merged.json` (the data folder), so nothing is asked again until one changes. The files are never rewritten. Without a session, or
when it fails or its text is not shorter, the files are joined as they are, each under its path.

Code: `tadamoo/guidelines.py` (`files`, `read`, `merged`), used by `tadamoo/stack.py`; tests: `tests/test_guidelines.py`.
