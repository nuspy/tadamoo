# Markdown in and out

## Reading documents (MarkItDown)

Word, PowerPoint, web pages and e-books that the person attaches are read as Markdown by Microsoft's MarkItDown (MIT), run by program with no plugins and no model: headings, lists and tables are kept, the same text comes out every time, and it costs no model tokens. A PDF goes through it when neither pypdf nor PyMuPDF is installed. Excel, CSV and the other formats keep the readers they had.

MarkItDown is optional (`requirements.txt`). Where it is missing, or a file is damaged (the signature of a Word or PDF file is checked first, because MarkItDown would otherwise read it as plain text), the plain readers answer as before.

The agent reads a document of its workspace the same way with `convert_to_markdown(path, start, end)`: the line count and the headings first, then the text in pieces. The text is data, fenced like any other text read from a file.

The MCP server `markitdown-mcp` is not needed for this; it can still be added from Connectors for tools outside the harness.

## Saving a Markdown file (`serve/mdexport.py`)

When the person saves a `.md` file from a conversation (web, and the Android app), they choose:

- only the Markdown file;
- only the converted file, in a format chosen from a list;
- both, in one zip.

The list is what this PC can make (`GET /api/export/formats?lang=`): Word (`.docx`), OpenDocument text (`.odt`) and web page (`.html`) from the standard library; PDF through the installed Edge or Chrome in headless mode; EPUB and RTF when pandoc is installed. The conversion is a program, not a model: `docx`, `odt` and `html` are byte-identical for the same text; a PDF carries its print time.

Routes: `GET /api/outputs/<session>/<id>/convert` for a file the agent made, `GET /api/agent/sessions/<session>/file-convert?path=` for one in its folder; both take `format`, `mode` (`converted` or `both`) and `lang`. A PC without conversion offers no list and the file is saved as it always was. The last choice is remembered on the device.
