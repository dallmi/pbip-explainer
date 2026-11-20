## Environment: Copilot Studio

This skill runs inside a Copilot Studio agent with code execution. Where the text below assumes Claude Code, this holds instead:

- **Sandbox:** no network, no package installation. Python 3 is available; `scripts/pbip_explain.py` needs only the standard library. `S` is `scripts/pbip_explain.py` inside this skill's folder.
- **Input:** the user attaches the PBIP project as one `.zip` (the folder that holds `X.pbip`, `X.SemanticModel/` and `X.Report/`). The tool recognises a zip by its extension: if the attachment arrives without `.zip` (for example because the user renamed it to get past the upload), copy it to `<name>.zip` first. If the zip is too large to attach, ask for a zip of only the `*.SemanticModel` and `*.Report` folders and the `.pbip` file; the `.pbi/` folders and `*.abf` caches are not needed.
- **Work folder:** `work/<name>/` is a folder in this conversation's sandbox. The user sees only the files you hand over as downloads, 10 MB at most each. Use the file names, not the `work/` paths, when you tell the user what you hand over.
- **Hand over** (section 5): `<Name>-documentation.html` and `<Name>-documentation.md` from `docs/`, `model-diagram.svg`, and the knowledge pack zipped as `<Name>-knowledge.zip` (`knowledge/` plus `enrichment.json`). Zip with Python's `zipfile`. If the HTML exceeds 10 MB, hand over the Markdown and the SVG only and say why.
- **Knowledge pack for a Q&A agent:** the files in `<Name>-knowledge.zip` can be uploaded as knowledge to a Copilot Studio agent without code execution (`agent-brief.md`, `*.csv`, the documentation `.md`). Mention this once in the handover.
- **Follow-up questions:** the sandbox may be reset between conversations. If `work/<name>/knowledge/model.json` is gone, ask the user to attach the zip again (or the `<Name>-knowledge.zip` you handed over: unpack it into `work/<name>/` and the `query` and `docs` commands work again).
- **Lessons:** you cannot change this skill. When you hit a Power BI construct the tool does not handle, say so in your answer under *Limits found*, with the object name and what was missing, so it can be carried back into the repository.
